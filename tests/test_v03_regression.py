"""Real-world dense infographic regression tests and explicit fallback boundaries."""
import json
from io import BytesIO
from PIL import Image
from fastapi.testclient import TestClient
import pytest
from pptx import Presentation
from studio import core, server, reconstruction as rc

@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path)
    monkeypatch.setattr(server,'DATA',tmp_path)
    p=core.create_project('Dense slide','Test',1)
    p['locked']=True;core.save_project(p)
    img=Image.new('RGB',(640,360),'#E7F4FF')
    f=BytesIO();img.save(f,'PNG')
    core.write_image(p,'S001','design',f.getvalue(),'S001_design.png')
    return p, f.getvalue()

def test_raster_first_not_falsely_editable(env):
    p,data=env
    result=rc.initialize_raster_first(p,'S001')
    assert result['native_text_count']==0
    assert p['slides'][0]['elements']==[]
    assert p['slides'][0]['image_metadata']['clean']['embedded_text_remaining'] is True
    report=server.render_deck(p['id'],'editable')
    assert report['builder']['backend']=='python-pptx'
    assert not report['native_text']['slides'][0]['native_text_count']
    assert any('栅格文字' in w['message'] for w in report['qa']['warnings'])
    deck=Presentation(core.project_dir(p['id'])/'build'/report['filename'])
    assert len(deck.slides)==1
    assert not any(x.has_text_frame and x.text for x in deck.slides[0].shapes)

def test_promotion_has_real_text_and_minimal_changed_pixels(env):
    import numpy as np
    p,data=env;rc.initialize_raster_first(p,'S001')
    r={'text':'TEST','editable':True,'role':'custom','box':[.1,.1,.2,.08]}
    rc.persist_regions(p,'S001',[r]);c=rc.generate_clean_candidate(p,'S001',[r],mask_mode='rectangle')
    assert c['report']['mask_percent']>0
    src=rc.image_arr(rc.source_for(p,'S001'))
    clean=rc.image_arr(rc.lab_dir(p['id'])/'S001_candidate.png')
    mask=np.asarray(Image.open(rc.lab_dir(p['id'])/'S001_mask.png'))>0
    assert np.array_equal(src[~mask],clean[~mask]), 'No pixel outside selection may change'
    rc.accept_clean(p,'S001')
    core.update_slide(p,'S001',{'elements':[{'id':'ocr_001','kind':'text','text':'TEST','box':r['box'],'style':{'size':20,'font':'Arial','color':'000000'}}]})
    report=server.render_deck(p['id'],'editable')
    assert report['native_text']['ok']
    actual=Presentation(core.project_dir(p['id'])/'build'/report['filename'])
    assert 'TEST' in [x.text for x in actual.slides[0].shapes if x.has_text_frame]
    assert report['coverage'][0]['confirmed_editable_regions']==1

def test_design_change_requires_reapproval(env):
    p,data=env;rc.initialize_raster_first(p,'S001')
    altered=Image.new('RGB',(640,360),'#091A37');b=BytesIO();altered.save(b,'PNG')
    core.write_image(p,'S001','design',b.getvalue(),'S001_design.png')
    assert p['slides'][0]['image_metadata']['clean']['stale_source']
    with pytest.raises(ValueError,match='质量硬错误'):
        server.render_deck(p['id'],'editable')
    assert server.audit(p)['counts']['errors']>=1

def test_cached_preview_refused_after_project_edit(env):
    p,data=env;rc.initialize_raster_first(p,'S001')
    server.render_deck(p['id'],'editable')
    # Unit suite disables expensive PDF render; create an inert placeholder to
    # reach the revision gate without claiming a real Office preview.
    Image.new('RGB',(640,360),'white').save(core.project_dir(p['id'])/'build'/'editable_S001_preview.png')
    core.update_slide(p,'S001',{'intent':'new'})
    with TestClient(server.app) as client:
        r=client.post(f"/api/projects/{p['id']}/slides/S001/compare-render")
    assert r.status_code==400
    assert '已修改' in r.text

def test_ocr_candidate_not_auto_selected(env,monkeypatch):
    p,data=env
    monkeypatch.setattr(rc,'available_ocr',lambda:{'rapidocr':True,'tesseract':False,'simple_lama':False,'opencv':True})
    monkeypatch.setattr(rc,'_rapidocr',lambda arr:[{'text':'欢迎','box':[.1,.1,.2,.1],'confidence':.96}])
    r=rc.detect_text(p,'S001','rapidocr')
    assert len(r['regions'])==1 and r['regions'][0]['editable'] is False
    assert rc.reconstruction_coverage(p,'S001')['confirmed_editable_regions']==0

def test_diff_colormap_clips_before_uint8(env):
    from PIL import Image as PILImage
    p,data=env
    p['slides'][0]['elements']=[{'id':'editable_heading','kind':'text','text':'TEST','box':[.1,.1,.2,.1]}]
    preview=core.project_dir(p['id'])/'build'/'v.png'
    PILImage.new('RGB',(640,360),'#F00000').save(preview)
    result=rc.compare_render(p,'S001',preview)
    assert result['mae_0_255']>50
    assert result['native_text_mae_0_255'] is not None
    assert result['background_mae_0_255'] is not None
    assert result['native_text_regions'][0]['id']=='editable_heading'
    diff=PILImage.open(rc.lab_dir(p['id'])/result['diff']).convert('RGB')
    assert diff.size==(640,360)
