from copy import deepcopy
import json
import pytest
from studio import repair_pipeline as rp

TITLE='Fast Inference from Transformers via Speculative Decoding'

def fixture(monkeypatch,tmp_path):
    p={'id':'sample','revision':5,'design':{'font':'Noto Sans','bg':'FFFFFF','ink':'17243A'},'slides':[{'id':'S001','title':TITLE,'body':'','background':'FFFFFF','images':{'design':'S001_design.png','clean':'S001_clean.png'},'elements':[]}]}
    folder=tmp_path/'sample';(folder/'images').mkdir(parents=True)
    monkeypatch.setattr(rp.core,'project_dir',lambda pid:folder)
    from PIL import Image
    from studio import pptx_native
    monkeypatch.setattr(pptx_native,'project_dir',lambda pid:folder)
    monkeypatch.setattr(rp.rc,'project_dir',lambda pid:folder)
    for name in ['S001_design.png','S001_clean.png']:Image.new('RGB',(960,540),'white').save(folder/'images'/name)
    monkeypatch.setattr(rp.core,'read_project',lambda pid:deepcopy(p))
    rows=[{'id':'ocr_title','text':TITLE,'box':[.04,.02,.90,.08],'ocr_box':[.04,.02,.90,.08],'confidence':.998,'style':{'font':'Noto Sans','size':28,'bold':True},'line_id':'title'}]
    monkeypatch.setattr(rp.rc,'get_regions',lambda *args:{'source_valid':True,'source_sha256':'abc','regions':deepcopy(rows)})
    monkeypatch.setattr(rp.rc,'available_ocr',lambda:{'rapidocr':False})
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':True})
    return p,folder,rows

def test_missing_title_recovery_is_isolated_and_native(monkeypatch,tmp_path):
    p,folder,_=fixture(monkeypatch,tmp_path);original=deepcopy(p)
    result=rp.create(p)
    assert p==original and result['ai_calls']==0
    assert len(result['actions'])==1 and result['pptx_check']['ok']
    candidate=json.loads((folder/'build/repairs'/result['token']/'candidate_project.json').read_text())
    assert ''.join(e['text'] for e in candidate['slides'][0]['elements'])==TITLE
    assert all(e['text_layout']['version']==2 for e in candidate['slides'][0]['elements'])
    assert (folder/'build/repairs'/result['token']/'original_project.json').is_file()

def test_existing_title_does_not_duplicate(monkeypatch,tmp_path):
    p,_,rows=fixture(monkeypatch,tmp_path)
    p['slides'][0]['elements']=[{'id':'title','kind':'text','text':TITLE,'box':rows[0]['box']}]
    assert rp.inspect(p)['issue_count']==0

def test_high_confidence_badge_digits_use_design_and_clean_evidence(monkeypatch,tmp_path):
    p,_,_=fixture(monkeypatch,tmp_path)
    regions=[
        {'id':'badge_1','text':'1','box':[.05,.20,.05,.07],'ocr_box':[.065,.22,.018,.04],
         'source_box':[.065,.22,.018,.04],'confidence':.9998,'role':'ignore','style':{'font':'Noto Sans','size':26},
         'font_size_evidence':{'estimate_pt':26},'line_id':'badge_1'},
        {'id':'badge_3','text':'3','box':[.05,.60,.05,.07],'ocr_box':[.065,.62,.018,.04],
         'source_box':[.065,.62,.018,.04],'confidence':.9998,'role':'custom','style':{'font':'Noto Sans','size':27},
         'font_size_evidence':{'estimate_pt':27},'line_id':'badge_3'},
    ]
    p['slides'][0]['elements']=[{'id':'number_3','kind':'text','text':'3','box':[.05,.60,.05,.07],
        'ocr_box':[.065,.62,.018,.04],'source_box':[.065,.62,.018,.04],
        'style':{'font':'Noto Sans','size':27,'color':'FFFFFF'}}]
    monkeypatch.setattr(rp.rc,'get_regions',lambda *args:{'source_valid':True,'source_sha256':'abc','regions':deepcopy(regions)})
    monkeypatch.setattr(rp.rc,'available_ocr',lambda:{'rapidocr':False})
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':True,'remaining_ink_ratio':0,'removed_ink_ratio':1})
    monkeypatch.setattr(rp,'_sample_source_ink_color',lambda *args:'FFFFFF')
    monkeypatch.setattr(rp,'_sample_source_ink_extent',lambda _p,_s,box:{'height_px':27 if box[1]<.5 else 28,'width_px':18,'ink_pixels':220})
    page=rp.inspect(p,['S001'])['pages'][0]
    issue=next(i for i in page['issues'] if i.get('text')=='1')
    assert issue['suggested_action']=='create_missing_text'
    assert issue['evidence']['origin']=='design_first_numeric_content_check'
    assert issue['evidence']['peer_element']=='number_3'
    assert issue['evidence']['chosen_size_pt']==27
    assert page['content_completeness']['unmatched']==1

def test_badge_digit_without_clean_removal_proof_stays_review(monkeypatch,tmp_path):
    p,_,_=fixture(monkeypatch,tmp_path)
    regions=[
        {'id':'badge_1','text':'1','box':[.05,.20,.05,.07],'ocr_box':[.065,.22,.018,.04],
         'source_box':[.065,.22,.018,.04],'confidence':.9998,'role':'ignore','style':{'font':'Noto Sans','size':26},
         'font_size_evidence':{'estimate_pt':26},'line_id':'badge_1'},
        {'id':'badge_3','text':'3','box':[.05,.60,.05,.07],'ocr_box':[.065,.62,.018,.04],
         'source_box':[.065,.62,.018,.04],'confidence':.9998,'role':'custom','style':{'font':'Noto Sans','size':27},
         'font_size_evidence':{'estimate_pt':27},'line_id':'badge_3'},
    ]
    p['slides'][0]['elements']=[{'id':'number_3','kind':'text','text':'3','box':[.05,.60,.05,.07],
        'ocr_box':[.065,.62,.018,.04],'source_box':[.065,.62,.018,.04],
        'style':{'font':'Noto Sans','size':27,'color':'FFFFFF'}}]
    monkeypatch.setattr(rp.rc,'get_regions',lambda *args:{'source_valid':True,'source_sha256':'abc','regions':deepcopy(regions)})
    monkeypatch.setattr(rp.rc,'available_ocr',lambda:{'rapidocr':False})
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':False,'reason':'residual glyph'})
    monkeypatch.setattr(rp,'_sample_source_ink_color',lambda *args:'FFFFFF')
    monkeypatch.setattr(rp,'_sample_source_ink_extent',lambda *args:{'height_px':27,'width_px':18,'ink_pixels':220})
    page=rp.inspect(p,['S001'])['pages'][0]
    issue=next(i for i in page['issues'] if i.get('text')=='1')
    assert issue['suggested_action']=='review'

def test_ai_missing_text_becomes_candidate_only_with_independent_evidence(monkeypatch,tmp_path):
    p,_,rows=fixture(monkeypatch,tmp_path)
    rows.clear()  # Simulate OCR missing the title while the locked plan still has it.
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':True,'remaining_ink_ratio':0})
    proposal={'text':TITLE,'box':[.04,.02,.90,.08],'role':'title','confidence':.98,'evidence':'clear title in design'}
    issues=rp._ai_missing_issues(p,p['slides'][0],[proposal],[])
    assert len(issues)==1 and issues[0]['suggested_action']=='create_native_text'
    assert issues[0]['evidence']['plan_text_match'] and issues[0]['ai_proposal']

def test_exact_high_confidence_ocr_can_confirm_ai_proposal_below_model_score(monkeypatch,tmp_path):
    p,_,_=fixture(monkeypatch,tmp_path)
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':True,'remaining_ink_ratio':0})
    proposal={'text':TITLE,'box':[.04,.02,.90,.08],'role':'title','confidence':.90,'evidence':'visible'}
    issue=rp._ai_missing_issues(p,p['slides'][0],[proposal],[])[0]
    assert issue['suggested_action']=='create_native_text'
    assert issue['confidence']>=.97

def test_visual_confirmation_can_resolve_conflicted_badge_digit_using_peer_style(monkeypatch,tmp_path):
    p,_,_=fixture(monkeypatch,tmp_path)
    rows=[
        {'id':'badge_1','text':'1','box':[.065,.22,.018,.04],'ocr_box':[.065,.22,.018,.04],
         'source_box':[.05,.20,.05,.07],'confidence':.9991,'review_reason':'OCR冲突：T','style':{'font':'Noto Sans','size':26}},
        {'id':'badge_2','text':'2','box':[.565,.22,.018,.04],'ocr_box':[.565,.22,.018,.04],
         'source_box':[.55,.20,.05,.07],'confidence':.9998,'style':{'font':'Noto Sans','size':25}},
    ]
    p['slides'][0]['elements']=[{'id':'number_2','kind':'text','text':'2','box':[.55,.20,.05,.07],
        'ocr_box':[.565,.22,.018,.04],'source_box':[.565,.22,.018,.04],
        'style':{'font':'Noto Sans','size':25,'color':'FFFFFF'}},
        {'id':'diagram_one','kind':'text','text':'1次小模型调用','box':[.4,.8,.3,.04]}]
    monkeypatch.setattr(rp.rc,'get_regions',lambda *args:{'source_valid':True,'source_sha256':'abc','regions':deepcopy(rows)})
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':True,'remaining_ink_ratio':0,'removed_ink_ratio':1})
    monkeypatch.setattr(rp,'_sample_source_ink_color',lambda *args:'FFFFFF')
    monkeypatch.setattr(rp,'_sample_source_ink_extent',lambda _p,_s,box:{'height_px':26 if box[0]<.5 else 25,'width_px':18,'ink_pixels':220})
    issue=rp._ai_missing_issues(p,p['slides'][0],[{'text':'1','box':[.05,.20,.05,.07],
        'role':'label','confidence':.99,'visual_confirmed':True,'evidence':'设计稿圆标有1，渲染图为空'}],[])[0]
    assert issue['suggested_action']=='create_missing_text'
    assert issue['evidence']['visual_confirmed'] is True
    assert issue['source_rows'][0]['style']['size']==25
    assert issue['source_rows'][0]['box']==[.065,.22,.018,.04]
    assert issue['source_rows'][0]['source_box']==[.065,.22,.018,.04]
    result=rp.create(p,slides=['S001'],ai_issues={'S001':[{'text':'1','box':[.05,.20,.05,.07],
        'role':'label','confidence':.99,'visual_confirmed':True,'evidence':'设计稿圆标有1，渲染图为空'}]},ai_calls=1,max_actions=1)
    assert len(result['actions'])==1
    new_element=result['actions'][0]['parameters'][0]
    assert new_element['box'][0]==pytest.approx(.065)
    assert new_element['box'][2]==pytest.approx(.018)
    assert new_element['box'][3]==pytest.approx(.04)

def test_ai_duplicate_focus_and_full_slide_proposals_are_deduplicated(monkeypatch,tmp_path):
    p,_,_=fixture(monkeypatch,tmp_path)
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':True})
    a={'text':TITLE,'box':[.04,.02,.9,.08],'role':'title','confidence':.98,'evidence':'full slide'}
    b={**a,'box':[.041,.021,.899,.079],'evidence':'focus crop'}
    issues=rp._ai_missing_issues(p,p['slides'][0],[a,b],[])
    assert len(issues)==1

def test_model_cannot_auto_create_digit_without_visual_confirmation(monkeypatch,tmp_path):
    p,_,_=fixture(monkeypatch,tmp_path)
    rows=[{'id':'badge_1','text':'1','box':[.05,.20,.05,.07],'ocr_box':[.065,.22,.018,.04],
           'source_box':[.05,.20,.05,.07],'confidence':.9991,'style':{'font':'Noto Sans','size':26}}]
    monkeypatch.setattr(rp.rc,'get_regions',lambda *args:{'source_valid':True,'source_sha256':'abc','regions':deepcopy(rows)})
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':True})
    issue=rp._ai_missing_issues(p,p['slides'][0],[{'text':'1','box':[.065,.22,.018,.04],
        'role':'label','confidence':.99,'evidence':'digit'}],[])[0]
    assert issue['suggested_action']=='review'

def test_smart_repair_limits_each_candidate_to_highest_priority_action(monkeypatch,tmp_path):
    p,_,rows=fixture(monkeypatch,tmp_path)
    p['slides'][0]['body']='Missing body line'
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':True,'remaining_ink_ratio':0})
    result=rp.create(p,ai_issues={'S001':[{'text':'Missing body line','box':[.1,.3,.35,.06],
        'role':'body','confidence':.99,'evidence':'visible in design'}]},ai_calls=1,max_actions=1)
    assert len(result['actions'])==1
    assert result['actions'][0]['text']==TITLE
    assert result['diagnosis']['deferred_action_count']==1
    assert len(result['diagnosis']['repair_queue'])==2

def test_ai_suggestion_does_not_duplicate_deterministic_recovery(monkeypatch,tmp_path):
    p,_,_=fixture(monkeypatch,tmp_path)
    result=rp.create(p,ai_issues={'S001':[{'text':TITLE,'box':[.04,.02,.90,.08],
        'role':'title','confidence':.99,'evidence':'visible'}]},ai_calls=1)
    assert len(result['actions'])==1
    assert result['actions'][0]['text']==TITLE

@pytest.mark.parametrize('mutate',[
    lambda p,e: p['slides'][0].update(title='Unrelated planned title'),
    lambda p,e: p['slides'][0]['elements'].append({'id':'nearby','kind':'text','text':'Near object','box':[.04,.02,.9,.08]}),
])
def test_ai_missing_text_uncertain_or_overlapping_stays_review(monkeypatch,tmp_path,mutate):
    p,_,rows=fixture(monkeypatch,tmp_path);rows.clear()
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':True})
    mutate(p,None)
    proposal={'text':TITLE,'box':[.04,.02,.90,.08],'role':'title','confidence':.99,'evidence':'model guess'}
    issues=rp._ai_missing_issues(p,p['slides'][0],[proposal],[])
    assert len(issues)==1 and issues[0]['suggested_action']=='review'

def test_ai_missing_text_clean_residual_prevents_candidate(monkeypatch,tmp_path):
    p,_,rows=fixture(monkeypatch,tmp_path);rows.clear()
    monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':False,'reason':'residual text'})
    proposal={'text':TITLE,'box':[.04,.02,.90,.08],'role':'title','confidence':.99,'evidence':'model guess'}
    issues=rp._ai_missing_issues(p,p['slides'][0],[proposal],[])
    assert issues[0]['suggested_action']=='review'

@pytest.mark.parametrize('reason',['unknown','low_confidence','residual','stale_ocr'])
def test_uncertain_evidence_never_creates(monkeypatch,tmp_path,reason):
    p,_,rows=fixture(monkeypatch,tmp_path)
    if reason=='unknown':p['slides'][0]['title']='Different title'
    if reason=='low_confidence':rows[0]['confidence']=.6
    if reason=='residual':monkeypatch.setattr(rp,'clean_evidence',lambda *args:{'safe':False})
    if reason=='stale_ocr':monkeypatch.setattr(rp.rc,'get_regions',lambda *args:{'source_valid':False})
    result=rp.inspect(p)
    assert result['issue_count'] and not any(i['suggested_action']=='create_native_text' for page in result['pages'] for i in page['issues'])

@pytest.mark.parametrize('reason',['unvalidated','stale','tampered'])
def test_commit_rejects_unverified_stale_or_tampered_candidate(monkeypatch,tmp_path,reason):
    p,folder,_=fixture(monkeypatch,tmp_path);result=rp.create(p);path=folder/'build/repairs'/result['token']
    if reason!='unvalidated':rp.update_status(path,status='validated_pending_commit')
    if reason=='stale':p['revision']+=1
    if reason=='tampered':
        candidate=json.loads((path/'candidate_project.json').read_text());candidate['slides'][0]['title']='tampered';rp.core.safe_json_write(path/'candidate_project.json',candidate)
    monkeypatch.setattr(rp.core,'save_project',lambda p:pytest.fail('must not write'))
    with pytest.raises(ValueError):rp.commit(p['id'],result['token'])

def test_explicit_commit_keeps_backup(monkeypatch,tmp_path):
    p,folder,_=fixture(monkeypatch,tmp_path);result=rp.create(p);path=folder/'build/repairs'/result['token']
    rp.update_status(path,status='validated_pending_commit');saved=[]
    monkeypatch.setattr(rp.core,'save_project',lambda candidate:saved.append(deepcopy(candidate)))
    state=rp.commit(p['id'],result['token'])
    assert state['committed'] and len(saved)==1 and (path/'precommit_backup.json').is_file()

@pytest.mark.parametrize('improves',[True,False])
def test_browser_candidate_acceptance_requires_local_improvement(monkeypatch,tmp_path,improves):
    from PIL import Image
    p,folder,_=fixture(monkeypatch,tmp_path);result=rp.create(p);path=folder/'build/repairs'/result['token']
    objects=[{'id':id_,'final_text':TITLE,'dom':{'text':TITLE,'ink_box':{'width':100}},'anomalies':[]} for id_ in result['actions'][0]['new_ids']]
    for mode,color in [('before','gray'),('after','white' if improves else 'black')]:
        (path/mode/'validation').mkdir(parents=True)
        Image.new('RGB',(960,540),color).save(path/mode/'validation/S001_aligned.png')
    base={'pptx_pass':True,'workflow_pass':True,'layout_contract_pass':True,'visual_status':'requires_review'}
    def report(rows):return {**base,'pages':[{'slide_id':'S001','objects':rows,'failed_objects':0,'review_objects':0,'visual_error_objects':0}]}
    rp._finish_validation(path,{'before':report([]),'after':report(objects)})
    state=rp.get(p['id'],result['token'])
    assert state['status']==('validated_pending_commit' if improves else 'requires_review')
    assert state['comparison']['targeted'][0]['pass']==improves

def test_design_content_gate_requires_visible_dom_ink_for_every_source_row():
    diagnosis={'pages':[{'slide_id':'S001','issues':[{'issue_id':'missing_1','slide_id':'S001','text':'1',
        'source_region':[.1,.1,.02,.03]}],'content_completeness':{'source_valid':True,'design_regions':2,
        'inventory':[{'text':'1','source_region':[.1,.1,.02,.03],'native_status':'missing','matched_elements':[]},
            {'text':'A title','source_region':[.2,.2,.3,.05],'native_status':'matched','matched_elements':['title']} ]}}]}
    after={'pages':[{'slide_id':'S001','objects':[
        {'id':'new_1','final_text':'1','dom':{'text':'1','ink_box':{'width':8}}},
        {'id':'title','final_text':'A title','run_id':'line:x:run:0','dom':{'text':'A title','ink_box':{'width':40}}}]}]}
    actions=[{'issue_id':'missing_1','slide_id':'S001','text':'1','new_ids':['new_1']}]
    result=rp._apply_rendered_content_coverage(diagnosis,after,actions,[{'issue_id':'missing_1','pass':True}])
    assert result['status']=='complete'
    assert diagnosis['pages'][0]['content_completeness']['render_visible']==2
    after['pages'][0]['objects'][0]['dom']['ink_box']['width']=0
    second=rp._apply_rendered_content_coverage(diagnosis,after,actions,[{'issue_id':'missing_1','pass':False}])
    assert second['status']=='incomplete_or_review'


def test_repeat_validation_archives_previous_output_and_reuses_versioned_before(monkeypatch,tmp_path):
    from pathlib import Path
    from studio.browser_qa import DIAGNOSTICS_REVISION
    p,folder,_=fixture(monkeypatch,tmp_path);result=rp.create(p);path=folder/'build/repairs'/result['token'];calls=[]
    def fake_run(cmd,**kwargs):
        output=Path(cmd[cmd.index('--output')+1]);(output/'workspace').mkdir(parents=True)
        rp.core.safe_json_write(output/'report.json',{'pages':[{'diagnostics_revision':DIAGNOSTICS_REVISION}]})
        calls.append(output)
    def finish(folder,results,publish=True):
        if not publish:return {'comparison':{'targeted':[{'issue_id':result['actions'][0]['issue_id'],'pass':True}]},'accepted':True}
        rp.update_status(folder,status='validated_pending_commit',comparison={'targeted':[{'issue_id':result['actions'][0]['issue_id'],'pass':True}]},error=None)
    monkeypatch.setattr(rp.subprocess,'run',fake_run);monkeypatch.setattr(rp,'_finish_validation',finish)
    rp._validate(p['id'],path);rp._validate(p['id'],path)
    assert len(calls)==3
    assert list((path/'after').glob('validation_previous_*/workspace'))
    assert rp.get(p['id'],result['token'])['status']=='validated_pending_commit'


def test_trial_does_not_publish_terminal_status_before_rollback_decision(monkeypatch,tmp_path):
    from PIL import Image
    p,folder,_=fixture(monkeypatch,tmp_path);state=rp.create(p);path=folder/'build/repairs'/state['token']
    objects=[{'id':id_,'final_text':TITLE,'dom':{'text':TITLE,'ink_box':{'width':100}},'anomalies':[]} for id_ in state['actions'][0]['new_ids']]
    for mode,color in [('before','gray'),('after','white')]:
        (path/mode/'validation').mkdir(parents=True);Image.new('RGB',(960,540),color).save(path/mode/'validation/S001_aligned.png')
    def report(rows):return {'pptx_pass':True,'workflow_pass':True,'layout_contract_pass':True,'visual_status':'requires_review',
        'pages':[{'slide_id':'S001','objects':rows,'failed_objects':0,'review_objects':0,'visual_error_objects':0}]}
    rp.update_status(path,status='validating',validation_complete=False)
    result=rp._finish_validation(path,{'before':report([]),'after':report(objects)},publish=False)
    assert result['accepted']
    progress=rp.get(p['id'],state['token'])
    assert progress['status']=='validating' and not progress['validation_complete'] and 'comparison' not in progress
    rp._finish_validation(path,{'before':report([]),'after':report(objects)})
    assert rp.get(p['id'],state['token'])['validation_complete']


def test_validation_recovers_orphan_after_server_restart_without_double_worker(monkeypatch,tmp_path):
    p,folder,_=fixture(monkeypatch,tmp_path);state=rp.create(p);path=folder/'build/repairs'/state['token'];starts=[]
    class Thread:
        def __init__(self,**kwargs):self.kwargs=kwargs
        def start(self):starts.append(self.kwargs)
    monkeypatch.setattr(rp.threading,'Thread',Thread)
    rp.update_status(path,status='validating')
    try:
        rp.validate(p['id'],state['token']);rp.validate(p['id'],state['token'])
        assert len(starts)==1 and starts[0]['target']==rp._validation_worker
    finally:rp._RUNNING.discard((p['id'],state['token']))


def test_one_active_validation_per_project_prevents_parallel_browser_flood(monkeypatch,tmp_path):
    p,folder,_=fixture(monkeypatch,tmp_path);first=rp.create(p);second=rp.create(p)
    class Thread:
        def __init__(self,**kwargs):pass
        def start(self):pass
    monkeypatch.setattr(rp.threading,'Thread',Thread)
    try:
        rp.validate(p['id'],first['token'])
        with pytest.raises(ValueError,match='正在验收'):rp.validate(p['id'],second['token'])
        assert rp.get(p['id'],second['token'])['status']=='candidate_created'
    finally:rp._RUNNING.discard((p['id'],first['token']))


def test_model_repair_intents_bind_to_exact_verified_issues_and_never_supply_geometry():
    page={'issues':[{'issue_id':'line-1','issue_type':'line_geometry','text':'A B',
        'matched_elements':['a','b'],'suggested_action':'repair_line','confidence':1,
        'evidence':{'constraints':['baseline_mismatch']}}]}
    accepted,rejected=rp._bind_ai_repair_intents(page,[
        {'issue_id':'line-1','action':'repair_line_layout','object_ids':['a','b'],'related_object_ids':[],
         'confidence':.95,'evidence':'real baseline conflict','intent':'restore baseline','box':[0,0,1,1]},
        {'issue_id':'invented','action':'repair_line_layout','object_ids':['a'],'confidence':1,'evidence':'guess'},
        {'issue_id':'line-1','action':'repair_duplicate_text','object_ids':['a'],'confidence':.99,'evidence':'guess'}])
    assert len(accepted)==1 and accepted[0]['execution'].startswith('确定性布局引擎')
    assert page['issues'][0]['model_intents'][0]['action']=='repair_line_layout'
    assert len(rejected)==2
    assert all('box' not in x for x in accepted)
