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
