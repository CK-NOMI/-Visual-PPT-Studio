"""Delivery boundaries: actual saved changes and stale artifact rejection."""
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from studio import core, server


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE', '1')
    p = core.create_project('Delivery', 'Native text', 1)
    core.update_slide(p, 'S001', {'title': 'Visible title'})
    p['locked'] = True
    core.save_project(p)
    return p, TestClient(server.app)


def test_calibration_audits_saved_objects_and_preserves_backup(project):
    p, client = project
    original = deepcopy(p)
    elements = deepcopy(p['slides'][0]['elements'])
    elements[0]['style']['size'] = 22
    r = client.put(f'/api/projects/{p["id"]}/slides/S001', json={
        'elements': elements, '_calibration': {'expected_revision': p['revision'], 'phase': 'apply', 'round': 1}})
    assert r.status_code == 200, r.text
    audit = r.json()['calibration_audit']
    saved = core.read_project(p['id'])
    assert audit['applied'] and audit['after_revision'] == saved['revision']
    for row in audit['changes']:
        assert row['after'] == next(e for e in saved['slides'][0]['elements'] if e['id'] == row['id'])
    backup = core.project_dir(p['id']) / 'build' / 'calibration_changes' / audit['backup']
    assert json.loads(backup.read_text()) == original
    result = client.post(f'/api/projects/{p["id"]}/export?mode=editable')
    assert result.status_code == 200, result.text
    report = result.json()
    assert report['calibration_changes'][0]['after_revision'] == saved['revision']
    assert report['native_text']['ok'] and report['saved_layout_check']['ok']


def test_stale_calibration_cannot_overwrite_newer_project(project):
    p, client = project
    before = deepcopy(p)
    r = client.put(f'/api/projects/{p["id"]}/slides/S001', json={
        'elements': [], '_calibration': {'expected_revision': p['revision'] - 1}})
    assert r.status_code == 400
    assert core.read_project(p['id']) == before


def test_same_revision_content_tampering_disables_download(project):
    p, client = project
    report = client.post(f'/api/projects/{p["id"]}/export?mode=editable').json()
    assert report['artifact_sha256']
    p['slides'][0]['elements'][0]['style']['color'] = 'FF0000'
    core.safe_json_write(core.project_dir(p['id'])/'project.json', p)
    assert client.get(f'/build/{p["id"]}/{report["filename"]}').status_code == 409
    current = client.get(f'/build/{p["id"]}/report_editable.json').json()
    assert current['artifact_valid_current'] is False


def test_missing_export_report_is_normal_empty_state_not_http_404(project):
    p,client=project
    response=client.get(f'/build/{p["id"]}/report_editable.json')
    assert response.status_code==200
    body=response.json()
    assert body['artifact_valid_current'] is False
    assert body['artifact_status']=='尚未导出当前项目版本'


def test_temporary_chromium_preview_clone_is_not_listed_as_user_project(project):
    p,_=project
    temp_id='f'*12
    clone=deepcopy(p);clone['id']=temp_id
    root=core.DATA/temp_id;root.mkdir(parents=True)
    core.safe_json_write(root/'project.json',clone)
    (root/'.visual_agent_preview').write_text('temporary')
    assert temp_id not in {row['id'] for row in core.list_projects()}


def test_smart_diagnostic_cache_keys_revision_sources_layout_fonts_and_renderer(project, tmp_path, monkeypatch):
    from PIL import Image
    from studio import server
    p,_=project;root=server.project_dir(p['id']);slide=p['slides'][0]
    slide['images']={'design':'S001_design.png','clean':'S001_clean.png'}
    for filename,color in [('S001_design.png','white'),('S001_clean.png','white')]:Image.new('RGB',(64,36),color).save(root/'images'/filename)
    preview=tmp_path/'preview.png';Image.new('RGB',(64,36),'white').save(preview)
    report={'artifact_sha256':'1'*64,'preview_renderer':'LibreOffice (test)'}
    regions={'source_sha256':'ocr-sha','regions':[{'id':'title','text':'Title'}]}
    issues=[{'issue_id':'layout-1','issue_type':'line_geometry','suggested_action':'repair_line','matched_elements':['title']}]
    base,_=server._smart_diagnostic_cache(p['id'],p,slide,regions,preview,report,issues,'model-v1')
    changed=deepcopy(p);changed['revision']+=1
    revision_key,_=server._smart_diagnostic_cache(p['id'],changed,changed['slides'][0],regions,preview,report,issues,'model-v1')
    assert revision_key!=base
    p['slides'][0]['elements'][0]['style']['size']+=1
    layout_key,_=server._smart_diagnostic_cache(p['id'],p,slide,regions,preview,report,issues,'model-v1')
    assert layout_key!=base
    p['slides'][0]['elements'][0]['style']['size']-=1
    Image.new('RGB',(64,36),'black').save(preview)
    render_key,_=server._smart_diagnostic_cache(p['id'],p,slide,regions,preview,report,issues,'model-v1')
    assert render_key!=base


def test_clean_ocr_cache_hits_and_invalidates_on_revision_or_source(project, monkeypatch):
    from PIL import Image
    from studio import repair_pipeline as rp, reconstruction as rc
    p,_=project;root=core.project_dir(p['id']);slide=p['slides'][0]
    slide['images']={'design':'S001_design.png','clean':'S001_clean.png'}
    slide['image_metadata']={'design':{'sha256':'d'},'clean':{'source_design_sha256':'d'}}
    for filename,color in [('S001_design.png','white'),('S001_clean.png','white')]:
        Image.new('RGB',(32,18),color).save(root/'images'/filename)
    monkeypatch.setattr(rc,'available_ocr',lambda:{'rapidocr':True})
    calls=[]
    monkeypatch.setattr(rc,'_rapidocr',lambda image:(calls.append(image.shape) or [{'text':'x'}]))
    assert rp._cached_clean_ocr(p,slide)==[{'text':'x'}]
    assert rp._cached_clean_ocr(p,slide)==[{'text':'x'}]
    assert len(calls)==1
    p['revision']+=1
    rp._cached_clean_ocr(p,slide)
    assert len(calls)==2
    Image.new('RGB',(32,18),'black').save(root/'images'/'S001_clean.png')
    rp._cached_clean_ocr(p,slide)
    assert len(calls)==3


def test_render_comparison_cache_reuses_heatmap_and_invalidates_inputs(project, tmp_path, monkeypatch):
    from PIL import Image
    from studio import reconstruction as rc
    p,_=project;root=core.project_dir(p['id']);slide=p['slides'][0]
    slide['images']={'design':'S001_design.png'}
    Image.new('RGB',(64,36),'white').save(root/'images'/'S001_design.png')
    preview=tmp_path/'preview.png';Image.new('RGB',(64,36),'black').save(preview)
    monkeypatch.setattr(rc,'score_render_fidelity',lambda *args:{'score':1})
    first=rc.compare_render(p,'S001',preview,tmp_path/'first')
    second=rc.compare_render(p,'S001',preview,tmp_path/'second')
    assert first['cache_hit'] is False and second['cache_hit'] is True
    assert (tmp_path/'second'/'S001_diff.png').is_file()
    p['revision']+=1
    revised=rc.compare_render(p,'S001',preview,tmp_path/'third')
    assert revised['cache_hit'] is False
    Image.new('RGB',(64,36),'gray').save(preview)
    changed=rc.compare_render(p,'S001',preview,tmp_path/'fourth')
    assert changed['cache_hit'] is False


def test_style_rollback_restores_exact_layout_from_backup(project):
    p, client = project
    before = deepcopy(p['slides'][0])
    elements = deepcopy(before['elements'])
    elements[0]['style']['size'] = 8
    url = f'/api/projects/{p["id"]}/slides/S001'
    applied = client.put(url, json={'elements': elements, '_calibration': {
        'expected_revision': p['revision'], 'phase': 'apply'}}).json()['calibration_audit']
    response = client.put(url, json={'elements': before['elements'], '_calibration': {
        'expected_revision': applied['after_revision'], 'phase': 'rollback', 'restore_backup': applied['backup']}})
    assert response.status_code == 200, response.text
    assert core.read_project(p['id'])['slides'][0] == before
    # A client cannot turn a different geometry into an "exact rollback".
    modified = deepcopy(before['elements'])
    modified[0]['box'][0] += .01
    refused = client.put(url, json={'elements': modified, '_calibration': {
        'expected_revision': core.read_project(p['id'])['revision'], 'phase': 'rollback', 'restore_backup': applied['backup']}})
    assert refused.status_code == 400
    assert core.read_project(p['id'])['slides'][0] == before
