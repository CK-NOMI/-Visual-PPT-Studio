"""Exercise the real route and editing tools with a fully offline provider."""
from copy import deepcopy
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from studio import core, server, api_limits, api_usage


@pytest.fixture
def page(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path)
    monkeypatch.setattr(server,'DATA',tmp_path)
    monkeypatch.setattr(api_usage,'_usage_dir',lambda:tmp_path/'usage')
    monkeypatch.setattr(server,'_visual_agent_runs',{})
    project=core.create_project('offline cost check','test',1)
    root=core.project_dir(project['id'])
    Image.new('RGB',(64,36),'white').save(root/'images'/'S001_design.png')
    sl=project['slides'][0];sl['images']['design']='S001_design.png'
    sl['elements']=[{'id':i,'kind':'text','text':i,'box':[.1,.1+.2*n,.4,.1],
                     'style':{'size':18,'color':'FFFFFF'}} for n,i in enumerate(('a','b'))]
    # Stage 02 supplies native objects already processed by the layout engine.
    from studio.text_layout import layout_elements
    sl['elements'],_=layout_elements(sl['elements'],project.get('design'),'S001')
    project['locked']=True;core.safe_json_write(root/'project.json',project)
    worker=type('Worker',(),{'active_run_session':None})()
    monkeypatch.setattr(server,'_get_stage_preview_worker',lambda *a:worker)
    monkeypatch.setattr(server.rc,'get_regions',lambda *a:{'regions':[],'source_valid':True})
    def render(worker,candidate,label):
        session=worker.active_run_session
        Image.new('RGB',(64,36),'white').save(session/(label+'.png'))
        return {'chromium':label+'.png','preview_sha256':label}
    monkeypatch.setattr(server,'_render_visual_agent_browser_preview',render)
    def final(source,candidate,sid,session,label):
        Image.new('RGB',(64,36),'white').save(session/'final.png')
        return {'chromium':'final.png','libreoffice':'final.png',
            'browser_qa':{'layout_contract_pass':True,'pptx_pass':True,'workflow_pass':True},
            'pptx_report':{'native_text':{'ok':True},'saved_layout_check':{'ok':True}}}
    monkeypatch.setattr(server,'_render_visual_agent_candidate',final)
    return project,TestClient(server.app),root


def edit(target):
    return {'tool':'edit_element','arguments':{'slide_id':'S001','element_id':target,
        'patch':{'style':{'size':24}}}}


def accounted(phase,sl,issue):
    messages=[{'role':'user','content':json.dumps([phase,issue,sl['elements']])}]
    api_limits.before_request(messages)
    api_usage.record_request(model='deepseek-flash',messages=messages,response={'usage':{
        'prompt_tokens':100,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100,
        'completion_tokens':10,'total_tokens':110}})


def test_budget_pause_keeps_accepted_edit_reverts_unverified_and_resumes(page,monkeypatch):
    project,client,root=page
    calls=[]
    issues=[{'issue_id':i,'priority':'P1','description':'size '+i,'element_ids':[i],
             'expected_result':'24pt','status':'open','region':[.1,.1,.4,.1]} for i in ('a','b')]
    async def provider(current,sl,design,render,*,phase,**kwargs):
        issue=(kwargs.get('tool_feedback') or {}).get('current_issue') or {}
        accounted(phase,sl,issue.get('issue_id'))
        calls.append((phase,issue.get('issue_id')))
        if phase=='reflect':
            return {'decision':'accept','issues':[{'issue_id':issue['issue_id'],'status':'fixed'}]}
        if not issue:
            unfinished=[deepcopy(i) for i in issues if next(e for e in sl['elements'] if e['id']==i['issue_id'])['style']['size']!=24]
            return {'diagnosis':{'issues':unfinished},'tool_calls':[edit(unfinished[0]['issue_id'])] if unfinished else [],'decision':'act' if unfinished else 'done'}
        return {'diagnosis':{'issues':[]},'tool_calls':[edit(issue['issue_id'])],'decision':'act'}
    monkeypatch.setattr(server,'visual_agent_turn',provider)
    url=f'/api/projects/{project["id"]}/visual-agent/run'
    result=client.post(url,json={'slide_id':'S001','api_key':'mock-only','request_budget':3}).json()
    assert result['status']=='requires_review'
    assert result['can_continue'] is True
    assert result['cost_control']['requests']==3
    assert result['cost_control']['paused_reason']
    candidate=json.loads((root/'build/visual_agent'/result['token']/'candidate_project.json').read_text())
    sizes={e['id']:e['style']['size'] for e in candidate['slides'][0]['elements']}
    assert sizes=={'a':24,'b':18}  # Unreviewed b must not leak into the saved candidate.
    assert core.read_project(project['id'])['slides'][0]['elements'][0]['style']['size']==18
    resumed=client.post(url,json={'slide_id':'S001','api_key':'mock-only',
        'base_candidate_token':result['token'],'resume_from_candidate':True,'request_budget':6})
    assert resumed.status_code==200,resumed.text
    out=resumed.json()
    assert out['status']=='candidate_ready',out['reason']
    candidate=json.loads((root/'build/visual_agent'/out['token']/'candidate_project.json').read_text())
    assert {e['style']['size'] for e in candidate['slides'][0]['elements']}=={24}
    assert out['remaining_issue_count']==0
    assert calls.count(('reflect','a'))==1


def test_http_402_stops_entire_run_after_first_failure(page,monkeypatch):
    project,client,root=page
    calls=[]
    async def provider(current,sl,design,render,**kwargs):
        accounted('diagnose',sl,None)
        calls.append(1)
        api_usage.record_request(model='deepseek-flash',messages=[],status='failure',http_status=402)
        raise RuntimeError('HTTP 402')
    monkeypatch.setattr(server,'visual_agent_turn',provider)
    response=client.post(f'/api/projects/{project["id"]}/visual-agent/run',
        json={'slide_id':'S001','api_key':'mock-only'})
    assert response.status_code==200,response.text
    out=response.json()
    assert calls==[1]
    assert '402' in out['cost_control']['paused_reason']
    assert out['status']=='requires_review'


def test_final_visual_audit_cannot_be_skipped_by_budget_and_can_resume(page,monkeypatch):
    project,client,root=page
    calls=[]
    async def provider(current,sl,design,render,*,phase,**kwargs):
        issue=(kwargs.get('tool_feedback') or {}).get('current_issue') or {}
        accounted(phase,sl,issue.get('issue_id'))
        calls.append(phase)
        if phase=='reflect':
            return {'decision':'accept','issues':[{'issue_id':'a','status':'fixed'}]}
        if sl['elements'][0]['style']['size']==24:
            return {'diagnosis':{'issues':[]},'tool_calls':[],'decision':'done'}
        return {'diagnosis':{'issues':[{'issue_id':'a','priority':'P1','description':'size',
            'element_ids':['a'],'expected_result':'24pt','status':'open'}]},'tool_calls':[edit('a')]}
    monkeypatch.setattr(server,'visual_agent_turn',provider)
    url=f'/api/projects/{project["id"]}/visual-agent/run'
    first=client.post(url,json={'slide_id':'S001','api_key':'mock','request_budget':2}).json()
    assert first['status']=='requires_review'
    assert first['remaining_issue_count']==0
    assert first['final_visual_check_passed'] is False and first['can_continue'] is True
    resumed=client.post(url,json={'slide_id':'S001','api_key':'mock','request_budget':2,
        'base_candidate_token':first['token'],'resume_from_candidate':True})
    assert resumed.status_code==200,resumed.text
    assert resumed.json()['status']=='candidate_ready'
    assert resumed.json()['final_visual_check_passed'] is True
    assert calls==['diagnose','reflect','diagnose']


def test_independent_queued_plan_saves_a_diagnosis_but_keeps_both_visual_reviews(page,monkeypatch):
    project,client,root=page
    phases=[]
    issues=[{'issue_id':i,'priority':'P1','description':'size '+i,'element_ids':[i],
        'expected_result':'24pt','status':'open'} for i in ('a','b')]
    async def provider(current,sl,design,render,*,phase,**kwargs):
        issue=(kwargs.get('tool_feedback') or {}).get('current_issue') or {}
        accounted(phase,sl,issue.get('issue_id'));phases.append((phase,issue.get('issue_id')))
        if phase=='reflect':
            return {'decision':'accept','issues':[{'issue_id':issue['issue_id'],'status':'fixed'}]}
        if len(phases)==1:
            return {'diagnosis':{'issues':issues},'tool_calls':[edit('a')],
                'repair_plans':[{'issue_id':'b','tool_calls':[edit('b')]}]}
        return {'diagnosis':{'issues':[]},'tool_calls':[],'decision':'done'}
    monkeypatch.setattr(server,'visual_agent_turn',provider)
    response=client.post(f'/api/projects/{project["id"]}/visual-agent/run',
        json={'slide_id':'S001','api_key':'mock','request_budget':4})
    assert response.status_code==200,response.text
    out=response.json()
    assert out['status']=='candidate_ready',out['reason']
    assert out['queued_plan_hits']==1
    assert phases==[('diagnose',None),('reflect','a'),('reflect','b'),('diagnose',None)]


def test_no_change_gets_one_focused_retry_then_preserves_unresolved_issue(page,monkeypatch):
    project,client,root=page
    calls=[]
    async def provider(current,sl,design,render,*,phase,**kwargs):
        issue=(kwargs.get('tool_feedback') or {}).get('current_issue')
        accounted(phase,sl,'a')
        calls.append(1)
        return {'diagnosis':{'issues':[{'issue_id':'a','priority':'P1','description':'spacing',
            'element_ids':['a'],'expected_result':'visual spacing','status':'open'}]},
            'tool_calls':[{'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'a',
                'patch':{'style':{'size':18}}}}]}
    monkeypatch.setattr(server,'visual_agent_turn',provider)
    response=client.post(f'/api/projects/{project["id"]}/visual-agent/run',
        json={'slide_id':'S001','api_key':'mock-only'})
    assert response.status_code==200,response.text
    out=response.json()
    assert len(calls)==2
    assert out['issues'][0]['status']=='待复核'
    assert out['remaining_issue_count']==1 and out['can_continue']
    assert '连续两次' in out['issues'][0]['skip_reason']
