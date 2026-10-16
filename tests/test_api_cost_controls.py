"""Offline regression coverage: no live provider or paid request is used."""
import asyncio
import base64
from copy import deepcopy
import json

import httpx
import pytest

from studio import ai, api_images, api_limits, api_usage, core
from studio.ai_context import (object_view, source_view, table_view,
    model_object_context, history_view, runtime_messages_view)
from studio.ai_plans import RepairPlanQueue


@pytest.fixture(autouse=True)
def local_store(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'DATA', tmp_path)
    monkeypatch.setattr(api_usage, '_usage_dir', lambda: tmp_path / 'usage')
    api_images._unsupported.clear()


def image_message(raw=b'unchanged-image-content'):
    url='data:image/png;base64,'+base64.b64encode(raw).decode()
    return [{'role':'user','content':[{'type':'image_url','image_url':{'url':url}}]}]


def mock_uploads(monkeypatch, handler):
    original=httpx.Client
    class FakeClient:
        def __init__(self,*args,**kwargs):self.client=original(transport=httpx.MockTransport(handler))
        def __enter__(self):return self
        def __exit__(self,*args):self.client.close()
        def post(self,*args,**kwargs):return self.client.post(*args,**kwargs)
    monkeypatch.setattr(api_images.httpx,'Client',FakeClient)


def unpack_table(table):
    return [{table['columns'][i]:row[i] for i in present}
            for row,present in zip(table['rows'],table['present'])]


def test_projection_retains_visual_and_formula_evidence_and_does_not_mutate():
    e={'id':'formula','kind':'formula','text':r'\frac{QK^T}{\sqrt{d_k}}',
       'box':[.1,.2,.3,.1],'source_text':'original ', 'style':{'size':22,'color':'00FFFF'},
       'rich_runs':[{'text':'原文 ','style':{'bold':True}}],
       'text_layout':{'font':{'actual_family':'Cambria Math','fallback':False,
           'file_sha256':'PRIVATE-FONT-HASH','path':'/private/font.ttf'},
           'ink_box_pt':[10,20,30,40],'baseline_pt':24,'whitespace':{'trailing':' '},
           'input_signature':'INTERNAL','shaping':{'advance':12}}}
    original=deepcopy(e)
    context=model_object_context([e,e])
    first=context['objects'][0]
    assert first['text']==e['text'] and first['rich_runs']==e['rich_runs']
    assert first['box']==e['box'] and first['style']==e['style']
    layouts=unpack_table(context['layout_table'])
    assert layouts[0]['ink_box_pt']==[10,20,30,40]
    assert layouts[0]['whitespace']['trailing']==' '
    assert context['font_definitions'][layouts[0]['font_ref']]['actual_family']=='Cambria Math'
    assert len(context['font_definitions'])==1
    assert 'PRIVATE-FONT-HASH' not in json.dumps(context)
    assert '/private/' not in json.dumps(context)
    assert e==original


def test_table_roundtrip_distinguishes_missing_null_and_preserves_changed_source():
    rows=[{'a':None,'b':' '},{'b':'formula','c':[1,2]}]
    assert unpack_table(table_view(rows))==rows
    native={'id':'n','run_id':'r','text':'new','source_text':'old','box':[.2,.2,.2,.1]}
    row={'id':'ocr','run_id':'r','text':'old','source_text':'old','box':[.1,.2,.2,.1],
         'source_box':[.1,.2,.2,.1],'confidence':.98,'ocr_conflicts':[{'text':'older'}]}
    source=source_view([row],[native])[0]
    assert source['element_ref']=='n'
    assert source['text']=='old' and source['box']==row['box']
    assert source['confidence']==.98 and source['ocr_conflicts']==row['ocr_conflicts']


def test_history_keeps_all_issue_outcomes_and_recent_actual_tool_actions():
    rows=[{'round':i,'tool_calls':[{'tool':'edit_element','arguments':{'patch':{'text':str(i)}}}],
           'tool_events':[{'status':'no_change','result':{'element':{'id':'n','text':str(i)}}}],
           'after_render':{'whole_private_project':'DO NOT SEND'},
           'reflection':{'issues':[{'issue_id':f'i{i}','status':'review'}]},'outcome':'continue'} for i in range(8)]
    result=history_view(rows)
    assert len(result['latest_issue_outcomes'])==8
    assert len(result['recent_attempts'])==3
    assert result['recent_attempts'][-1]['tool_calls']==rows[-1]['tool_calls']
    assert 'DO NOT SEND' not in json.dumps(result)


def test_stable_tool_prefix_and_full_live_values_precede_issue_instructions(monkeypatch,tmp_path):
    from PIL import Image
    path=tmp_path/'source.png';Image.new('RGB',(32,20),'white').save(path)
    captured=[]
    async def fake(messages,*a,**kw):
        captured.append(messages)
        return {'diagnosis':{'issues':[]},'tool_calls':[]}
    monkeypatch.setattr(ai,'call_deepseek',fake)
    slide={'id':'S001','elements':[{'id':'live','kind':'text','text':'原文 ',
        'box':[.1,.2,.3,.1],'style':{'size':18,'color':'FFFFFF'}}]}
    for issue_id in ('UNIQUE-ISSUE-ONE','UNIQUE-ISSUE-TWO'):
        asyncio.run(ai.visual_agent_turn({},slide,path,path,
            tool_feedback={'current_issue':{'issue_id':issue_id,'description':'spacing'}}))
    assert captured[0][0]==captured[1][0]
    assert '工具Schema=' in captured[0][0]['content']
    for messages,issue_id in zip(captured,('UNIQUE-ISSUE-ONE','UNIQUE-ISSUE-TWO')):
        text=messages[1]['content'][0]['text']
        assert text.index('原文 ')<text.index(issue_id)
        assert '本轮阶段要求：' in text and '不要先调用get_element或get_page_state' in text
        assert len([x for x in messages[1]['content'] if x.get('type')=='image_url'])==2


def test_runtime_retains_tool_protocol_reasoning_design_and_latest_before_after():
    def img(name):return {'type':'image_url','image_url':{'url':name}}
    messages=[{'role':'user','content':[img('design'),img('baseline')]},
        {'role':'assistant','content':None,'tool_calls':[{'id':'call-1'}],'reasoning_content':'keep exactly'},
        {'role':'tool','tool_call_id':'call-1','content':'tool result'},
        {'role':'user','content':[img('render-1')]},
        {'role':'user','content':[img('render-2')]},
        {'role':'user','content':[img('render-3')]}]
    original=deepcopy(messages)
    result=runtime_messages_view(messages)
    assert result[1:3]==messages[1:3]
    urls=[p['image_url']['url'] for m in result if isinstance(m.get('content'),list)
          for p in m['content'] if p.get('type')=='image_url']
    assert urls==['design','render-2','render-3']
    assert messages==original


def test_files_upload_once_preserves_bytes_ttl_scopes_and_private_logs(monkeypatch,tmp_path):
    raw=b'EXACT-PRIVATE-IMAGE-CONTENT'
    calls=[]
    def handler(request):
        calls.append(request)
        assert raw in request.content
        assert b'86400' in request.content and b'user_data' in request.content
        return httpx.Response(200,json={'id':'file-api-'+str(len(calls))})
    mock_uploads(monkeypatch,handler)
    messages=image_message(raw);original=deepcopy(messages)
    a=api_images.prepare_messages(messages,base_url='https://api.deepseek.com',api_key='SECRET-KEY-A')
    b=api_images.prepare_messages(messages,base_url='https://api.deepseek.com',api_key='SECRET-KEY-A')
    assert len(calls)==1 and a==b
    assert messages==original
    assert a[0]['content'][0]['type']=='file'
    assert api_usage.message_metrics(a)['image_count']==1
    api_images.prepare_messages(messages,base_url='https://api.deepseek.com',api_key='SECRET-KEY-B')
    assert len(calls)==2
    report=api_usage.summary()
    assert report['network']['reused_references']==1
    assert report['network']['upload_bytes']==2*len(raw)
    for path in tmp_path.rglob('*'):
        if path.is_file():
            content=path.read_bytes()
            assert raw not in content and b'SECRET-KEY' not in content


def test_files_expired_cache_reuploads_and_low_detail_is_not_changed(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'id':'file-api-'+str(len(calls))})
    mock_uploads(monkeypatch,handler)
    kw={'base_url':'https://api.deepseek.com','api_key':'k'}
    api_images.prepare_messages(image_message(),**kw)
    for path in (core.DATA/'api_image_cache').glob('*.json'):
        cache=json.loads(path.read_text())
        for value in cache.values():value['valid_until']=0
        path.write_text(json.dumps(cache))
    api_images.prepare_messages(image_message(),**kw)
    assert len(calls)==2
    low=image_message();low[0]['content'][0]['image_url']['detail']='low'
    assert api_images.prepare_messages(low,**kw)==low
    assert len(calls)==2


def test_files_unsupported_is_visible_and_does_not_keep_retrying(monkeypatch):
    calls=[]
    def handler(request):calls.append(request);return httpx.Response(404)
    mock_uploads(monkeypatch,handler)
    messages=image_message();kw={'base_url':'https://api.deepseek.com','api_key':'k'}
    assert api_images.prepare_messages(messages,**kw)==messages
    assert api_images.prepare_messages(messages,**kw)==messages
    assert len(calls)==1
    assert api_usage.summary()['network']['inline_fallbacks']==1


@pytest.mark.parametrize('code',[401,402,403])
def test_upload_auth_or_balance_failure_stops_before_inference(monkeypatch,code):
    mock_uploads(monkeypatch,lambda request:httpx.Response(code))
    budget=api_limits.RequestBudget();token=api_limits.bind(budget)
    try:
        with pytest.raises(api_images.ImageUploadError):
            api_images.prepare_messages(image_message(),base_url='https://api.deepseek.com',api_key='k')
        with pytest.raises(api_limits.APIPaused):budget.check()
        assert budget.requests==0
    finally:api_limits.reset(token)


def test_budget_limits_exact_repeats_unknown_usage_and_inflight_cost():
    budget=api_limits.RequestBudget(max_requests=2)
    budget.before('one',10);budget.before('two',10)
    with pytest.raises(api_limits.APIPaused):budget.before('three',10)
    budget=api_limits.RequestBudget()
    budget.before('same',10);budget.before('same',10)
    with pytest.raises(api_limits.APIPaused):budget.before('same',10)
    budget=api_limits.RequestBudget(max_cost_cny=.1)
    budget.after({'cost':{'cost_cny':.12}})
    with pytest.raises(api_limits.APIPaused):budget.check()
    budget=api_limits.RequestBudget()
    budget.after({});budget.after({})
    with pytest.raises(api_limits.APIPaused):budget.check()


def test_async_transport_uses_file_reference_usage_and_stops_on_budget(monkeypatch):
    uploads=[];inferences=[]
    mock_uploads(monkeypatch,lambda r:(uploads.append(r) or httpx.Response(200,json={'id':'file-api-ok'})))
    original=httpx.AsyncClient
    def handler(request):
        body=json.loads(request.content);inferences.append(body)
        assert body['messages'][-1]['content'][0]=={'type':'file','file_id':'file-api-ok'}
        return httpx.Response(200,json={'model':'deepseek-flash','choices':[{'message':{'content':'{"ok":true}'}}],
            'usage':{'prompt_tokens':100,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100,
                     'completion_tokens':3,'total_tokens':103}})
    class AsyncClient:
        def __init__(self,*a,**kw):self.client=original(transport=httpx.MockTransport(handler))
        async def __aenter__(self):return self
        async def __aexit__(self,*a):await self.client.aclose()
        async def post(self,*a,**kw):return await self.client.post(*a,**kw)
    monkeypatch.setattr(ai.httpx,'AsyncClient',AsyncClient)
    monkeypatch.setenv('DEEPSEEK_BASE_URL','https://api.deepseek.com')
    async def run():
        budget=api_limits.RequestBudget(max_requests=1);token=api_limits.bind(budget)
        try:
            assert await ai.call_deepseek(image_message(),api_key='SECRET')=={'ok':True}
            with pytest.raises(api_limits.APIPaused):
                await ai.call_deepseek(image_message(b'different'),api_key='SECRET')
            assert budget.cost_cny>0
        finally:api_limits.reset(token)
    asyncio.run(run())
    assert len(uploads)==1 and len(inferences)==1
    assert api_usage.summary()['totals']['total_tokens']==103


def test_planned_edits_expire_on_target_or_sibling_changes_and_cannot_cross_pages():
    slide={'id':'S001','elements':[{'id':'a','kind':'text','text':'A','line_id':'line',
        'box':[0,0,.1,.1]},{'id':'b','kind':'text','text':'B','line_id':'line','box':[.1,0,.1,.1]}]}
    plan={'issue_id':'i','tool_calls':[{'tool':'edit_element','arguments':{
        'slide_id':'S001','element_id':'b','patch':{'box':[.2,0,.1,.1]}}}]}
    queue=RepairPlanQueue();queue.add([plan],slide)
    assert queue.take({'issue_id':'i'},slide)==plan['tool_calls']
    assert queue.take({'issue_id':'i'},slide)==[]
    queue.add([plan],slide)
    slide['elements'][0]['box']=[.05,0,.1,.1]
    assert queue.take({'issue_id':'i'},slide)==[]
    cross=deepcopy(plan);cross['tool_calls'][0]['arguments']['slide_id']='S002'
    queue.add([cross],slide)
    assert queue.take({'issue_id':'i'},slide)==[]


def test_sync_deleted_file_is_reuploaded_only_once_and_all_attempts_count(monkeypatch):
    from studio.visual_ppt_runtime.agent import DeepSeekChatProvider
    uploads=[];inferences=[]
    def handler(request):
        if request.url.path.endswith('/files'):
            uploads.append(1)
            return httpx.Response(200,json={'id':'file-api-'+str(len(uploads))})
        body=json.loads(request.content);inferences.append(body)
        if len(inferences)==1:
            return httpx.Response(404,json={'error':{'code':'file_not_found'}})
        return httpx.Response(200,json={'choices':[{'message':{'content':'ok','reasoning_content':'preserved'}}],
            'usage':{'prompt_tokens':2,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':2,
                     'completion_tokens':1,'total_tokens':3}})
    mock_uploads(monkeypatch,handler)
    budget=api_limits.RequestBudget(max_requests=2);token=api_limits.bind(budget)
    try:
        result=DeepSeekChatProvider(api_key='secret').complete(image_message())
    finally:api_limits.reset(token)
    assert result['reasoning_content']=='preserved'
    assert len(uploads)==len(inferences)==2
    assert budget.requests==2 and budget.unknown_cost_requests==1
    assert inferences[0]['messages'][0]['content'][0]['file_id']!=inferences[1]['messages'][0]['content'][0]['file_id']
