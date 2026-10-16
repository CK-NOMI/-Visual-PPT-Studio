from copy import deepcopy

from studio import core
from studio.visual_agent_tools import execute_tool_calls


def project(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'DATA', tmp_path)
    p = core.create_project('Agent tools', 'test', 1)
    core.update_slide(p, 'S001', {'title': 'Visible title'})
    return p


def test_model_can_edit_multiple_geometry_and_typography_properties_in_isolated_graph(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    original = deepcopy(p)
    candidate, events, _ = execute_tool_calls(p, 'S001', [{
        'tool': 'edit_element', 'arguments': {'slide_id': 'S001', 'element_id': 'title', 'patch': {
            'box': [.12, .11, .76, .14], 'style': {'size': 36, 'bold': True, 'align': 'center', 'line_spacing': 1.2}}}}], mode='free')
    assert events[0]['status'] == 'applied'
    title = next(e for e in candidate['slides'][0]['elements'] if e['id'] == 'title')
    assert title['box'] == [.12, .11, .76, .14]
    assert title['style']['size'] == 36 and title['style']['line_spacing'] == 1.2
    assert p == original


def test_restore_mode_requires_design_text_and_source_geometry_for_new_native_text(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    box = [.1, .2, .04, .06]
    candidate, events, _ = execute_tool_calls(p, 'S001', [{
        'tool': 'create_element', 'arguments': {'slide_id': 'S001', 'element': {
            'kind': 'text', 'text': '1', 'box': box, 'style': {'size': 18, 'color': 'FFFFFF', 'align': 'center'}}}}],
        mode='restore', source_regions=[{'text': '1', 'box': box}])
    assert events[0]['status'] == 'applied'
    assert candidate['slides'][0]['elements'][-1]['text'] == '1'
    _, rejected, _ = execute_tool_calls(p, 'S001', [{
        'tool': 'create_element', 'arguments': {'slide_id': 'S001', 'element': {
            'kind': 'text', 'text': 'invented', 'box': box, 'style': {'size': 18}}}}], mode='restore', source_regions=[])
    assert rejected[0]['status'] == 'rejected'
    assert '来源证据' in rejected[0]['error']


def test_restore_mode_can_use_verbatim_high_confidence_visual_missing_issue(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    box = [.1, .08, .7, .06]
    issue = {'issue_id': 'missing-title', 'priority': 'P0', 'category': '关键内容缺失',
        'description': 'Design顶部明确显示英文标题 “Fast Inference from Transformers”，当前页面完全缺失。',
        'expected_result': 'Fast Inference from Transformers', 'region': [.08,.05,.8,.12], 'confidence': .96}
    candidate, events, _ = execute_tool_calls(p, 'S001', [{
        'tool': 'create_element', 'arguments': {'slide_id': 'S001', 'evidence_issue_id': 'missing-title',
            'element': {'kind':'text','text':'Fast Inference from Transformers','box':box,
                'style':{'size':28,'color':'17243A'}}}}], mode='restore', source_regions=[], visual_issues=[issue])
    assert events[0]['status'] == 'applied'
    assert candidate['slides'][0]['elements'][-1]['text'] == issue['expected_result']


def test_restore_mode_rejects_duplicate_text_at_existing_visual_location(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    slide = p['slides'][0]
    box = [.1, .2, .04, .06]
    slide['elements'].append({'id':'existing-number-4','kind':'text','text':'4','box':box,
        'style':{'size':25,'color':'17243A'}})
    issue = {'issue_id':'missing-4','priority':'P1','category':'编号缺失',
        'description':'Design徽标中清晰显示数字4。','expected_result':'显示数字4',
        'region':[.08,.18,.08,.1],'confidence':.95}
    candidate, events, _ = execute_tool_calls(p, 'S001', [{
        'tool':'create_element','arguments':{'slide_id':'S001','evidence_issue_id':'missing-4',
            'element':{'kind':'text','text':'4','box':box,
                'style':{'size':25,'color':'FFFFFF'}}}}], mode='restore',
        source_regions=[], visual_issues=[issue])
    assert events[0]['status']=='rejected'
    assert 'existing-number-4' in events[0]['error']
    assert events[0]['result']['duplicate_element']['id']=='existing-number-4'
    assert len(candidate['slides'][0]['elements'])==len(slide['elements'])


def test_missing_text_uses_unique_exact_source_geometry_not_model_estimate(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    source_box = [.075, .526, .388, .028]
    issue = {'issue_id':'missing-line','priority':'P0','category':'关键内容缺失',
        'description':'Design中明确显示“模型量化、蒸馏、结构优化”。',
        'expected_result':'模型量化、蒸馏、结构优化', 'region':[.07,.48,.5,.08], 'confidence':.95}
    source = [{'source_text':'模型量化、蒸馏、结构优化','source_box':source_box,
        'box':source_box,'confidence':.99,'review_required':True}]
    candidate, events, _ = execute_tool_calls(p, 'S001', [{
        'tool':'create_element','arguments':{'slide_id':'S001','evidence_issue_id':'missing-line',
            'element':{'kind':'text','text':'模型量化、蒸馏、结构优化','box':[.09,.49,.44,.03],
                'style':{'size':13}}}}], mode='restore', source_regions=source, visual_issues=[issue])
    created=candidate['slides'][0]['elements'][-1]
    assert events[0]['status']=='applied'
    assert created['box']==source_box
    assert events[0]['result']['placement_adjustment']['requested_box']==[.09,.49,.44,.03]


def test_isolated_style_edits_accept_low_confidence_but_creation_still_needs_source_proof(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    box = [.1, .08, .7, .06]
    issue = {'issue_id':'missing-title','priority':'P0','category':'关键内容缺失',
        'description':'标题不清楚可能是 Fast Inference','region':[.08,.05,.8,.12],'confidence':.6}
    _, events, _ = execute_tool_calls(p, 'S001', [{
        'tool':'create_element','arguments':{'slide_id':'S001','evidence_issue_id':'missing-title',
            'element':{'kind':'text','text':'Fast Inference','box':box,'style':{'size':28}}}}],
        mode='restore',source_regions=[],visual_issues=[issue])
    assert events[0]['status']=='rejected'
    issue['element_ids']=['title']
    candidate,events,_=execute_tool_calls(p,'S001',[{
        'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
            'evidence_issue_id':'missing-title','patch':{'style':{'color':'FFFFFF','size':14},
                'box':[.12,.1,.65,.05]}}}],
        mode='restore',source_regions=[],visual_issues=[issue])
    assert events[0]['status']=='applied'
    assert candidate['slides'][0]['elements'][0]['style']['color']=='FFFFFF'
    assert candidate['slides'][0]['elements'][0]['style']['size']==14


def test_restore_mode_allows_exact_visual_issue_text_correction_with_linked_evidence(tmp_path, monkeypatch):
    p=project(tmp_path,monkeypatch)
    issue={'issue_id':'space-issue','priority':'P1','category':'英文空格缺失',
        'description':'Design显示标题为“Visible title text”。',
        'expected_result':'标题文字应为“Visible title text”','element_ids':['title'],
        'region':[.06,.14,.88,.16],'confidence':.91}
    candidate,events,_=execute_tool_calls(p,'S001',[{
        'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
            'evidence_issue_id':'space-issue','patch':{'text':'Visible title text'}}}],
        mode='restore',source_regions=[],visual_issues=[issue])
    assert events[0]['status']=='applied'
    assert candidate['slides'][0]['elements'][0]['text']=='Visible title text'
    _,rejected,_=execute_tool_calls(p,'S001',[{
        'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
            'evidence_issue_id':'space-issue','patch':{'text':'Invented title'}}}],
        mode='restore',source_regions=[],visual_issues=[issue])
    assert rejected[0]['status']=='rejected'


def test_restore_mode_requires_issue_link_for_all_object_edits(tmp_path, monkeypatch):
    p=project(tmp_path,monkeypatch)
    issue={'issue_id':'title-style','priority':'P2','category':'字号差异',
        'description':'标题字号与Design不同。','element_ids':['title'],
        'region':[.05,.05,.9,.2],'confidence':.9}
    call={'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
        'patch':{'style':{'size':31}}}}
    _,unlinked,_=execute_tool_calls(p,'S001',[call],mode='restore',visual_issues=[issue])
    assert unlinked[0]['status']=='rejected'
    assert '每项编辑都必须关联' in unlinked[0]['error']
    mismatched={'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'body',
        'evidence_issue_id':'title-style','patch':{'style':{'size':31}}}}
    _,wrong_target,_=execute_tool_calls(p,'S001',[mismatched],mode='restore',visual_issues=[issue])
    assert wrong_target[0]['status']=='rejected'
    linked={'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
        'evidence_issue_id':'title-style','patch':{'style':{'size':31}}}}
    candidate,accepted,_=execute_tool_calls(p,'S001',[linked],mode='restore',visual_issues=[issue])
    assert accepted[0]['status']=='applied'
    assert candidate['slides'][0]['elements'][0]['style']['size']==31


def test_model_noop_edit_is_reported_as_no_change(tmp_path, monkeypatch):
    p=project(tmp_path,monkeypatch)
    current=next(e for e in p['slides'][0]['elements'] if e['id']=='title')['style']['size']
    candidate,events,history=execute_tool_calls(p,'S001',[{
        'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
            'patch':{'style':{'size':current}}}}],mode='free')
    assert events[0]['status']=='no_change'
    assert candidate==p
    assert history==[]


def test_restore_mode_allows_evidence_linked_whitespace_only_p2_correction(tmp_path, monkeypatch):
    p=project(tmp_path,monkeypatch)
    p['slides'][0]['elements'][0]['text']='VisibleTitle'
    issue={'issue_id':'p2-space','priority':'P2','category':'中英文空格差异',
        'description':'Design显示 Visible Title，当前文本缺少空格。',
        'expected_result':'Visible Title','element_ids':['title'],'confidence':.7}
    candidate,events,_=execute_tool_calls(p,'S001',[{
        'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
            'evidence_issue_id':'p2-space','patch':{'text':'Visible Title'}}}],
        mode='restore',source_regions=[],visual_issues=[issue])
    assert events[0]['status']=='applied'
    assert candidate['slides'][0]['elements'][0]['text']=='Visible Title'
    _,rejected,_=execute_tool_calls(p,'S001',[{
        'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
            'evidence_issue_id':'p2-space','patch':{'text':'New title'}}}],
        mode='restore',source_regions=[],visual_issues=[issue])
    assert rejected[0]['status']=='rejected'


def test_text_edit_refreshes_line_layout_so_added_spaces_move_following_run(tmp_path, monkeypatch):
    from studio.text_layout import layout_elements
    p=project(tmp_path,monkeypatch);slide=p['slides'][0]
    box=[.1,.2,.5,.05]
    elements=[
        {'id':'run-a','kind':'text','text':'Visible','box':box,'source_box':box,'ocr_box':box,
            'style':{'font':'Arial','size':18,'color':'000000'},'line_id':'line-x','run_id':'line-x:run:0',
            'block_id':'block-x','run_order':0,'text_class':'latin','source_text':'VisibleTitle','layout_managed':True},
        {'id':'run-b','kind':'text','text':'Title','box':box,'source_box':box,'ocr_box':box,
            'style':{'font':'Arial','size':18,'color':'000000'},'line_id':'line-x','run_id':'line-x:run:7',
            'block_id':'block-x','run_order':1,'text_class':'latin','source_text':'VisibleTitle','layout_managed':True},
    ]
    slide['elements'],slide['text_structure']=layout_elements(elements,p['design'],slide['id'])
    old_x=next(e for e in slide['elements'] if e['id']=='run-b')['box'][0]
    issue={'issue_id':'spacing','priority':'P1','category':'空格缺失','expected_result':'Visible Title',
        'element_ids':['run-a'],'confidence':.95}
    candidate,events,_=execute_tool_calls(p,'S001',[{'tool':'edit_element','arguments':{
        'slide_id':'S001','element_id':'run-a','evidence_issue_id':'spacing','patch':{'text':'Visible '}}}],
        mode='restore',source_regions=[],visual_issues=[issue])
    new=next(e for e in candidate['slides'][0]['elements'] if e['id']=='run-b')
    first=next(e for e in candidate['slides'][0]['elements'] if e['id']=='run-a')
    assert events[0]['status']=='applied'
    assert first['text_layout']['text']=='Visible '
    assert new['box'][0]>old_x
    assert new['text_layout']['line_baseline_pt']==first['text_layout']['line_baseline_pt']


def test_full_line_correction_is_distributed_over_existing_ocr_runs(tmp_path, monkeypatch):
    from studio.text_layout import layout_elements

    p=project(tmp_path,monkeypatch)
    slide=p['slides'][0]
    elements=[
        {'id':'ocr-title-latin','kind':'text','text':'1LLM ','source_text':'1LLM 工作流程',
         'box':[.2,.2,.2,.05],'ocr_box':[.2,.2,.2,.05],'source_box':[.2,.2,.5,.05],
         'style':{'font':'Arial','size':18,'color':'FFFFFF'},'line_id':'line-title',
         'block_id':'block-title','run_id':'line-title:run:0','run_order':0,'text_class':'latin','layout_managed':True},
        {'id':'ocr-title-cjk','kind':'text','text':'工作流程','source_text':'1LLM 工作流程',
         'box':[.4,.2,.3,.05],'ocr_box':[.2,.2,.5,.05],'source_box':[.2,.2,.5,.05],
         'style':{'font':'Arial','size':18,'color':'FFFFFF'},'line_id':'line-title',
         'block_id':'block-title','run_id':'line-title:run:5','run_order':5,'text_class':'cjk','layout_managed':True},
    ]
    slide['elements'],slide['text_structure']=layout_elements(elements,p['design'],slide['id'])

    candidate,events,_=execute_tool_calls(p,'S001',[{'tool':'edit_element','arguments':{
        'slide_id':'S001','element_id':'ocr-title-latin','patch':{'text':'LLM 工作流程'}}}],mode='free')

    assert events[0]['status']=='applied'
    assert events[0]['result']['full_line_edit_distributed'] is True
    edited=candidate['slides'][0]['elements']
    line=[e for e in edited if e.get('line_id')=='line-title']
    assert ''.join(e['text'] for e in sorted(line,key=lambda e:e.get('run_order',0)))=='LLM 工作流程'
    assert [e['text'] for e in sorted(line,key=lambda e:e.get('run_order',0))]==['LLM ','工作流程']
    _,repeat,_=execute_tool_calls(candidate,'S001',[{'tool':'edit_element','arguments':{
        'slide_id':'S001','element_id':'ocr-title-latin','patch':{'text':'LLM 工作流程'}}}],mode='free')
    assert repeat[0]['status']=='no_change'
    assert repeat[0]['result']['current_line_text']=='LLM 工作流程'


def test_batch_edit_is_atomic_and_honors_layout_locks(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    p['slides'][0]['elements'][0]['layout_locks'] = {'x': True}
    before = deepcopy(p['slides'][0]['elements'])
    candidate, events, _ = execute_tool_calls(p, 'S001', [{
        'tool': 'batch_edit', 'arguments': {'slide_id': 'S001', 'edits': [
            {'element_id': 'body', 'patch': {'style': {'size': 20}}},
            {'element_id': 'title', 'patch': {'box': [.2, .1, .7, .1]}}]}}], mode='free')
    assert events[0]['status'] == 'rejected'
    assert candidate['slides'][0]['elements'] == before


def test_undo_restores_last_tool_mutation(tmp_path, monkeypatch):
    p = project(tmp_path, monkeypatch)
    candidate, events, history = execute_tool_calls(p, 'S001', [
        {'tool': 'edit_element', 'arguments': {'slide_id': 'S001', 'element_id': 'title', 'patch': {'style': {'size': 31}}}},
        {'tool': 'undo_edit', 'arguments': {'slide_id': 'S001'}}], mode='free')
    assert [e['status'] for e in events] == ['applied', 'applied']
    assert candidate['slides'][0]['elements'][0] == p['slides'][0]['elements'][0]
    assert history == []
