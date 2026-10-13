from io import BytesIO
from PIL import Image
import pytest
from studio import core,reconstruction as rc
from studio.browser_qa import assess_page


@pytest.fixture
def scene(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path)
    monkeypatch.setattr(rc,'available_ocr',lambda:{'rapidocr':False})
    p=core.create_project('QA','Content',1);p['locked']=True;core.save_project(p)
    data=BytesIO();Image.new('RGB',(960,540),'white').save(data,'PNG')
    for role in ('design','clean'):core.write_image(p,'S001',role,data.getvalue(),role+'.png')
    core.update_slide(p,'S001',{'elements':[{'id':'t','kind':'text','text':'Test  ',
                                          'box':[.1,.1,.3,.1],'style':{'size':20,'font':'Arial'}}]})
    shot=tmp_path/'shot.png';shot.write_bytes(data.getvalue())
    return p,p['slides'][0],shot,tmp_path


def observation(element):
    return {'stage':{'width':960,'height':540},'objects':[{
        'id':element['id'],'text':'Test  ','box':element['box'],'font_size_px':20,
        'baseline_px':element['text_layout']['baseline_pt'],'font_status':'loaded',
        'range_rects':[{'x':96,'y':60,'width':55,'height':24}],
        'ink_box':{'x':96,'y':60,'width':45,'height':15}}]}


def test_empty_dom_is_not_a_successful_empty_page(scene):
    p,s,shot,folder=scene
    result=assess_page(p,s,{'stage':{'width':960,'height':540},'objects':[]},shot,folder)
    assert not result['layout_contract_pass']
    assert result['failed_objects']==1
    assert result['objects'][0]['anomalies'][0]['type']=='missing_dom_object'
    assert (folder/result['objects'][0]['crop']).is_file()


def test_browser_shrink_and_removed_spaces_are_detected_independently(scene):
    p,s,shot,folder=scene;obs=observation(s['elements'][0])
    obs['objects'][0].update(text='Test',font_size_px=10)
    result=assess_page(p,s,obs,shot,folder)
    types={a['type'] for a in result['objects'][0]['anomalies']}
    assert 'browser_changed_font_size' in types
    assert 'text_or_whitespace_changed' in types
    assert not result['layout_contract_pass']


def test_visible_overlap_is_distinct_from_overlapping_transparent_frames(scene):
    from copy import deepcopy
    p,s,shot,folder=scene
    other=deepcopy(s['elements'][0]);other['id']='t2';other['run_id']='separate-run';other['line_id']='separate-line'
    s['elements'].append(other)
    obs=observation(s['elements'][0]);dom=deepcopy(obs['objects'][0]);dom['id']='t2';obs['objects'].append(dom)
    result=assess_page(p,s,obs,shot,folder)
    assert result['visual_error_objects']==1
    assert any(a['type']=='visible_glyph_overlap' for o in result['objects'] for a in o['anomalies'])


def test_conflicting_ocr_is_preserved_and_marked_for_review():
    regions=[{'text':'T','box':[.049,.191,.0108,.0278],'confidence':.91},
             {'text':'1','box':[.0493,.1835,.0096,.0354],'confidence':.95},
             {'text':'1','box':[.6,.2,.01,.03],'confidence':.98}]
    kept=rc._dedupe_ocr_fragments(regions)
    assert len(kept)==3
    marked=rc.mark_ocr_conflicts(kept)
    assert sum(bool(r.get('review_required')) for r in marked)==2
    assert not regions[0].get('review_required')
    assert next(r for r in marked if r['box'][0]==.6).get('review_required') is None


def test_confirmed_ocr_fragment_can_still_be_deduplicated():
    regions=[{'text':'Token','box':[.1,.2,.06,.03]},
             {'text':'Token label','box':[.1,.2,.2,.04]}]
    assert [r['text'] for r in rc._dedupe_ocr_fragments(regions)]==['Token label']


def test_source_width_note_is_info_but_real_ink_clipping_is_error(scene):
    p,s,shot,folder=scene;e=s['elements'][0];e['text_layout']['layout_warning'].append('source_width_exceeded:no_font_shrink')
    obs=observation(e);result=assess_page(p,s,obs,shot,folder)
    note=next(a for a in result['objects'][0]['anomalies'] if a['type']=='source_width_exceeded:no_font_shrink')
    assert note['severity']=='info' and result['line_issues']
    obs['objects'][0]['ink_box']['width']=900
    bad=assess_page(p,s,obs,shot,folder)
    assert any(a['type']=='glyph_outside_frame' and a['severity']=='error' for a in bad['objects'][0]['anomalies'])


def test_independent_source_ink_disproves_ocr_padding_but_not_wrong_color(scene):
    from PIL import ImageDraw
    p,s,shot,folder=scene;e=s['elements'][0];design=Image.new('RGB',(960,540),'white');ImageDraw.Draw(design).rectangle((100,65,144,79),fill='black')
    design.save(core.project_dir(p['id'])/'images'/s['images']['design'])
    obs=observation(e);obs['objects'][0].update(ink_box={'x':100,'y':65,'width':45,'height':15},font_color='rgb(0, 0, 0)')
    diffs={'objective_score':{'differences':[{'id':e['id'],'type':'glyph_size','expected_height':.08,'actual_height':.028},
                                           {'id':e['id'],'type':'text_color','expected':'FF0000','actual':'000000'}]}}
    result=assess_page(p,s,obs,shot,folder,diffs)
    evidence={a['type']:a for a in result['objects'][0]['anomalies']}
    assert evidence['image_glyph_size']['severity']=='info'
    assert evidence['image_glyph_size']['classification']=='ocr_padding_false_positive'
    assert evidence['image_text_color']['severity']=='info'
    obs['objects'][0]['font_color']='rgb(255, 0, 0)'
    wrong=assess_page(p,s,obs,shot,folder,diffs)
    assert next(a for a in wrong['objects'][0]['anomalies'] if a['type']=='image_text_color')['severity']=='review'


def test_tile_coordinates_map_to_one_source_detection(monkeypatch):
    import numpy as np
    calls=[]
    def fake(arr):
        calls.append(arr.shape)
        if len(calls)==1:return [{'text':'Label','box':[.1,.1,.2,.1],'confidence':.99}]
        if len(calls)==2:return [{'text':'Label','box':[.1/.54,.1/.54,.2/.54,.1/.54],'confidence':.99}]
        return []
    monkeypatch.setattr(rc,'_rapidocr',fake)
    result=rc._rapidocr_multiscale(np.zeros((1000,1000,3),dtype=np.uint8),full_upscale=False)
    assert len(result)==1 and result[0]['box']==pytest.approx([.1,.1,.2,.1])


def test_ocr_overlap_disproved_only_when_actual_ink_is_disjoint(scene):
    from copy import deepcopy
    p,s,shot,folder=scene;other=deepcopy(s['elements'][0]);other.update(id='second',run_id='second',line_id='second');s['elements'].append(other)
    obs=observation(s['elements'][0]);right=deepcopy(obs['objects'][0]);right.update(id='second',ink_box={'x':500,'y':60,'width':45,'height':15});obs['objects'].append(right)
    visual={'objective_score':{'differences':[{'type':'text_overlap','objects':['t','second']}]}}
    result=assess_page(p,s,obs,shot,folder,visual)
    assert all(a['severity']=='info' for o in result['objects'] for a in o['anomalies'] if a['type']=='image_text_overlap')
    right['ink_box']['x']=98
    overlap=assess_page(p,s,obs,shot,folder,visual)
    assert overlap['visual_error_objects']==1
    assert all(a['severity']=='review' for o in overlap['objects'] for a in o['anomalies'] if a['type']=='image_text_overlap')
