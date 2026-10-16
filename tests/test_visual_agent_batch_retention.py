"""Route regressions: redundant/invalid siblings must not erase valid repairs."""
import hashlib
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from studio import core, server


@pytest.mark.parametrize('sibling_status', ['no_change', 'rejected'])
def test_mixed_batch_reaches_visual_review_and_retains_valid_changes(
        tmp_path, monkeypatch, sibling_status):
    monkeypatch.setattr(core, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setenv('PPT_STUDIO_QUEUED_REPAIR_PLANS', '0')
    monkeypatch.setattr(server.rc, 'get_regions', lambda *_: {'regions': []})
    monkeypatch.setattr(server, 'record_task_outcome', lambda **_: None)
    project = core.create_project('Mixed batch regression', 'mock only', 1)
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
    renders = []

    def preview(worker, candidate, label):
        renders.append(deepcopy(candidate))
        out = worker.active_run_session / 'iterations' / label
        out.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (64, 36), 'white').save(out / 'chromium.png')
        return {'chromium': f'iterations/{label}/chromium.png',
                'preview_sha256': hashlib.sha256(label.encode()).hexdigest()}

    monkeypatch.setattr(server, '_render_visual_agent_browser_preview', preview)
    monkeypatch.setattr(server, '_render_visual_agent_candidate', lambda *_: {
        'browser_qa': {'layout_contract_pass': True, 'pptx_pass': True, 'workflow_pass': True},
        'pptx_report': {'native_text': {'ok': True}, 'saved_layout_check': {'ok': True}},
    })
    issues = [
        {'issue_id': 'heading-position', 'priority': 'P0', 'status': 'open',
         'element_ids': ['heading'], 'description': 'Move heading'},
        {'issue_id': 'body-position', 'priority': 'P1', 'status': 'open',
         'element_ids': ['body'], 'description': 'Check body'},
    ]
    phases = []
    changed_box = [.2, .1, .4, .1]

    async def model(candidate, current_slide, *_, phase, **kwargs):
        phases.append(phase)
        if len(phases) == 1:
            body_box = [.1, .4, .5, .1] if sibling_status == 'no_change' else [2, .4, .5, .1]
            return {'diagnosis': {'issues': deepcopy(issues)}, 'tool_calls': [
                {'tool': 'edit_element', 'arguments': {
                    'element_id': 'heading', 'patch': {'box': changed_box}}},
                {'tool': 'edit_element', 'arguments': {
                    'element_id': 'body', 'patch': {'box': body_box}}},
            ]}
        if phase == 'reflect':
            # The previous bug stopped before either this render or this turn.
            assert current_slide['elements'][0]['box'] == changed_box
            assert len(renders) == 2
            feedback = kwargs['tool_feedback']
            assert len(feedback['batch_issues']) == 2
            assert [event['status'] for event in feedback['tool_events']] == ['applied', sibling_status]
            return {'decision': 'accept', 'confidence': .95,
                    'issues': [{'issue_id': issue['issue_id'], 'status': 'fixed'} for issue in issues]}
        return {'diagnosis': {'issues': []}, 'tool_calls': [], 'decision': 'done'}

    monkeypatch.setattr(server, 'visual_agent_turn', model)
    response = TestClient(server.app).post(f'/api/projects/{project["id"]}/visual-agent/run', json={
        'slide_id': 'S001', 'mode': 'restore', 'api_key': 'mock-only',
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert phases == ['diagnose', 'reflect', 'diagnose']
    assert result['status'] == 'candidate_ready'
    assert result['iterations'][0]['outcome'] == 'accept'
    assert not any(issue.get('skip_reason') for issue in result['issues'])
    saved = json.loads((root / 'build' / 'visual_agent' / result['token'] / 'candidate_project.json').read_text())
    assert saved['slides'][0]['elements'][0]['box'] == changed_box
    assert saved['slides'][0]['elements'][1]['box'] == [.1, .4, .5, .1]
    assert core.read_project(project['id']) == original
