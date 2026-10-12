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
