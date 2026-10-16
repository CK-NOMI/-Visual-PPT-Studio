import hashlib
import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from studio import core, server, visual_agent_preview


class _FakePersistentPreview:
    def __init__(self, *args, **kwargs):
        self.session = Path(args[2])
        self.project=args[0]
        self.slide_id=args[1]
    def render(self, candidate, label):
        out=self.session/'iterations'/label;out.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(64,36),'white').save(out/'chromium.png')
        return {'label':label,'chromium':f'iterations/{label}/chromium.png',
            'renderer':'Chromium browser','preview_sha256':hashlib.sha256(label.encode()).hexdigest(),
            'preview_only':True}
    def inspect_element(self,element_id):
        slide=next(s for s in self.project['slides'] if s['id']==self.slide_id)
        element=next(e for e in slide['elements'] if e['id']==element_id)
        return {'id':element_id,'exists':True,'text':element.get('text',''),'visible':True,'box':element.get('box')}
    def close(self): pass


def test_visual_agent_issue_updates_preserve_untouched_findings():
    initial=[
        {'issue_id':'missing-1','priority':'P0','status':'open','description':'number absent'},
        {'issue_id':'space-2','priority':'P1','status':'open','description':'English word spacing'},
    ]
    updated=server._merge_visual_agent_issues(initial,[
        {'issue_id':'missing-1','status':'fixed','confidence':.92},
    ])
    assert len(updated)==2
    assert updated[0]['status']=='fixed' and updated[0]['confidence']==.92
    assert updated[1]==initial[1]


def test_visual_agent_detects_an_exact_edit_repeated_after_acceptance():
    accepted={'round':1,'outcome':'accept','tool_calls':[{'tool':'batch_edit','arguments':{'slide_id':'S002','edits':[
        {'element_id':'token-1','patch':{'text':'生成 Token 1'},'evidence_issue_id':'issue-1'},
        {'element_id':'token-2','patch':{'style':{'size':12}},'evidence_issue_id':'issue-1'},
    ]}}]}
    repeated=[{'tool':'batch_edit','arguments':{'slide_id':'S002','edits':[
        {'element_id':'token-1','patch':{'text':'生成 Token 1'},'evidence_issue_id':'issue-3'},
        {'element_id':'token-2','patch':{'style':{'size':12}},'evidence_issue_id':'issue-3'},
    ]}}]
    assert server._visual_agent_repeated_accepted_edit(repeated,[accepted])=={
        'issue_id':'issue-1','edits':[{'element_id':'token-1','patch':{'text':'生成 Token 1'}},
            {'element_id':'token-2','patch':{'style':{'size':12}}}]}
    changed=[{'tool':'batch_edit','arguments':{'slide_id':'S002','edits':[
        {'element_id':'token-1','patch':{'text':'生成 Token 2'},'evidence_issue_id':'issue-3'},
    ]}}]
    assert server._visual_agent_repeated_accepted_edit(changed,[accepted]) is None


def test_user_can_mark_a_pending_duplicate_issue_fixed_without_editing_project(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path);monkeypatch.setattr(server,'DATA',tmp_path)
    project=core.create_project('Manual issue decision','test',1)
    token='a'*32;folder=core.project_dir(project['id'])/'build'/'visual_agent'/token;folder.mkdir(parents=True)
    issue={'issue_id':'issue-3','status':'待定','ai_reason':'参数与 issue-1 完全重复。','duplicate_of':'issue-1'}
    record={'token':token,'project_id':project['id'],'issues':[issue],'status':'candidate_ready'}
    core.safe_json_write(folder/'status.json',record)
    original=core.read_project(project['id'])
    response=TestClient(server.app).post(
        f'/api/projects/{project["id"]}/visual-agent/{token}/issues/issue-3/decision',json={'status':'fixed'})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['issues'][0]['status']=='已修复'
    assert result['issues'][0]['human_status']=='fixed'
    assert result['issues'][0]['ai_reason']==issue['ai_reason']
    assert core.read_project(project['id'])==original


def test_visual_agent_skips_duplicate_accepted_edit_and_continues_to_next_issue(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path);monkeypatch.setattr(server,'DATA',tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE','1')
    project=core.create_project('Duplicate issue continuation','test',1)
    root=core.project_dir(project['id']);Image.new('RGB',(64,36),'white').save(root/'images'/'S001_design.png')
    slide=project['slides'][0];slide['images']['design']='S001_design.png'
    slide['elements']=[{'id':'heading','kind':'text','text':'Heading','box':[.1,.1,.4,.1],
        'style':{'size':18,'color':'17243A'}}]
    project['locked']=True;core.safe_json_write(root/'project.json',project)
    monkeypatch.setattr(server.rc,'get_regions',lambda *_:{'regions':[],'source_valid':True})
    monkeypatch.setattr(visual_agent_preview,'VisualAgentBrowserPreview',_FakePersistentPreview)
    monkeypatch.setattr(server,'_render_visual_agent_browser_preview',lambda worker,candidate,label:worker.render(candidate,label))
    calls=[]
    def edit(issue_id,patch):
        return {'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'heading',
            'evidence_issue_id':issue_id,'patch':patch}}
    issues=[{'issue_id':'i1','priority':'P1','description':'heading style one','element_ids':['heading'],'status':'open'},
        {'issue_id':'i2','priority':'P2','description':'same heading style repeated','element_ids':['heading'],'status':'open'},
        {'issue_id':'i3','priority':'P3','description':'heading color','element_ids':['heading'],'status':'open'}]
    async def fake_turn(current,sl,design_path,render_path,*,phase,**kwargs):
        feedback=kwargs.get('tool_feedback') or {};current_issue=feedback.get('current_issue') or {};calls.append((phase,current_issue.get('issue_id'),bool(feedback.get('duplicate_issue'))))
        if len(calls)==1:return {'diagnosis':{'issues':issues},'tool_calls':[edit('i1',{'style':{'size':24}})]}
        if phase=='reflect' and current_issue.get('issue_id')=='i1':
            return {'decision':'accept','confidence':.99,'issues':[{'issue_id':'i1','status':'fixed'}]}
        if phase=='diagnose' and feedback.get('duplicate_issue'):
            return {'diagnosis':{'issues':[{'issue_id':'i2','status':'待定','ai_reason':'i2提出的字号和对象与i1已验收修改完全相同，因此跳过重复编辑。'}]},
                'tool_calls':[edit('i3',{'style':{'color':'AABBCC'}})],'reason':'继续i3'}
        if phase=='diagnose' and current_issue.get('issue_id') is None and not any(row[2] for row in calls):
            return {'diagnosis':{'issues':[]},'tool_calls':[edit('i2',{'style':{'size':24}})],'reason':'i2已排队'}
        if phase=='reflect' and current_issue.get('issue_id')=='i3':
            return {'decision':'accept','confidence':.99,'issues':[{'issue_id':'i3','status':'fixed'}]}
        return {'diagnosis':{'issues':[]},'tool_calls':[],'decision':'done','reason':'no more issues'}
    monkeypatch.setattr(server,'visual_agent_turn',fake_turn)
    monkeypatch.setattr(server,'_render_visual_agent_candidate',lambda *_:{
        'browser_qa':{'layout_contract_pass':True,'pptx_pass':True,'workflow_pass':True},
        'pptx_report':{'native_text':{'ok':True},'saved_layout_check':{'ok':True}},
        'libreoffice':'final.png','artifact_sha256':'b'*64})
    response=TestClient(server.app).post(f'/api/projects/{project["id"]}/visual-agent/run',json={
        'slide_id':'S001','mode':'restore','api_key':'test-only','max_attempts_per_issue':2})
    assert response.status_code==200,response.text
    result=response.json();by_id={issue['issue_id']:issue for issue in result['issues']}
    assert by_id['i2']['status']=='待定'
    assert '字号' in by_id['i2']['ai_reason']
    assert by_id['i2']['duplicate_of']=='i1'
    assert by_id['i3']['status'] in {'已修复','fixed'}
    assert result['status']=='candidate_ready'
    assert any(row['outcome']=='duplicate_of_accepted_edit' for row in result['iterations'])
    assert core.read_project(project['id'])['slides'][0]['elements'][0]['style']['size']==18


def test_visual_agent_issue_filter_binds_safe_batch_edits_without_read_roundtrips():
    issue={'issue_id':'p2-spacing','element_ids':['token-1','token-2']}
    rows=[
        {'tool':'get_element','arguments':{'element_id':'token-1'}},
        {'tool':'batch_edit','arguments':{'slide_id':'S002','edits':[
            {'element_id':'token-1','patch':{'text':'Token 1'}},
            {'element_id':'token-2','patch':{'text':'Token 2'}},
        ]}},
        {'tool':'batch_edit','arguments':{'slide_id':'S002','edits':[
            {'element_id':'token-1','patch':{'text':'Token 1'}},
            {'element_id':'unrelated','patch':{'text':'other'}},
        ]}},
    ]
    selected=server._visual_agent_issue_calls(rows,issue)
    assert len(selected)==1
    assert [e['evidence_issue_id'] for e in selected[0]['arguments']['edits']]==['p2-spacing']*2
    assert rows[1]['arguments']['edits'][0].get('evidence_issue_id') is None


def test_visual_agent_missing_text_evidence_requires_source_and_render_agreement():
    project={'slides':[{'id':'S002','elements':[
        {'id':'nearby','kind':'text','text':'标题','box':[.1,.18,.2,.04]},
        {'id':'far_digit','kind':'text','text':'1','box':[.4,.8,.02,.03]},
    ]}]}
    regions=[{'id':'ocr-num-1','text':'1','source_text':'1','source_box':[.049,.194,.01,.025],
        'confidence':.999,'review_required':True,'ocr_conflicts':[{'text':'T'}],
        'style':{'size':17,'color':'FAFCFE','other':'ignored'}}]
    qa={'page':{'unmapped_image_evidence':[{'type':'missing_text','expected_text':'1','actual':'未检测到'},
        {'type':'missing_text','expected_text':'T','actual':'未检测到'}]}}
    evidence=server._visual_agent_missing_text_evidence(project,'S002',regions,qa)
    assert len(evidence)==1
    assert evidence[0]['source_text']=='1'
    assert evidence[0]['source_box']==regions[0]['source_box']
    assert evidence[0]['source_review_required'] is True
    assert evidence[0]['source_conflicts']==[{'text':'T'}]
    assert evidence[0]['source_style']=={'size':17,'color':'FAFCFE'}
    issue={'issue_id':'model-issue','priority':'P1','category':'missing text',
        'description':'Badge number 1 is absent','expected_result':'1','confidence':.6,
        'region':[.04,.18,.06,.06],'element_ids':['nearby']}
    fused=server._bind_visual_missing_evidence([issue],evidence)[0]
    assert fused['issue_id']=='model-issue'
    assert fused['priority']=='P0' and fused['confidence']==.9
    assert fused['category']=='missing_content' and fused['element_ids']==[]
    assert fused['expected_result']=='1'
    assert fused['verified_source_id']=='ocr-num-1'


def test_visual_agent_runs_on_candidate_and_requires_apply_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE', '1')
    project = core.create_project('Visual agent', 'test', 1)
    root = core.project_dir(project['id'])
    Image.new('RGB', (64, 36), 'white').save(root/'images'/'S001_design.png')
    slide = project['slides'][0]
    slide['images']['design'] = 'S001_design.png'
    project['locked'] = True
    core.safe_json_write(root/'project.json', project)
    base = json.loads((root/'project.json').read_text())

    monkeypatch.setattr(server.rc, 'get_regions', lambda *_: {'regions': [], 'source_valid': True})
    monkeypatch.setattr(visual_agent_preview, 'VisualAgentBrowserPreview', _FakePersistentPreview)
    calls = []
    reflection_feedback = []

    async def fake_turn(project, slide, design_path, render_path, *, phase, **kwargs):
        calls.append(phase)
        if phase == 'diagnose':
            return {'diagnosis': {'issues': [{'issue_id': 'i1', 'priority': 'P1', 'description': 'title',
                'region': [.1, .1, .5, .1], 'expected_result': 'Title'}]},
                    'tool_calls': [{'tool': 'edit_element', 'arguments': {'slide_id': 'S001', 'element_id': 'title',
                        'evidence_issue_id':'i1','patch': {'style': {'size': 31}}}}], 'reason': 'visible mismatch'}
        reflection_feedback.extend(kwargs.get('tool_feedback', {}).get('issues', []))
        return {'decision': 'accept','confidence':.95,'issues':[{'issue_id':'i1','status':'fixed','confidence':.95},],
                'tool_calls': [], 'reason': 'render improved'}

    monkeypatch.setattr(server, 'visual_agent_turn', fake_turn)

    def fake_render(source, candidate, slide_id, session, label):
        out = session/'iterations'/label
        out.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (64, 36), 'white').save(out/'libreoffice.png')
        return {'label': label, 'libreoffice': f'iterations/{label}/libreoffice.png',
            'chromium': None, 'artifact_sha256': hashlib.sha256(label.encode()).hexdigest(),
            'browser_qa': {'exit_code': 0, 'layout_contract_pass': True, 'pptx_pass': True,
                           'workflow_pass': True, 'visual_status': 'pass'},
            'pptx_report': {'native_text': {'ok': True}, 'saved_layout_check': {'ok': True}}}

    monkeypatch.setattr(server, '_render_visual_agent_candidate', fake_render)
    def fake_quick_render(worker,candidate,label):
        out=worker.session/'iterations'/label;out.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(64,36),'white').save(out/'chromium.png')
        return {'label':label,'chromium':f'iterations/{label}/chromium.png','renderer':'Chromium browser',
            'preview_sha256':hashlib.sha256(label.encode()).hexdigest(),'preview_only':True}
    monkeypatch.setattr(server, '_render_visual_agent_browser_preview', fake_quick_render)
    client = TestClient(server.app)
    response = client.post(f'/api/projects/{project["id"]}/visual-agent/run', json={
        'slide_id': 'S001', 'mode': 'free', 'instruction': 'Make the title larger',
        'max_iterations': 1, 'api_key': 'test-only','max_attempts_per_issue':10,'confidence_threshold':.85})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'candidate_ready' and result['user_confirmation_required'] is True
    assert calls == ['diagnose', 'reflect', 'diagnose']
    assert reflection_feedback[0]['issue_id'] == 'i1'
    assert core.read_project(project['id']) == base
    candidate = json.loads((root/'build'/'visual_agent'/result['token']/'candidate_project.json').read_text())
    assert next(e for e in candidate['slides'][0]['elements'] if e['id']=='title')['style']['size'] == 31

    def fake_export(pid, mode):
        saved = core.read_project(pid)
        return {'project_revision': saved['revision'], 'filename': 'presentation_editable_r1_test.pptx',
                'artifact_sha256': 'a'*64}
    monkeypatch.setattr(server, 'render_deck', fake_export)
    applied = client.post(f'/api/projects/{project["id"]}/visual-agent/{result["token"]}/apply')
    assert applied.status_code == 200, applied.text
    current = core.read_project(project['id'])
    assert current['revision'] == base['revision'] + 1
    assert next(e for e in current['slides'][0]['elements'] if e['id']=='title')['style']['size'] == 31
    latest=client.get(f'/api/projects/{project["id"]}/visual-agent/latest?slide_id=S001').json()
    assert latest['status']=='applied' and latest['applied_revision']==current['revision']
    undone=client.post(f'/api/projects/{project["id"]}/visual-agent/{result["token"]}/undo')
    assert undone.status_code==200,undone.text
    restored=core.read_project(project['id'])
    assert restored['revision']==base['revision']+2
    assert restored['slides']==base['slides']


def test_visual_agent_allows_different_slides_but_rejects_same_slide_concurrency(tmp_path, monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path);monkeypatch.setattr(server,'DATA',tmp_path)
    monkeypatch.setattr(server,'_visual_agent_runs',{})
    monkeypatch.setenv('PPT_PREVIEW_DISABLE','1')
    project=core.create_project('Parallel slide agents','test',2)
    root=core.project_dir(project['id'])
    for slide in project['slides']:
        filename=f'{slide["id"]}_design.png'
        Image.new('RGB',(64,36),'white').save(root/'images'/filename)
        slide['images']['design']=filename
    project['locked']=True;core.safe_json_write(root/'project.json',project)
    monkeypatch.setattr(server.rc,'get_regions',lambda *_:{'regions':[],'source_valid':True})
    monkeypatch.setattr(visual_agent_preview,'VisualAgentBrowserPreview',_FakePersistentPreview)
    async def fake_turn(*args,**kwargs):
        return {'diagnosis':{'issues':[]},'tool_calls':[],'reason':'no confirmed issue'}
    monkeypatch.setattr(server,'visual_agent_turn',fake_turn)
    active_id='a'*32
    server._visual_agent_runs[active_id]={'project_id':project['id'],'slide_id':'S001','running':True,
        'progress':{'phase':'editing','slide_id':'S001'}}
    client=TestClient(server.app)
    body={'mode':'restore','diagnose_only':True,'api_key':'test-only'}
    same=client.post(f'/api/projects/{project["id"]}/visual-agent/run',json={**body,'slide_id':'S001'})
    assert same.status_code==409
    other=client.post(f'/api/projects/{project["id"]}/visual-agent/run',json={**body,'slide_id':'S002'})
    assert other.status_code==200,other.text
    assert other.json()['slide_id']=='S002'
    assert server._visual_agent_runs[active_id]['running'] is True


def test_visual_agent_serializes_candidate_apply_per_project(monkeypatch):
    pid='parallel-project'
    lock=threading.Lock();lock.acquire()
    monkeypatch.setattr(server,'_visual_agent_apply_locks',{pid:lock})
    response=TestClient(server.app).post('/api/projects/'+pid+'/visual-agent/'+'a'*32+'/apply')
    assert response.status_code==409
    assert '另一页' in response.json()['detail']
    assert lock.locked()


def test_visual_agent_hands_off_misbound_reflection_edits_to_the_matching_issue(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path);monkeypatch.setattr(server,'DATA',tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE','1')
    project=core.create_project('Issue handoff','test',1);root=core.project_dir(project['id'])
    Image.new('RGB',(64,36),'white').save(root/'images'/'S001_design.png')
    project['slides'][0]['images']['design']='S001_design.png';project['locked']=True
    core.safe_json_write(root/'project.json',project)
    monkeypatch.setattr(server.rc,'get_regions',lambda *_:{'regions':[],'source_valid':True})
    monkeypatch.setattr(visual_agent_preview,'VisualAgentBrowserPreview',_FakePersistentPreview)
    issue1={'issue_id':'i1','priority':'P1','confidence':.95,'status':'open','description':'title size',
        'element_ids':['title'],'expected_result':'title size corrected'}
    issue2={'issue_id':'i2','priority':'P1','confidence':.8,'status':'open','description':'body size',
        'element_ids':['body'],'expected_result':'body size corrected'}
    phases=[]
    async def fake_turn(current,slide,design_path,render_path,*,phase,**kwargs):
        phases.append((phase,kwargs.get('tool_feedback',{}).get('current_issue',{}).get('issue_id')))
        if phase=='diagnose' and not kwargs.get('tool_feedback',{}).get('accepted_issues'):
            return {'diagnosis':{'issues':[issue1,issue2]},'tool_calls':[{'tool':'edit_element','arguments':{
                'slide_id':'S001','element_id':'title','evidence_issue_id':'i1','patch':{'style':{'size':30}}}}]}
        if phase=='reflect' and not any(name=='reflect' for name,_ in phases[:-1]):
            return {'decision':'continue','confidence':.95,'reason':'i1 is fixed; moving to body',
                'tool_calls':[{'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'body',
                    'evidence_issue_id':'i1','patch':{'style':{'size':16}}}}]}
        if phase=='reflect':
            return {'decision':'accept','confidence':.95,'reason':'i2 is fixed',
                'issues':[{'issue_id':'i2','status':'fixed','confidence':.95}],'tool_calls':[]}
        return {'diagnosis':{'issues':[]},'tool_calls':[],'reason':'no further issue'}
    monkeypatch.setattr(server,'visual_agent_turn',fake_turn)
    def fake_quick_render(worker,candidate,label):
        out=worker.session/'iterations'/label;out.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(64,36),'white').save(out/'chromium.png')
        return {'label':label,'chromium':f'iterations/{label}/chromium.png','renderer':'Chromium browser',
            'preview_sha256':hashlib.sha256(label.encode()).hexdigest(),'preview_only':True}
    monkeypatch.setattr(server,'_render_visual_agent_browser_preview',fake_quick_render)
    def fake_render(source,candidate,slide_id,session,label):
        out=session/'iterations'/label;out.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(64,36),'white').save(out/'libreoffice.png')
        return {'label':label,'libreoffice':f'iterations/{label}/libreoffice.png','chromium':f'iterations/{label}/chromium.png',
            'preview_sha256':hashlib.sha256(label.encode()).hexdigest(),'artifact_sha256':'a'*64,
            'browser_qa':{'layout_contract_pass':True,'pptx_pass':True,'workflow_pass':True},
            'pptx_report':{'native_text':{'ok':True},'saved_layout_check':{'ok':True}}}
    monkeypatch.setattr(server,'_render_visual_agent_candidate',fake_render)
    response=TestClient(server.app).post(f'/api/projects/{project["id"]}/visual-agent/run',json={
        'slide_id':'S001','mode':'restore','instruction':'fix visible issues','api_key':'test-only',
        'max_iterations':3,'confidence_threshold':.85,'max_attempts_per_issue':10})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='candidate_ready',result.get('reason')
    assert [row['status'] for row in result['issues']]==['已修复','fixed']
    assert len(result['iterations'])==2
    assert all(event['status']=='applied' for event in result['iterations'][1]['tool_events'])
    candidate=json.loads((root/'build'/'visual_agent'/result['token']/'candidate_project.json').read_text())
    elements={element['id']:element for element in candidate['slides'][0]['elements']}
    assert elements['title']['style']['size']==30
    assert elements['body']['style']['size']==16


def test_visual_agent_stop_request_prevents_pending_tool_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path)
    monkeypatch.setattr(server,'DATA',tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE','1')
    project=core.create_project('Visual agent stop','test',1)
    root=core.project_dir(project['id'])
    Image.new('RGB',(64,36),'white').save(root/'images'/'S001_design.png')
    project['slides'][0]['images']['design']='S001_design.png'
    project['locked']=True
    core.safe_json_write(root/'project.json',project)
    base=core.read_project(project['id'])
    run_id='a'*32
    monkeypatch.setattr(server.rc,'get_regions',lambda *_:{'regions':[],'source_valid':True})
    monkeypatch.setattr(visual_agent_preview, 'VisualAgentBrowserPreview', _FakePersistentPreview)

    async def fake_turn(current,slide,design_path,render_path,*,phase,**kwargs):
        if phase=='diagnose':
            stopped=server.stop_visual_agent(project['id'],run_id)
            assert stopped['stop_requested'] is True
            return {'diagnosis':{'issues':[]},'tool_calls':[{'tool':'edit_element','arguments':{
                'slide_id':'S001','element_id':'title','patch':{'style':{'size':32}}}}]}
        raise AssertionError('stop must prevent the reflection/model edit cycle')
    monkeypatch.setattr(server,'visual_agent_turn',fake_turn)

    def fake_render(source,candidate,slide_id,session,label):
        out=session/'iterations'/label;out.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(64,36),'white').save(out/'libreoffice.png')
        return {'label':label,'libreoffice':f'iterations/{label}/libreoffice.png','chromium':None,
            'artifact_sha256':hashlib.sha256(label.encode()).hexdigest()}
    monkeypatch.setattr(server,'_render_visual_agent_candidate',fake_render)
    response=TestClient(server.app).post(f'/api/projects/{project["id"]}/visual-agent/run',json={
        'run_id':run_id,'slide_id':'S001','mode':'free','instruction':'larger title',
        'max_iterations':2,'api_key':'test-only'})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='stopped'
    assert result['iterations']==[]
    assert core.read_project(project['id'])==base


def test_visual_agent_continues_after_accept_and_rolls_back_only_failed_round(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'DATA', tmp_path)
    monkeypatch.setattr(server, 'DATA', tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE', '1')
    project = core.create_project('Visual agent loop', 'test', 1)
    root = core.project_dir(project['id'])
    Image.new('RGB', (64, 36), 'white').save(root/'images'/'S001_design.png')
    slide = project['slides'][0]
    slide['images']['design'] = 'S001_design.png'
    project['locked'] = True
    core.safe_json_write(root/'project.json', project)
    base = json.loads((root/'project.json').read_text())
    monkeypatch.setattr(server.rc, 'get_regions', lambda *_: {'regions': [], 'source_valid': True})
    monkeypatch.setattr(visual_agent_preview, 'VisualAgentBrowserPreview', _FakePersistentPreview)
    phases=[];preview_calls=[]
    async def fake_turn(current, slide, design_path, render_path, *, phase, **kwargs):
        phases.append(phase)
        if phase=='diagnose' and phases.count('diagnose')==1:
            return {'diagnosis':{'issues':[{'issue_id':'i1','priority':'P0','description':'missing line'}]},
                'tool_calls':[{'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
                    'evidence_issue_id':'i1','patch':{'style':{'size':31}}}}]}
        if phase=='reflect' and phases.count('reflect')==1:
            return {'decision':'continue','confidence':.6,'issues':[{'issue_id':'i1','priority':'P0','status':'未处理'}],
                'tool_calls':[{'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
                    'evidence_issue_id':'i1','patch':{'box':[.12,.1,.7,.1]}}}]}
        return {'decision':'accept','confidence':.95,'issues':[{'issue_id':'i1','priority':'P0','status':'fixed','confidence':.95}],
            'tool_calls':[],'reason':'issue resolved'}
    monkeypatch.setattr(server, 'visual_agent_turn', fake_turn)
    def fake_render(source, candidate, slide_id, session, label):
        out=session/'iterations'/label;out.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(64,36),'white').save(out/'libreoffice.png')
        return {'label':label,'libreoffice':f'iterations/{label}/libreoffice.png','chromium':None,
            'artifact_sha256':hashlib.sha256(label.encode()).hexdigest(),
            'browser_qa':{'exit_code':0,'layout_contract_pass':True,'pptx_pass':True,'workflow_pass':True,'visual_status':'pass'},
            'pptx_report':{'native_text':{'ok':True},'saved_layout_check':{'ok':True}}}
    monkeypatch.setattr(server, '_render_visual_agent_candidate', fake_render)
    def fake_quick_render(worker,candidate,label):
        preview_calls.append(label)
        out=worker.session/'iterations'/label;out.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(64,36),'white').save(out/'chromium.png')
        return {'label':label,'chromium':f'iterations/{label}/chromium.png','renderer':'Chromium browser',
            'preview_sha256':hashlib.sha256(label.encode()).hexdigest(),'preview_only':True}
    monkeypatch.setattr(server, '_render_visual_agent_browser_preview', fake_quick_render)
    response=TestClient(server.app).post(f'/api/projects/{project["id"]}/visual-agent/run',json={
        'slide_id':'S001','mode':'free','instruction':'edit the title','max_iterations':3,'api_key':'test-only'})
    assert response.status_code==200,response.text
    result=response.json()
    assert phases==['diagnose','reflect','reflect','diagnose']
    assert result['status']=='candidate_ready'
    assert result['iterations'][0]['outcome']=='continue'
    assert result['iterations'][1]['outcome']=='accept'
    assert len(preview_calls)==3  # baseline plus two issue previews
    candidate=json.loads((root/'build'/'visual_agent'/result['token']/'candidate_project.json').read_text())
    title=next(e for e in candidate['slides'][0]['elements'] if e['id']=='title')
    assert title['style']['size']==31
    assert title['box']==[.12,.1,.7,.1]
    assert core.read_project(project['id'])==base


def test_visual_agent_reaudits_and_keeps_other_initial_issues_after_accept(tmp_path, monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path);monkeypatch.setattr(server,'DATA',tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE','1')
    project=core.create_project('Visual agent issue audit','test',1)
    root=core.project_dir(project['id'])
    Image.new('RGB',(64,36),'white').save(root/'images'/'S001_design.png')
    slide=project['slides'][0];slide['images']['design']='S001_design.png';slide['elements'][0]['text']='Title';slide['elements'][1]['text']='Body'
    project['locked']=True;core.safe_json_write(root/'project.json',project)
    monkeypatch.setattr(server.rc,'get_regions',lambda *_:{'regions':[],'source_valid':True})
    monkeypatch.setattr(visual_agent_preview,'VisualAgentBrowserPreview',_FakePersistentPreview)
    monkeypatch.setattr(server,'_render_visual_agent_candidate',lambda *args,**kwargs:{
        'browser_qa':{'exit_code':0,'layout_contract_pass':True,'pptx_pass':True,'workflow_pass':True},
        'pptx_report':{'native_text':{'ok':True},'saved_layout_check':{'ok':True}},
        'artifact_sha256':'a'*64})
    monkeypatch.setattr(server,'_render_visual_agent_browser_preview',lambda worker,candidate,label:
        worker.render(candidate,label))
    phases=[]
    async def fake_turn(current,sl,design_path,render_path,*,phase,**kwargs):
        phases.append(phase)
        if phase=='diagnose' and phases.count('diagnose')==1:
            return {'diagnosis':{'issues':[
                {'issue_id':'i1','priority':'P0','description':'title size mismatch','element_ids':['title'],'status':'open'},
                {'issue_id':'i2','priority':'P1','description':'body position mismatch','element_ids':['body'],'status':'open'}]},
                'tool_calls':[{'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
                    'evidence_issue_id':'i1','patch':{'style':{'size':30}}}}]}
        if phase=='reflect' and phases.count('reflect')==1:
            return {'decision':'accept','confidence':.95,'issues':[{'issue_id':'i1','status':'fixed','confidence':.95}],
                'tool_calls':[]}
        if phase=='diagnose' and phases.count('diagnose')==2:
            return {'diagnosis':{'issues':[{'issue_id':'i2','priority':'P1','description':'body position mismatch',
                'element_ids':['body'],'status':'open'}]},
                'tool_calls':[{'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'body',
                    'evidence_issue_id':'i2','patch':{'box':[.2,.3,.6,.1]}}}]}
        if phase=='reflect' and phases.count('reflect')==2:
            return {'decision':'accept','confidence':.96,'issues':[{'issue_id':'i2','status':'fixed','confidence':.96}],
                'tool_calls':[]}
        return {'diagnosis':{'issues':[]},'tool_calls':[],'decision':'done','reason':'No further supported differences'}
    monkeypatch.setattr(server,'visual_agent_turn',fake_turn)
    response=TestClient(server.app).post(f'/api/projects/{project["id"]}/visual-agent/run',json={
        'slide_id':'S001','mode':'free','instruction':'fix the two differences','max_attempts_per_issue':2,'api_key':'test-only'})
    assert response.status_code==200,response.text
    result=response.json()
    assert phases==['diagnose','reflect','diagnose','reflect','diagnose']
    assert result['status']=='candidate_ready'
    assert {x['issue_id']:x['status'] for x in result['issues']}=={'i1':'fixed','i2':'fixed'}
    candidate=json.loads((root/'build'/'visual_agent'/result['token']/'candidate_project.json').read_text())
    out=next(s for s in candidate['slides'] if s['id']=='S001')
    assert next(e for e in out['elements'] if e['id']=='title')['style']['size']==30
    assert next(e for e in out['elements'] if e['id']=='body')['box']==[.2,.3,.6,.1]


def test_visual_agent_keeps_accepted_candidate_but_blocks_delivery_with_open_issue(tmp_path, monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path);monkeypatch.setattr(server,'DATA',tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE','1')
    project=core.create_project('Visual agent unresolved issue gate','test',1)
    root=core.project_dir(project['id'])
    Image.new('RGB',(64,36),'white').save(root/'images'/'S001_design.png')
    slide=project['slides'][0];slide['images']['design']='S001_design.png'
    project['locked']=True;core.safe_json_write(root/'project.json',project)
    monkeypatch.setattr(server.rc,'get_regions',lambda *_:{'regions':[],'source_valid':True})
    monkeypatch.setattr(visual_agent_preview,'VisualAgentBrowserPreview',_FakePersistentPreview)
    monkeypatch.setattr(server,'_render_visual_agent_browser_preview',lambda worker,candidate,label:
        worker.render(candidate,label))
    monkeypatch.setattr(server,'_render_visual_agent_candidate',lambda *args,**kwargs:{
        'browser_qa':{'exit_code':0,'layout_contract_pass':True,'pptx_pass':True,'workflow_pass':True,'visual_status':'pass'},
        'pptx_report':{'native_text':{'ok':True},'saved_layout_check':{'ok':True}},'artifact_sha256':'b'*64})
    phases=[]
    async def fake_turn(current,sl,design_path,render_path,*,phase,**kwargs):
        phases.append(phase)
        if len(phases)==1:
            return {'diagnosis':{'issues':[{'issue_id':'fixed','priority':'P1','element_ids':['title'],'status':'open'}]},
                'tool_calls':[{'tool':'edit_element','arguments':{'slide_id':'S001','element_id':'title',
                    'evidence_issue_id':'fixed','patch':{'style':{'size':31}}}}]}
        if phase=='reflect':
            return {'decision':'accept','confidence':.96,'issues':[{'issue_id':'fixed','status':'fixed','confidence':.96}],
                'tool_calls':[]}
        issue={'issue_id':'open','priority':'P1','element_ids':['body'],'status':'open'}
        return {'diagnosis':{'issues':[issue]},'tool_calls':[],'reason':'Needs review'}
    monkeypatch.setattr(server,'visual_agent_turn',fake_turn)
    response=TestClient(server.app).post(f'/api/projects/{project["id"]}/visual-agent/run',json={
        'slide_id':'S001','mode':'free','instruction':'check current page','max_attempts_per_issue':2,'api_key':'test-only'})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='requires_review'
    candidate=json.loads((root/'build'/'visual_agent'/result['token']/'candidate_project.json').read_text())
    assert next(e for e in candidate['slides'][0]['elements'] if e['id']=='title')['style']['size']==31
    assert {row['issue_id']:row['status'] for row in result['issues']}=={'fixed':'fixed','open':'open'}


def test_visual_agent_duplicate_create_is_rechecked_and_not_stacked(tmp_path, monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path);monkeypatch.setattr(server,'DATA',tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE','1')
    project=core.create_project('Visual agent duplicate evidence','test',1)
    root=core.project_dir(project['id'])
    Image.new('RGB',(64,36),'white').save(root/'images'/'S001_design.png')
    slide=project['slides'][0];slide['images']['design']='S001_design.png'
    text='Missing-looking line';box=[.2,.3,.5,.05]
    slide['elements'].append({'id':'existing-line','kind':'text','text':text,'box':box,
        'style':{'size':18,'color':'17243A'}})
    project['locked']=True;core.safe_json_write(root/'project.json',project)
    base=core.read_project(project['id'])
    monkeypatch.setattr(server.rc,'get_regions',lambda *_:{'regions':[],'source_valid':True})
    monkeypatch.setattr(visual_agent_preview,'VisualAgentBrowserPreview',_FakePersistentPreview)
    monkeypatch.setattr(server,'_render_visual_agent_browser_preview',lambda worker,candidate,label:
        worker.render(candidate,label))
    calls=[]
    async def fake_turn(current,sl,design_path,render_path,*,phase,**kwargs):
        calls.append((phase,kwargs.get('tool_feedback') or {}))
        issue={'issue_id':'missing-line','priority':'P0','category':'missing_content',
            'description':f'Design中显示{ text }，渲染缺失。','expected_result':text,
            'region':box,'element_ids':[],'confidence':.95,'status':'open'}
        if len(calls)==1:
            return {'diagnosis':{'issues':[issue]},'tool_calls':[{'tool':'create_element',
                'arguments':{'slide_id':'S001','evidence_issue_id':'missing-line',
                    'element':{'kind':'text','text':text,'box':box,'style':{'size':18,'color':'17243A'}}}}]}
        if len(calls)==2:
            assert kwargs.get('tool_feedback',{}).get('current_issue') is None
            assert kwargs.get('tool_feedback',{}).get('accepted_issues')==['missing-line']
            return {'diagnosis':{'issues':[]},'decision':'done','tool_calls':[],'reason':'No other issue'}
        return {'diagnosis':{'issues':[]},'decision':'done','tool_calls':[],'reason':'No other issue'}
    monkeypatch.setattr(server,'visual_agent_turn',fake_turn)
    response=TestClient(server.app).post(f'/api/projects/{project["id"]}/visual-agent/run',json={
        'slide_id':'S001','mode':'restore','instruction':'check missing line','max_attempts_per_issue':2,'api_key':'test-only'})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='diagnosis_complete'
    assert result['issues'][0]['status']=='false_positive'
    assert len(calls)==2
    candidate=json.loads((root/'build'/'visual_agent'/result['token']/'candidate_project.json').read_text())
    assert len(candidate['slides'][0]['elements'])==len(base['slides'][0]['elements'])
    assert core.read_project(project['id'])==base


def test_visual_agent_noop_missing_text_edit_uses_chromium_visibility_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path);monkeypatch.setattr(server,'DATA',tmp_path)
    monkeypatch.setenv('PPT_PREVIEW_DISABLE','1')
    project=core.create_project('Visual agent no-op visibility','test',1)
    root=core.project_dir(project['id'])
    Image.new('RGB',(64,36),'white').save(root/'images'/'S001_design.png')
    slide=project['slides'][0];slide['images']['design']='S001_design.png'
    box=[.2,.3,.5,.05]
    element={'id':'existing-line','kind':'text','text':'Visible missing line','box':box,
        'style':{'size':18,'color':'17243A'}}
    slide['elements'].append(element)
    project['locked']=True;core.safe_json_write(root/'project.json',project)
    base=core.read_project(project['id'])
    monkeypatch.setattr(server.rc,'get_regions',lambda *_:{'regions':[],'source_valid':True})
    monkeypatch.setattr(visual_agent_preview,'VisualAgentBrowserPreview',_FakePersistentPreview)
    monkeypatch.setattr(server,'_render_visual_agent_browser_preview',lambda worker,candidate,label:
        worker.render(candidate,label))
    phases=[]
    async def fake_turn(current,sl,design_path,render_path,*,phase,**kwargs):
        phases.append(phase)
        if len(phases)==1:
            issue={'issue_id':'missing-visible-line','priority':'P0','category':'missing_content',
                'description':'Visible missing line missing from render','expected_result':'Visible missing line',
                'region':box,'element_ids':['existing-line'],'confidence':.95,'status':'open'}
            return {'diagnosis':{'issues':[issue]},'tool_calls':[{'tool':'edit_element','arguments':{
                'slide_id':'S001','element_id':'existing-line','evidence_issue_id':'missing-visible-line',
                'patch':{'style':{'size':18,'color':'17243A'}}}}]}
        return {'diagnosis':{'issues':[]},'decision':'done','tool_calls':[],'reason':'No other supported issues'}
    monkeypatch.setattr(server,'visual_agent_turn',fake_turn)
    response=TestClient(server.app).post(f'/api/projects/{project["id"]}/visual-agent/run',json={
        'slide_id':'S001','mode':'restore','instruction':'check visible line','max_attempts_per_issue':2,'api_key':'test-only'})
    assert response.status_code==200,response.text
    result=response.json()
    assert phases==['diagnose','diagnose']
    assert result['status']=='diagnosis_complete'
    assert result['issues'][0]['status']=='false_positive'
    assert 'Chromium DOM确认' in result['issues'][0]['evidence']
    assert result['iterations'][0]['outcome']=='false_positive_text_visible_in_chromium'
    assert core.read_project(project['id'])==base
