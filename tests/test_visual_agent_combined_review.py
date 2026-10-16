"""A cached diagnosis plan plus combined reviews needs two/three model turns."""
import asyncio
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from studio import ai, core, server


@pytest.mark.parametrize('rounds', [1, 2])
def test_diagnosis_and_two_edit_rounds_reuse_plans_and_whole_page_review(
        tmp_path, monkeypatch, rounds):
    monkeypatch.setattr(core, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setenv('PPT_STUDIO_QUEUED_REPAIR_PLANS', '1')
    monkeypatch.setattr(server.rc, 'get_regions', lambda *_: {'regions': []})
    monkeypatch.setattr(server, 'record_task_outcome', lambda **_: None)
    project = core.create_project('Combined review', 'mock only', 1)
    root = core.project_dir(project['id'])
    Image.new('RGB', (64, 36), 'white').save(root / 'images' / 'S001_design.png')
    slide = project['slides'][0]
    slide['images']['design'] = 'S001_design.png'
    slide['elements'] = [
        {'id': 'heading', 'kind': 'text', 'text': 'Heading',
         'box': [.1, .1, .4, .1], 'style': {'size': 18}},
        {'id': 'body', 'kind': 'text', 'text': 'Body',
         'box': [.1, .4, .5, .1], 'style': {'size': 14}},
    ]
    project['locked'] = True
    core.safe_json_write(root / 'project.json', project)
    original = deepcopy(project)

    class Preview:
        active_run_session = None

    worker = Preview()
    monkeypatch.setattr(server, '_get_stage_preview_worker', lambda *_: worker)

    def preview(worker, candidate, label):
        path = worker.active_run_session / 'iterations' / label / 'chromium.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (64, 36), 'white').save(path)
        return {'chromium': f'iterations/{label}/chromium.png'}

    monkeypatch.setattr(server, '_render_visual_agent_browser_preview', preview)
    monkeypatch.setattr(server, '_render_visual_agent_candidate', lambda *_: {
        'browser_qa': {'layout_contract_pass': True, 'pptx_pass': True, 'workflow_pass': True},
        'pptx_report': {'native_text': {'ok': True}, 'saved_layout_check': {'ok': True}},
    })
    issues = [
        {'issue_id': 'heading-position', 'priority': 'P0', 'status': 'open',
         'element_ids': ['heading'], 'description': 'Heading position'},
        {'issue_id': 'body-position', 'priority': 'P1', 'status': 'open',
         'element_ids': ['body'], 'description': 'Body position'},
    ]

    def edits(x):
        return [{'tool': 'edit_element', 'arguments': {
            'element_id': element_id, 'patch': {'box': [x, y, width, .1]}}}
            for element_id, y, width in [('heading', .1, .4), ('body', .4, .5)]]

    phases = []

    async def model(candidate, current_slide, *_, phase, **kwargs):
        phases.append(phase)
        if phase == 'diagnose':
            assert len(phases) == 1, 'Cached plans should avoid another diagnosis/plan request'
            assert kwargs.get('diagnose_only') is True
            return {'diagnosis': {'issues': deepcopy(issues)}, 'tool_calls': [],
                    'repair_plans': [{'issue_id': issue['issue_id'], 'tool_calls': [call]}
                                     for issue, call in zip(issues, edits(.2))]}
        turn = phases.count('reflect')
        assert [element['box'][0] for element in current_slide['elements']] == ([.2, .2] if turn == 1 else [.3, .3])
        assert len(kwargs['tool_feedback']['batch_issues']) == 2
        if turn < rounds:
            return {'decision': 'continue', 'page_review_passed': False,
                    'issues': deepcopy(issues), 'tool_calls': edits(.3)}
        return {'decision': 'accept', 'page_review_passed': True, 'confidence': .96,
                'issues': [{'issue_id': issue['issue_id'], 'status': 'fixed'} for issue in issues]}

    monkeypatch.setattr(server, 'visual_agent_turn', model)
    client = TestClient(server.app)
    diagnosed = client.post(f'/api/projects/{project["id"]}/visual-agent/run', json={
        'slide_id': 'S001', 'mode': 'restore', 'api_key': 'mock-only', 'diagnose_only': True})
    assert diagnosed.status_code == 200, diagnosed.text
    diagnosis = diagnosed.json()
    assert core.read_project(project['id']) == original
    diagnosis_candidate = json.loads((root / 'build' / 'visual_agent' / diagnosis['token'] / 'candidate_project.json').read_text())
    assert diagnosis_candidate == original
    response = client.post(f'/api/projects/{project["id"]}/visual-agent/run', json={
        'slide_id': 'S001', 'mode': 'restore', 'api_key': 'mock-only',
        'diagnosis_token': diagnosis['token'], 'base_candidate_token': diagnosis['token']})
    assert response.status_code == 200, response.text
    result = response.json()
    assert phases == ['diagnose'] + ['reflect'] * rounds
    assert result['status'] == 'candidate_ready'
    assert result['final_visual_check_passed'] is True
    assert len(result['iterations']) == rounds
    assert core.read_project(project['id']) == original


def test_diagnose_prompt_requests_unexecuted_plans(tmp_path, monkeypatch):
    path = tmp_path / 'design.png'
    Image.new('RGB', (64, 36), 'white').save(path)
    prompts = []

    async def capture(messages, *_, **kwargs):
        prompts.append(messages[1]['content'][0]['text'])
        return {'diagnosis': {'issues': []}, 'tool_calls': [], 'repair_plans': []}

    monkeypatch.setattr(ai, 'call_deepseek', capture)
    asyncio.run(ai.visual_agent_turn({}, {'id': 'S001', 'elements': []}, path, path,
                                     diagnose_only=True))
    assert 'tool_calls必须为空数组' in prompts[0]
    assert 'repair_plans中的待执行工具计划' in prompts[0]
