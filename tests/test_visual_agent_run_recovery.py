"""Disconnected clients must not lose, cancel, or duplicate server-owned jobs."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from studio import core, server


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setattr(server, '_visual_agent_runs', {})
    monkeypatch.setattr(server, '_visual_agent_jobs', {})
    return core.create_project('Recovery regression', 'mock only', 2)


def test_active_run_discovery_and_conflict_return_original_run_id(project):
    run_id = 'a' * 32
    server._visual_agent_runs[run_id] = {
        'project_id': project['id'], 'slide_id': 'S002', 'mode': 'restore', 'running': True,
        'stop_event': asyncio.Event(), '_event_loop': object(), '_active_model_task': object(),
        'progress': {'phase': 'model_feedback', 'batch_issue_ids': ['i1', 'i2'], 'batch_size': 2},
    }
    client = TestClient(server.app)
    response = client.get(f'/api/projects/{project["id"]}/visual-agent/active-run?slide_id=S002')
    assert response.status_code == 200
    active = response.json()
    assert active['run_id'] == run_id and active['running'] is True
    assert active['progress']['batch_issue_ids'] == ['i1', 'i2']
    assert not any(key.startswith('_') for key in active)
    assert 'stop_event' not in active
    assert client.get(f'/api/projects/{project["id"]}/visual-agent/active-run?slide_id=S001').json() is None
    duplicate = client.post(f'/api/projects/{project["id"]}/visual-agent/run?background=true', json={
        'slide_id': 'S002', 'api_key': 'mock-only'})
    assert duplicate.status_code == 409
    assert duplicate.json()['detail']['run_id'] == run_id
    assert not server._visual_agent_jobs


def test_request_cancellation_leaves_job_alive_and_completion_discoverable(project, monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        run_id, token = 'b' * 32, 'c' * 32

        async def execute(pid, request):
            calls.append(request.run_id)
            server._visual_agent_runs[request.run_id] = {
                'project_id': pid, 'slide_id': request.slide_id, 'running': True,
                'progress': {'phase': 'editing', 'slide_id': request.slide_id},
            }
            started.set()
            await release.wait()
            return {'token': token, 'run_id': request.run_id}

        monkeypatch.setattr(server, '_execute_visual_agent_run', execute)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            request = asyncio.create_task(client.post(f'/api/projects/{project["id"]}/visual-agent/run',
                json={'run_id': run_id, 'slide_id': 'S001', 'api_key': 'mock-only'}))
            await asyncio.wait_for(started.wait(), 2)
            owned_task = server._visual_agent_jobs[run_id]['task']
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not owned_task.done()
            active = (await client.get(f'/api/projects/{project["id"]}/visual-agent/active-run?slide_id=S001')).json()
            assert active['run_id'] == run_id and active['running']
            duplicate = await client.post(f'/api/projects/{project["id"]}/visual-agent/run?background=true',
                json={'run_id': 'd' * 32, 'slide_id': 'S001', 'api_key': 'mock-only'})
            assert duplicate.status_code == 409
            release.set()
            await asyncio.wait_for(owned_task, 2)
            await asyncio.sleep(0)
            complete = (await client.get(f'/api/projects/{project["id"]}/visual-agent/runs/{run_id}')).json()
            assert complete['running'] is False and complete['result_token'] == token
            assert calls == [run_id]
            repeated = await client.post(f'/api/projects/{project["id"]}/visual-agent/run?background=true',
                json={'run_id': run_id, 'slide_id': 'S001', 'api_key': 'mock-only'})
            assert repeated.status_code == 202 and len(calls) == 1

    asyncio.run(scenario())


def test_background_start_returns_before_long_job_finishes(project, monkeypatch):
    async def scenario():
        release = asyncio.Event()
        async def execute(pid, request):
            await release.wait()
            return {'token': 'e' * 32}
        monkeypatch.setattr(server, '_execute_visual_agent_run', execute)
        run_id = 'f' * 32
        response = await server.run_visual_agent(project['id'], server.VisualAgentRequest(
            run_id=run_id, slide_id='S001', api_key='mock-only'), background=True)
        assert response.status_code == 202
        assert json.loads(response.body)['run_id'] == run_id
        task = server._visual_agent_jobs[run_id]['task']
        assert not task.done()
        release.set()
        await task
        await asyncio.sleep(0)
        assert server._visual_agent_runs[run_id]['running'] is False
    asyncio.run(scenario())
