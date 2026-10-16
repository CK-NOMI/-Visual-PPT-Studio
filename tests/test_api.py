from io import BytesIO
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from studio import ai,core,server,reconstruction as rc

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DATA',tmp_path)
    monkeypatch.setattr(server,'DATA',tmp_path)
    return TestClient(server.app)

def png(color=(255,255,255)):
    b=BytesIO();Image.new('RGB',(640,360),color).save(b,'PNG');return b.getvalue()

def test_all_deepseek_calls_receive_shared_agent_contract():
    user_payload=[{'type':'text','text':'image follows'},
                  {'type':'image_url','image_url':{'url':'data:image/png;base64,abc'}}]
    messages=ai._with_base_system([
        {'role':'system','content':'Task-specific JSON schema.'},
        {'role':'user','content':user_payload},
    ])
    assert len([m for m in messages if m['role']=='system'])==1
    assert '待分析数据' in messages[0]['content']
    assert 'Task-specific JSON schema.' in messages[0]['content']
    assert messages[1]['content']==user_payload

def test_stage03_smart_repair_calls_vision_once_and_builds_isolated_candidate(tmp_path,monkeypatch,client):
    import asyncio
    from studio import repair_pipeline as rp
    design=tmp_path/'design.png';render=tmp_path/'render.png'
    design.write_bytes(png());render.write_bytes(png())
    project={'id':'smart','revision':3,'design':{'font':'Arial'},'slides':[{'id':'S001','title':'Missing title',
        'body':'','images':{'design':'S001_design.png','clean':'S001_clean.png'},'elements':[]}]}
    monkeypatch.setattr(server,'p_or_404',lambda pid:project)
    monkeypatch.setattr(server,'configured_api_key',lambda:'configured')
    monkeypatch.setattr(server,'_current_export_preview',lambda *args:render)
    monkeypatch.setattr(server,'_current_export_report',lambda *args:{'artifact_sha256':'a'*64,'preview_renderer':'test'})
    monkeypatch.setattr(server,'_smart_diagnostic_cache',lambda *args:('cache-key',tmp_path/'cache.json'))
    monkeypatch.setattr(server,'rel_media',lambda *args:design)
    monkeypatch.setattr(rp.rc,'get_regions',lambda *args:{'source_valid':True,'regions':[]})
    monkeypatch.setattr(rp,'inspect',lambda *args,**kwargs:{'pages':[{'slide_id':'S001','issues':[]}],'issue_count':0})
    seen={}
    async def proposal(*args,**kwargs):
        seen['call']=args
        seen['verified_layout_issues']=kwargs.get('verified_layout_issues')
        return {'missing':[{'text':'Missing title','box':[.1,.1,.4,.1],'role':'title','confidence':.98,'evidence':'visible in design'}],
                'repairs':[{'issue_id':'existing-line','action':'repair_line_layout','object_ids':['title'],'related_object_ids':[],
                    'confidence':.95,'evidence':'visible baseline conflict','intent':'restore baseline'}],'review':[]}
    monkeypatch.setattr(ai,'propose_missing_text',proposal)
    def fake_create(p,slides,issues,calls,review,**kwargs):
        seen['issues']=issues;seen['calls']=calls;seen['review']=review;seen['ai_intents']=kwargs.get('ai_intents')
        return {'token':'test','actions':[],'diagnosis':{'issue_count':1},'ai_calls':calls}
    monkeypatch.setattr(rp,'create',fake_create)
    response=client.post('/api/projects/smart/repair/smart-candidates',json={'slides':['S001']})
    assert response.status_code==200
    assert seen['calls']==1 and seen['issues'].get('S001',[{}])[0].get('text')=='Missing title', (response.json(),seen)
    assert seen['ai_intents']['S001'][0]['action']=='repair_line_layout'

def test_vision_focus_regions_keep_unmatched_badge_digit_even_if_same_digit_exists_elsewhere():
    focus=server._vision_focus_regions([
        {'id':'badge-1','text':'1','box':[.05,.18,.05,.07],'confidence':.999,'review_reason':'OCR冲突：T'},
        {'id':'line-1','text':'High confidence source phrase','box':[.1,.4,.3,.04],'confidence':.98},
    ],[
        {'id':'diagram-1','text':'1次小模型调用','box':[.4,.8,.3,.04]},
        {'id':'second-bullet','text':'Different phrase','box':[.1,.5,.3,.04]},
    ])
    assert focus[0]['id']=='badge-1'
    assert any(x['id']=='line-1' for x in focus)

def test_missing_text_vision_request_includes_paired_enlarged_regions(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';render=tmp_path/'render.png';design.write_bytes(png());render.write_bytes(png())
    seen={}
    async def fake(messages,*args,**kwargs):
        seen['content']=messages[1]['content']
        return {'missing':[],'repairs':[],'review':[]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    asyncio.run(ai.propose_missing_text({}, {'id':'S002','title':'','body':''},design,render,[],[],
        focus_regions=[{'id':'badge-1','text':'1','box':[.05,.18,.05,.07],'reason':'OCR冲突'}]))
    blocks=seen['content']
    assert len([x for x in blocks if x.get('type')=='image_url'])==4
    assert any('放大区域 badge-1 · Design' in x.get('text','') for x in blocks)
    assert any('放大区域 badge-1 · PPTX' in x.get('text','') for x in blocks)

def test_focus_finding_promotes_only_visual_confirmation_with_no_native_object(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';render=tmp_path/'render.png';design.write_bytes(png());render.write_bytes(png())
    async def fake(*args,**kwargs):
        return {'missing':[],'repairs':[],'review':['区域疑似有数字'],'focus_findings':[
            {'region_id':'badge-1','status':'missing','text':'1','visual_confirmed':True,'confidence':.98,'evidence':'Design有1，PPT局部为空'}]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    result=asyncio.run(ai.propose_missing_text({}, {'id':'S002','title':'','body':''},design,render,[],[],
        focus_regions=[{'id':'badge-1','text':'1','box':[.05,.18,.05,.07],'native_object_ids':[]}]))
    assert result['missing'][0]['text']=='1' and result['missing'][0]['visual_confirmed'] is True
    assert result['focus_findings'][0]['status']=='missing'

def test_focus_finding_does_not_duplicate_full_slide_proposal(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';render=tmp_path/'render.png';design.write_bytes(png());render.write_bytes(png())
    async def fake(*args,**kwargs):
        return {'missing':[{'text':'Missing bullet','box':[.075,.525,.39,.031],'confidence':.95}],
                'repairs':[],'review':[],'focus_findings':[
                    {'region_id':'bullet','status':'missing','text':'Missing bullet','visual_confirmed':True,
                     'confidence':.96,'evidence':'同一位置确实缺失'}]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    result=asyncio.run(ai.propose_missing_text({}, {'id':'S002','title':'','body':''},design,render,[],[],
        focus_regions=[{'id':'bullet','text':'Missing bullet','box':[.074,.524,.40,.033],'native_object_ids':[]}]))
    assert len(result['missing'])==1

def test_focus_finding_with_native_occupant_remains_non_actionable(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';render=tmp_path/'render.png';design.write_bytes(png());render.write_bytes(png())
    async def fake(*args,**kwargs):
        return {'missing':[],'repairs':[],'review':[],'focus_findings':[
            {'region_id':'badge-1','status':'missing','text':'1','visual_confirmed':True,'confidence':.98,'evidence':'unclear'}]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    result=asyncio.run(ai.propose_missing_text({}, {'id':'S002','title':'','body':''},design,render,[],[],
        focus_regions=[{'id':'badge-1','text':'1','box':[.05,.18,.05,.07],'native_object_ids':['number-1']}]))
    assert result['missing']==[] and result['focus_findings'][0]['visual_confirmed'] is False

def test_deepseek_network_failure_retries_and_reports_dns_clearly(monkeypatch):
    import asyncio,socket
    attempts={'count':0}
    class BrokenClient:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def post(self,*args,**kwargs):
            attempts['count']+=1
            error=ai.httpx.ConnectError('')
            error.__cause__=socket.gaierror(8,'name lookup failed')
            raise error
    monkeypatch.setattr(ai.httpx,'AsyncClient',lambda **kwargs:BrokenClient())
    async def no_wait(_):return None
    monkeypatch.setattr(ai.asyncio,'sleep',no_wait)
    with pytest.raises(ai.AIError,match='DNS 无法解析 DeepSeek API 域名'):
        asyncio.run(ai.call_deepseek([{'role':'user','content':'test'}],api_key='test-secret'))
    assert attempts['count']==2

def test_pair_review_prompt_says_removed_text_is_expected(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';clean=tmp_path/'clean.png';design.write_bytes(png());clean.write_bytes(png())
    seen={}
    async def fake(messages,*args,**kwargs):
        seen['messages']=messages
        return {'score':90,'usable_as_clean':True,'residual_text':[],'layout_changes':[],
                'nontext_damage':[],'recommendations':[]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    asyncio.run(ai.review_image_pair({}, {'id':'S001'},design,clean))
    system=seen['messages'][0]['content'];prompt=seen['messages'][1]['content'][0]['text']
    assert '原文字缺失' in system+prompt
    assert '绝不能' in prompt
    assert '只把' in prompt

def test_structure_prompt_matches_output_object_limit(tmp_path,monkeypatch):
    import asyncio
    image=tmp_path/'design.png';image.write_bytes(png())
    seen={}
    async def fake(messages,*args,**kwargs):
        seen['prompt']=messages[1]['content'][0]['text']
        return {'objects':[]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    project={'topic':'test'};slide={'id':'S001'}
    asyncio.run(ai.analyze_structure(project,slide,image))
    assert '最多56个对象' in seen['prompt']
    assert '最多32个对象' not in seen['prompt']

def test_calibration_prompt_uses_rendered_effect_and_preserves_formula(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';clean=tmp_path/'clean.png';render=tmp_path/'render.png'
    for p in (design,clean,render):p.write_bytes(png())
    seen={}
    async def fake(messages,*args,**kwargs):
        seen['system']=messages[0]['content'];seen['prompt']=messages[1]['content'][0]['text']
        return {'score':50,'issues':[],'corrections':[]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    project={'design':{'font':'Test'}}
    slide={'id':'S001','elements':[{'id':'eq1','kind':'formula','text':'x^2','box':[.1,.1,.2,.1],'style':{}}]}
    asyncio.run(ai.calibrate_text_layout(project,slide,design,clean,render))
    assert 'PPTX真实渲染' in seen['prompt']
    assert '不得重写、简化、翻译' in seen['prompt']
    assert '禁止返回box、坐标、边距' in seen['prompt']
    assert '缺字/重影/基线/间距问题只记为issues' in seen['prompt']
    assert '禁止修改几何坐标' in seen['system']
    assert '先从真实渲染观察差异' in seen['system']

def test_visual_repair_intents_are_batched_and_bound_to_verified_issue_ids(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';render=tmp_path/'render.png'
    design.write_bytes(png());render.write_bytes(png());seen={}
    async def fake(messages,*args,**kwargs):
        seen['prompt']=messages[1]['content'][0]['text']
        return {'missing':[], 'repairs':[{'issue_id':'line-1','action':'repair_line_layout',
            'object_ids':['a','b'],'related_object_ids':[],'confidence':.95,'evidence':'DOM baseline differs',
            'intent':'restore one baseline'}], 'review':[]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    project={'design':{'font':'Arial'}}
    slide={'id':'S001','title':'Title','body':'Body'}
    verified=[{'issue_id':'line-1','issue_type':'line_geometry','matched_elements':['a','b'],
        'suggested_action':'repair_line','confidence':1,'evidence':{'constraints':['baseline_mismatch']}}]
    result=asyncio.run(ai.propose_missing_text(project,slide,design,render,[],[
        {'id':'a','kind':'text','text':'Title','box':[.1,.1,.2,.1],'style':{'size':20}},
        {'id':'b','kind':'text','text':'Body','box':[.3,.1,.2,.1],'style':{'size':20}}],
        verified_layout_issues=verified,
        repair_feedback=[{'text':'Missing badge 1','result':'上轮文字未落在原图区域'}]))
    assert result['repairs'][0]['issue_id']=='line-1'
    assert 'DOM baseline differs'==result['repairs'][0]['evidence']
    assert '已证实布局异常' in seen['prompt'] and 'line-1' in seen['prompt']
    assert '不要返回坐标、字号、颜色、删除指令或改写内容' in seen['prompt']
    assert '圆形徽标' in seen['prompt'] and 'visual_confirmed' in seen['prompt']
    assert '此前候选验收反馈' in seen['prompt'] and '上轮文字未落在原图区域' in seen['prompt']

def test_calibration_sends_labeled_original_and_current_render_every_call(tmp_path,monkeypatch):
    import asyncio,base64
    design=tmp_path/'design.png';clean=tmp_path/'clean.png';render=tmp_path/'render.png'
    design.write_bytes(b'original-design-marker');clean.write_bytes(b'clean-background-marker');render.write_bytes(b'current-render-marker')
    seen={}
    async def fake(messages,*args,**kwargs):
        seen['content']=messages[1]['content']
        return {'score':50,'issues':[],'corrections':[]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    slide={'id':'S001','elements':[{'id':'title','kind':'text','text':'标题','box':[.1,.1,.5,.1],'style':{}}]}
    asyncio.run(ai.calibrate_text_layout({'design':{'font':'Test'}},slide,design,clean,render,optimization_round=7))
    blocks=seen['content'];labels=[x.get('text','') for x in blocks if x.get('type')=='text']
    images=[x['image_url']['url'] for x in blocks if x.get('type')=='image_url']
    assert len(images)==2
    assert '对照图 1/2 — A【原始完整设计稿' in labels[1]
    assert '对照图 2/2 — C【当前可编辑PPTX真实渲染' in labels[2]
    assert images[0].endswith(base64.b64encode(b'original-design-marker').decode())
    assert images[1].endswith(base64.b64encode(b'current-render-marker').decode())
    assert '不得为了消除换行而把字重调细' in labels[0]
    assert '文本框的宽度/高度和位置' in labels[0]

def test_calibration_rejects_missing_or_aliased_reference_images(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';render=tmp_path/'render.png';clean=tmp_path/'clean.png'
    design.write_bytes(b'design');render.write_bytes(b'render');clean.write_bytes(b'clean')
    monkeypatch.setattr(ai,'call_deepseek',lambda *a,**k:None)
    slide={'id':'S001','elements':[{'id':'t','kind':'text','text':'标题','box':[0,0,.5,.1]}]}
    try:asyncio.run(ai.calibrate_text_layout({},slide,design,clean,tmp_path/'missing.png'))
    except ai.AIError as e:assert '真实渲染图' in str(e)
    else:raise AssertionError('missing rendered image must block calibration')
    try:asyncio.run(ai.calibrate_text_layout({},slide,design,clean,design))
    except ai.AIError as e:assert '同一文件' in str(e)
    else:raise AssertionError('aliased reference images must block calibration')

def test_calibration_prompt_prioritizes_browser_verified_layout_and_peer_styles(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';clean=tmp_path/'clean.png';render=tmp_path/'render.png'
    for p in (design,clean,render):p.write_bytes(png())
    monkeypatch.setattr(rc,'get_regions',lambda project,sid:{'source_valid':True,'regions':[
        {'text':'研究目标','role':'title','box':[.4,.1,.08,.04],'style':{'size':22,'bold':True}},
        {'text':'论文信息','role':'title','box':[.1,.1,.15,.04],'style':{'size':22,'bold':True}},
    ]})
    seen={}
    async def fake(messages,*args,**kwargs):
        seen['prompt']=messages[1]['content'][0]['text']
        return {'score':40,'issues':['标题换行压住正文'],'corrections':[]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    slide={'id':'S001','elements':[
        {'id':'target','kind':'text','text':'研究目标','box':[.4,.1,.08,.04],'style':{'size':22,'bold':True}},
        {'id':'peer','kind':'text','text':'论文信息','box':[.1,.1,.15,.04],'style':{'size':22,'bold':True}},
    ]}
    result=asyncio.run(ai.calibrate_text_layout({'design':{'font':'Test'}},slide,design,clean,render,
        optimization_feedback='前一候选未提升，请换方案',optimization_round=2))
    assert result['layout_issues']==[]
    assert result['layout_observation_status']=='unavailable_no_browser_observation'
    assert result['layout_evidence'][0]['type']=='estimated_overflow_capacity'
    assert len(result['peer_style_groups'])==1
    assert '并列级别的标题或正文必须保持一致' in seen['prompt']
    assert '浏览器DOM/Canvas实测布局异常' in seen['prompt']
    assert '估算不代表字形遮挡/溢出' in seen['prompt']
    assert 'estimated_overflow_capacity' not in seen['prompt']
    assert 'frame_overlap' not in seen['prompt']
    assert '前一候选未提升' in seen['prompt']

def test_calibration_frame_intersection_requires_browser_ink_confirmation():
    slide={'id':'S001','elements':[
        {'id':'left','kind':'text','text':'作者','box':[.1,.1,.3,.1],'style':{},'line_id':'line','run_order':0},
        {'id':'right','kind':'text','text':'Name','box':[.2,.1,.3,.1],'style':{},'line_id':'line','run_order':1},
    ]}
    current=[{'id':e['id'],'kind':e['kind'],'content':e['text'],'box':e['box'],'style':e['style'],
              'line_id':e['line_id'],'run_order':e['run_order']} for e in slide['elements']]
    observation={'stage':{'width':960,'height':540},'objects':[
        {'id':'left','box_px':{'x':96,'y':54,'width':288,'height':54},
         'ink_box':{'x':200,'y':60,'width':40,'height':20},'range_rects':[{'x':200,'y':60,'width':40,'height':20}],
         'baseline_px':75,'overflow':'visible'},
        {'id':'right','box_px':{'x':192,'y':54,'width':288,'height':54},
         'ink_box':{'x':260,'y':60,'width':40,'height':20},'range_rects':[{'x':260,'y':60,'width':40,'height':20}],
         'baseline_px':75,'overflow':'visible'},
    ]}
    issues,_,evidence=ai._calibration_layout_context({},slide,current,observation)
    assert not issues
    assert len([x for x in evidence if x['type']=='frame_overlap'])==1
    observation['objects'][1]['ink_box']['x']=230
    observation['objects'][1]['range_rects'][0]['x']=230
    issues,_,_=ai._calibration_layout_context({},slide,current,observation)
    assert any(x['type']=='visible_ink_overlap' for x in issues)

def test_calibration_browser_observation_is_revision_and_frame_bound():
    project={'id':'project123456','revision':4}
    slide={'id':'S001','elements':[{'id':'t','kind':'text','text':'Title','box':[.1,.1,.5,.1],
                                    'style':{'size':18}}]}
    observation={'project_id':'project123456','project_revision':4,'slide_id':'S001',
        'stage':{'width':960,'height':540},'objects':[{'id':'t','text':'Title',
        'box_px':{'x':96,'y':54,'width':480,'height':54},'ink_box':{'x':100,'y':60,'width':80,'height':18},
        'range_rects':[{'x':100,'y':60,'width':80,'height':18}],'baseline_px':78,
        'font_size_px':18,'font_weight':'400','font_color':'rgb(0, 0, 0)','overflow':'visible'}]}
    accepted,status=server.validate_calibration_layout_observation(observation,project,slide)
    assert status=='revision_bound_chromium_dom' and len(accepted['objects'])==1
    observation['project_revision']=3
    ignored,status=server.validate_calibration_layout_observation(observation,project,slide)
    assert ignored is None and status=='ignored_stale_or_wrong_scope'


def test_calibration_accepts_bounded_tracking_and_line_spacing(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';clean=tmp_path/'clean.png';render=tmp_path/'render.png'
    for p in (design,clean,render):p.write_bytes(png())
    async def fake(messages,*args,**kwargs):
        return {'score':55,'issues':[],'corrections':[{'id':'t1','box':[.1,.1,.8,.2],
            'style':{'char_spacing_pt':.75,'line_spacing':1.2},'reason':'rendered lines too tight'}]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    slide={'id':'S001','elements':[{'id':'t1','kind':'text','text':'示例文字','box':[.1,.1,.7,.2],'style':{}}]}
    result=asyncio.run(ai.calibrate_text_layout({'design':{'font':'Test'}},slide,design,clean,render))
    assert result['corrections'][0]['style']=={'char_spacing_pt':.75,'line_spacing':1.2}

def test_calibration_keeps_style_only_candidate_and_normalizes_malformed_fields(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';clean=tmp_path/'clean.png';render=tmp_path/'render.png'
    for p in (design,clean,render):p.write_bytes(png())
    async def fake(messages,*args,**kwargs):
        return {'score':50,'issues':{'type':'missing_text','expected':'2'},
                'corrections':[{'id':'number_2','style':{'color':'F3F9F9','bold':True},
                                'box':[.2,.2,.9,.9],'reason':'number exists but blends into its circle'}]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    original_box=[.5,.2,.02,.03]
    slide={'id':'S002','elements':[{'id':'number_2','kind':'text','text':'2','box':original_box,'style':{}}]}
    result=asyncio.run(ai.calibrate_text_layout({'design':{'font':'Test'}},slide,design,clean,render))
    assert result['issues'][0]=='{"type": "missing_text", "expected": "2"}'
    assert result['corrections'][0]['box']==original_box  # invalid optional box does not discard a valid color fix
    assert result['corrections'][0]['style']=={'color':'F3F9F9','bold':True}
    assert any('坐标无效' in issue for issue in result['issues'])

def test_api_plan_upload_export(client):
    res=client.post('/api/projects',json={'title':'Demo','topic':'Engineering','pages':2,'style':'editorial'})
    assert res.status_code==200
    pid=res.json()['id']
    assert client.get(f'/api/projects/{pid}/prompts').status_code==400
    assert client.post(f'/api/projects/{pid}/lock',json={'locked':True}).status_code==200
    assert client.get(f'/api/projects/{pid}/prompts').status_code==200
    missing=client.post(f'/api/projects/{pid}/upload-batch',files=[('files',('bad.png',png(),'image/png'))])
    assert missing.status_code==400
    response=client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':'clean'},files={'file':('my cover artwork final.png',png(),'image/png')})
    assert response.status_code==200,response.text
    assert response.json()['filename']=='S001_clean.png'
    assert response.json()['original_filename']=='my cover artwork final.png'
    assert client.get(f'/media/{pid}/S001_clean.png').status_code==200
    exp=client.post(f'/api/projects/{pid}/export?mode=editable')
    assert exp.status_code==200,exp.text
    assert exp.json()['native_text']['ok'] is True
    assert len(exp.json()['previews'])==2 or exp.json()['render_warning'] is not None
    assert client.get(f'/build/{pid}/presentation_editable.pptx').status_code==200

def test_second_stage_confirmation_is_per_slide_and_clean_hash_scoped(client):
    pid=client.post('/api/projects',json={'title':'Multi page gate','topic':'Reconstruction','pages':2}).json()['id']
    client.post(f'/api/projects/{pid}/lock',json={'locked':True})
    for sid in ('S001','S002'):
        for role in ('design','clean'):
            response=client.post(f'/api/projects/{pid}/upload',data={'slide_id':sid,'role':role},
                                 files={'file':(f'{sid}_{role}.png',png(),'image/png')})
            assert response.status_code==200,response.text
    add_text=lambda sid: client.put(f'/api/projects/{pid}/slides/{sid}',json={'elements':[
        {'id':'ocr_001','kind':'text','text':sid,'box':[.1,.1,.3,.1],'style':{}}]})
    assert add_text('S001').status_code==200
    assert client.post(f'/api/projects/{pid}/slides/S001/confirm-reconstruction').status_code==200
    pages=client.get(f'/api/projects/{pid}/reconstruction-progress').json()['pages']
    assert [(p['slide_id'],p['confirmed']) for p in pages]==[('S001',True),('S002',False)]
    assert client.post(f'/api/projects/{pid}/slides/S002/confirm-reconstruction').status_code==200
    assert all(p['confirmed'] for p in client.get(f'/api/projects/{pid}/reconstruction-progress').json()['pages'])
    client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':'clean'},
                files={'file':('S001_clean_v2.png',png((245,245,245)),'image/png')})
    pages=client.get(f'/api/projects/{pid}/reconstruction-progress').json()['pages']
    assert pages[0]['confirmed'] is False
    assert pages[1]['confirmed'] is True


def test_calibration_memory_persists_only_render_validated_prompt_guidance(client):
    pid=client.post('/api/projects',json={'title':'Memory test','topic':'OCR feedback','pages':1}).json()['id']
    url=f'/api/projects/{pid}/calibration-memory'
    assert client.get(url).json()=={'version':1,'history':[],'playbook':[]}
    saved=client.post(url,json={'slide_id':'S001','round':2,'before':70,'after':78,
        'outcome':'improved','properties':['char_spacing_pt','line_spacing'],
        'issues':['文字过挤'],'corrections':[{'id':'t1','box':[.1,.1,.8,.1],
        'style':{'color':'AA2233','char_spacing_pt':.5},'reason':'match target'}],
        'differences_before':[{'type':'text_color','expected':'AA2233','actual':'777777'}],
        'differences_after':[{'type':'text_color','expected':'AA2233','actual':'AA2233'}],
        'candidate_applied':True,'prompt_insight':'先看真实字面密度再小幅调节字间距。'})
    assert saved.status_code==200,saved.text
    memory=client.get(url).json()
    assert memory['history'][0]['delta']==8
    assert memory['history'][0]['corrections'][0]['style']['color']=='AA2233'
    assert memory['history'][0]['differences_before'][0]['actual']=='777777'
    assert memory['history'][0]['candidate_applied'] is True
    assert memory['playbook'][0]['validated_by_render'] is True
    assert memory['playbook'][0]['guidance'].startswith('先看真实')
    rejected=client.post(url,json={'slide_id':'S001','round':3,'before':78,'after':75,
        'outcome':'no_improvement','properties':['size'],'prompt_insight':'不要记录失败提示'})
    assert rejected.status_code==200
    assert len(client.get(url).json()['playbook'])==1

def test_project_is_local_and_untrusted_path_refused(client):
    assert client.get('/api/projects/../../../etc/passwd').status_code in (404,400)
    assert client.get('/api/health').json()['offline_image_mode'] is True

def test_clean_uploaded_before_design_must_be_repaired_as_a_pair(client):
    p=client.post('/api/projects',json={'title':'Pair check','topic':'Image pairs','pages':1}).json()
    pid=p['id'];client.post(f'/api/projects/{pid}/lock',json={'locked':True})
    files={'file':('S001_clean.png',png(),'image/png')}
    assert client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':'clean'},files=files).status_code==200
    files={'file':('S001_design.png',png(),'image/png')}
    assert client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':'design'},files=files).status_code==200
    project=client.get(f'/api/projects/{pid}').json()
    assert project['slides'][0]['image_metadata']['clean']['stale_source'] is True
    assert any('底图已过期' in e['message'] for e in client.get(f'/api/projects/{pid}/qa').json()['errors'])
    files={'file':('S001_clean.png',png(),'image/png')}
    assert client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':'clean'},files=files).status_code==200
    project=client.get(f'/api/projects/{pid}').json()
    assert project['slides'][0]['image_metadata']['clean']['stale_source'] is False

def test_single_upload_can_be_removed_and_restored(client):
    p=client.post('/api/projects',json={'title':'Undo upload','topic':'Image upload','pages':1}).json();pid=p['id']
    client.post(f'/api/projects/{pid}/lock',json={'locked':True})
    files={'file':('my selected image.png',png(),'image/png')}
    assert client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':'design'},files=files).status_code==200
    removed=client.post(f'/api/projects/{pid}/slides/S001/images/design/remove',json={})
    assert removed.status_code==200,removed.text
    token=removed.json()['undo_token']
    project=client.get(f'/api/projects/{pid}').json()
    assert 'design' not in project['slides'][0]['images']
    assert client.get(f'/media/{pid}/S001_design.png').status_code==404
    restored=client.post(f'/api/projects/{pid}/uploads/{token}/restore',json={})
    assert restored.status_code==200,restored.text
    project=client.get(f'/api/projects/{pid}').json()
    assert project['slides'][0]['images']['design']=='S001_design.png'
    assert project['slides'][0]['image_metadata']['design']['original_filename']=='my selected image.png'

def test_ai_prompt_agent_returns_reviewable_pair(client,monkeypatch):
    p=client.post('/api/projects',json={'title':'Transformer','topic':'Attention','pages':1}).json();pid=p['id']
    client.put(f'/api/projects/{pid}/slides/S001',json={'title':'Transformer','body':'Attention mechanism'})
    client.post(f'/api/projects/{pid}/lock',json={'locked':True})
    async def fake(project,slide,api_key,model):
        assert slide['title']=='Transformer'
        return {'slide_id':slide['id'],'design_prompt':'含字原图提示词','clean_prompt':'无字底图提示词',
                'visual_contract':'构图一致','checks':['文字已清除']}
    monkeypatch.setattr(server,'generate_image_prompts',fake)
    response=client.post(f'/api/projects/{pid}/slides/S001/prompts/ai',json={})
    assert response.status_code==200,response.text
    assert response.json()['clean_prompt']=='无字底图提示词'

def test_prompt_agent_pins_exact_planned_text(monkeypatch):
    import asyncio
    project={'title':'Transformer','topic':'Attention','audience':'工程师','style':'editorial',
             'design':{'bg':'FFFFFF','ink':'112233','accent':'445566','font':'Test Font'},
             'slides':[{'id':'S001','number':1,'title':'标题原文','body':'正文原文','intent':'说明结构'}]}
    async def fake(*args,**kwargs):
        return {'design_prompt':'生成技术信息图','clean_prompt':'移除文字并保持构图',
                'visual_contract':'布局相同','checks':['检查残留文字']}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    result=asyncio.run(ai.generate_image_prompts(project,project['slides'][0]))
    assert '标题原文' in result['design_prompt']
    assert '正文原文' in result['design_prompt']
    assert '不得新增任何字符' in result['clean_prompt'] or '移除文字并保持构图' in result['clean_prompt']

def test_prompt_agent_replaces_no_text_rule_in_full_design_prompt(monkeypatch):
    import asyncio
    project={'title':'论文汇报','topic':'Speculative Decoding','audience':'研究人员','style':'editorial',
             'design':{'bg':'0B1430','ink':'FFFFFF','accent':'3766CF','font':'Test Font'},
             'slides':[{'id':'S001','number':1,'title':'Fast Inference','body':'推测解码加速推理','intent':'学术封面'}]}
    async def fake(*args,**kwargs):
        return {'design_prompt':'制作深色学术封面。重要：不要生成任何文字，只生成背景。',
                'clean_prompt':'移除文字并保持构图','visual_contract':'构图一致','checks':['检查文字']}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    result=asyncio.run(ai.generate_image_prompts(project,project['slides'][0]))
    assert '完整视觉设计稿' in result['design_prompt']
    assert 'Fast Inference' in result['design_prompt']
    assert '不要生成任何文字' not in result['design_prompt']
    assert '无文字' in result['clean_prompt'] or '移除文字' in result['clean_prompt']
    assert '替换为含字完整设计稿' in result['notice']

def test_pair_review_overrides_contradictory_model_approval(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';clean=tmp_path/'clean.png';design.write_bytes(png());clean.write_bytes(png())
    async def fake(*args,**kwargs):
        return {'score':62,'usable_as_clean':True,'residual_text':[],'layout_changes':['面板发生偏移'],
                'nontext_damage':['图标样式变化'],'recommendations':['重新生成']}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    result=asyncio.run(ai.review_image_pair({}, {'id':'S001'},design,clean))
    assert result['model_recommended_clean'] is True
    assert result['usable_as_clean'] is False

def test_model_json_repairs_raw_newlines_inside_ocr_strings():
    parsed=ai._json_from_model('{"text":"first line\nsecond line"}')
    assert parsed=={'text':'first line\nsecond line'}

def test_pair_review_normalizes_pixel_boxes(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';clean=tmp_path/'clean.png';design.write_bytes(png());clean.write_bytes(png())
    async def fake(*args,**kwargs):
        return {'score':40,'usable_as_clean':False,
                'residual_text':[{'text':'heading','box':[40,10,500,70],'confidence':.95}],
                'layout_changes':[],'nontext_damage':[],'recommendations':['去除残字']}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    result=asyncio.run(ai.review_image_pair({}, {'id':'S001'},design,clean))
    assert result['residual_text'][0]['box']==[.0625,round(10/360,6),.78125,round(70/360,6)]

def test_pair_review_fails_closed_on_malformed_model_fields(tmp_path,monkeypatch):
    import asyncio
    design=tmp_path/'design.png';clean=tmp_path/'clean.png';design.write_bytes(png());clean.write_bytes(png())
    async def fake(*args,**kwargs):
        return {'score':95,'usable_as_clean':True,'residual_text':'uncertain result',
                'layout_changes':[],'nontext_damage':[],'recommendations':[]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    result=asyncio.run(ai.review_image_pair({}, {'id':'S001'},design,clean))
    assert result['usable_as_clean'] is False
    assert result['schema_warnings']==['residual_text']

def test_deepseek_pair_review_requires_hash_bound_clean(client,monkeypatch):
    p=client.post('/api/projects',json={'title':'Pair','topic':'Review','pages':1}).json();pid=p['id']
    client.post(f'/api/projects/{pid}/lock',json={'locked':True})
    for role in ('design','clean'):
        assert client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':role},
            files={'file':(f'S001_{role}.png',png(),'image/png')}).status_code==200
    async def fake(*args):
        return {'score':95,'usable_as_clean':True,'residual_text':[],'layout_changes':[],
                'nontext_damage':[],'recommendations':[]}
    monkeypatch.setattr(server,'review_image_pair',fake)
    response=client.post(f'/api/projects/{pid}/slides/S001/review-image-pair',json={})
    assert response.status_code==200,response.text
    assert response.json()['source_pair_current'] is True

def test_pair_review_marks_only_local_residuals_as_repairable(client,monkeypatch):
    p=client.post('/api/projects',json={'title':'Residual','topic':'Review','pages':1}).json();pid=p['id']
    client.post(f'/api/projects/{pid}/lock',json={'locked':True})
    for role in ('design','clean'):
        assert client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':role},
            files={'file':(f'S001_{role}.png',png(),'image/png')}).status_code==200
    async def fake(*args):
        return {'score':78,'usable_as_clean':False,
                'residual_text':[{'text':'label','box':[.2,.3,.1,.04],'confidence':.94}],
                'layout_changes':[],'nontext_damage':[],'recommendations':['局部修复']}
    monkeypatch.setattr(server,'review_image_pair',fake)
    response=client.post(f'/api/projects/{pid}/slides/S001/review-image-pair',json={})
    assert response.status_code==200,response.text
    assert response.json()['repairable_clean'] is True

def test_refine_clean_prompt_uses_current_hash_bound_pair(client,monkeypatch):
    p=client.post('/api/projects',json={'title':'Pair refine','topic':'Image editing','pages':1}).json();pid=p['id']
    client.post(f'/api/projects/{pid}/lock',json={'locked':True})
    for role in ('design','clean'):
        assert client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':role},
            files={'file':(f'S001_{role}.png',png(),'image/png')}).status_code==200
    async def fake(project,slide,design_path,clean_path,review,current_prompt,api_key,model):
        assert slide['id']=='S001' and current_prompt=='旧提示词'
        assert review['layout_changes']==['标题栏偏移']
        return {'slide_id':'S001','clean_prompt':'只编辑原图并擦除标题','focus_regions':['标题栏'],'checks':['无残字']}
    monkeypatch.setattr(server,'refine_clean_prompt',fake)
    response=client.post(f'/api/projects/{pid}/slides/S001/prompts/refine-clean',json={
        'current_prompt':'旧提示词','review':{'score':70,'layout_changes':['标题栏偏移']}})
    assert response.status_code==200,response.text
    assert response.json()['clean_prompt']=='只编辑原图并擦除标题'
    project=core.read_project(pid)
    core.write_image(project,'S001','design',png((0,0,0)),'replacement.png')
    response=client.post(f'/api/projects/{pid}/slides/S001/prompts/refine-clean',json={
        'current_prompt':'旧提示词','review':{'score':70}})
    assert response.status_code==400

def test_calibration_agent_uses_current_render_and_only_suggestions(client,monkeypatch):
    p=client.post('/api/projects',json={'title':'Calibration','topic':'Text layout','pages':1}).json();pid=p['id']
    client.post(f'/api/projects/{pid}/lock',json={'locked':True})
    for role in ('design','clean'):
        assert client.post(f'/api/projects/{pid}/upload',data={'slide_id':'S001','role':role},
            files={'file':(f'S001_{role}.png',png(),'image/png')}).status_code==200
    project=core.read_project(pid)
    core.update_slide(project,'S001',{'elements':[{'id':'heading','kind':'text','text':'Editable','box':[.1,.1,.4,.1],'style':{'size':20}}]})
    # Bind the preview fixture to a real, current exported artifact, without
    # pretending this inert PNG is an Office rendering. Hash/revision checks stay enabled.
    report=server.render_deck(pid,'editable');folder=core.project_dir(pid)/'build'
    name=f"editable_r{project['revision']}_{report['artifact_sha256'][:12]}_S001_preview.png"
    (folder/name).write_bytes(png());report['previews']=[name]
    core.safe_json_write(folder/'report_editable.json',report)
    async def fake(*args):
        return {'slide_id':'S001','score':82,'issues':['标题偏低'],
                'corrections':[{'id':'heading','box':[.1,.08,.4,.1],'style':{'size':22},'reason':'与参考图基线对齐'}]}
    monkeypatch.setattr(server,'calibrate_text_layout',fake)
    response=client.post(f'/api/projects/{pid}/slides/S001/calibrate-text',json={})
    assert response.status_code==200,response.text
    assert response.json()['corrections'][0]['id']=='heading'
    assert core.read_project(pid)['slides'][0]['elements'][0]['box']==[.1,.1,.4,.1]
    assert 'box' not in response.json()['corrections'][0]
    assert response.json()['corrections'][0]['style']['size']==22
    assert response.json()['restricted_suggestions']
    assert response.json()['corrections'][0]['target_text']=='Editable'
