"""Image-grounded DeepSeek editing agent, with native tool-call and JSON fallback.

The model never receives filesystem paths, API keys, or approval/publish tools.
Vision judgement is advisory. Only a host QA callback can auto-accept drafts.
"""
from __future__ import annotations
import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Callable, Protocol
from .tools import EditingTools, TOOLS, RenderOutcome
from .errors import EditingError,ValidationError

SYSTEM = """你是 Visual PPT Studio 的视觉页面编辑 Agent。你拥有对隔离草稿的受控编辑工具，但不能修改正式项目。
目标：对照 Design 设计原稿与最新真实 PPT 渲染图，修复视觉差异；同一问题相关的参数应组合成一个原子事务。
先调用 get_page_state/get_element 获取对象 ID、原文和参数，不要根据图片猜测 ID。
识别：P0 核心内容缺失/错字；P1 严重错位/裁切/行距；P2 字体/颜色/对齐；P3 小细节。
不要把浏览器 UI 标签当作 PPT 内容。不要把透明文本框重叠等同于字形重叠。
还原模式中，不得编造/重写设计稿没有支持的文字。新增和修改文字需要 host 已注册证据 ID。
编辑工具允许直接指定精确的 box 或 box_pt(960×540点单位)、字号、字间距、行距、粗体、下划线、删除线、中英文字体和多段 rich_runs；公式使用 LaTeX text、size、align。布局引擎不应悄悄覆盖你给出的参数。
精确修改前用 get_element 查看既有 text、rich_runs 和字体，保持原文及空格；修改文字格式可分段 rich_runs，文本拼接必须严格等于 object.text。
不支持的属性（例如任意数值字体重量、图片内部图形）不可假称修改成功。
在编辑之后必须调用 render_page 真实渲染并查看反馈；不要声称没有执行的修改已完成。
如果工具返回失败，请根据明确错误换方案，避免重复无效操作。尽量一次只解决一个独立 issue，但可在一个事务调整多个对象。
目标是改善而不是制造更多改动。不得请求 shell、文件系统、网络和绕过权限的操作。
如果已没有可信可修复问题，直接说明 done 和剩余待人工复核问题。
只返回工具调用或一个简短 JSON {"done":true,"reason":"..."}。"""

class ChatProvider(Protocol):
    def complete(self,messages:list[dict],tools:list[dict]|None=None)->dict:...

class DeepSeekChatProvider:
    """OpenAI-compatible chat-completions transport. No API key ever in logs."""
    def __init__(self, *, api_key:str|None=None,model:str='deepseek-flash',
                 base_url:str='https://api.deepseek.com',timeout:float=90,allow_keyless:bool=False):
        self.api_key=api_key or os.getenv('DEEPSEEK_API_KEY','')
        if not self.api_key and not allow_keyless:raise ValidationError('当前 API Key 未配置')
        if not base_url.startswith('https://'):raise ValidationError('HTTPS API endpoint required')
        self.base_url=base_url.rstrip('/')
        self.model=model
        self.timeout=timeout

    def complete(self,messages:list[dict],tools:list[dict]|None=None,*,_reference_retry=False)->dict:
        import httpx
        import time
        from ..api_usage import record_request
        from ..api_images import prepare_messages, file_reference_failed, invalidate_references
        from ..api_limits import current as current_budget, before_request
        if current_budget() is not None:current_budget().check()
        inline_messages=messages
        messages=prepare_messages(messages,base_url=self.base_url,api_key=self.api_key)
        before_request(messages)
        body={'model':self.model,'messages':messages,'temperature':0.1,'max_tokens':2700}
        if tools:body.update({'tools':tools,'tool_choice':'auto'})
        started=time.perf_counter()
        telemetry_recorded=False
        try:
            with httpx.Client(timeout=self.timeout) as client:
                headers={'Authorization':'Bearer '+self.api_key} if self.api_key else {}
                response=client.post(self.base_url+'/chat/completions',json=body,headers=headers)
                elapsed=(time.perf_counter()-started)*1000
                if response.status_code>=400:
                    record_request(model=self.model,messages=messages,duration_ms=elapsed,status='failure',
                        http_status=response.status_code,error_class='HTTPStatusError',thinking=None,
                        application_status='provider_http_error')
                    telemetry_recorded=True
                    if not _reference_retry and file_reference_failed(response):
                        invalidate_references(messages,base_url=self.base_url,api_key=self.api_key)
                        return self.complete(inline_messages,tools,_reference_retry=True)
                response.raise_for_status()
            result=response.json()
            try:message=result['choices'][0]['message']
            except (KeyError,IndexError,TypeError) as exc:
                record_request(model=self.model,messages=messages,response=result,duration_ms=elapsed,status='failure',
                    http_status=response.status_code,error_class=type(exc).__name__,application_status='missing_message')
                telemetry_recorded=True
                raise
            record_request(model=self.model,messages=messages,response=result,duration_ms=elapsed,status='success',
                http_status=response.status_code,thinking=None,application_status='success')
            telemetry_recorded=True
            return message
        except (httpx.HTTPError,KeyError,IndexError,ValueError) as exc:
            # HTTP failures are recorded above; transport/JSON errors reach here.
            if not telemetry_recorded:
                record_request(model=self.model,messages=messages,duration_ms=(time.perf_counter()-started)*1000,
                    status='failure',error_class=type(exc).__name__,application_status='transport_or_parse_error')
            # Do not include provider error body; some endpoints echo request credentials.
            raise EditingError('DeepSeek request or response failed: '+type(exc).__name__) from exc


def image_block(path:str|Path,max_side:int=1200)->dict:
    """Downscale screenshots to contain latency/token cost; keep aspect ratio."""
    from PIL import Image
    import io
    path=Path(path)
    if not path.is_file():raise ValidationError('registered image path does not exist')
    if path.stat().st_size>40_000_000:raise ValidationError('image too large')
    with Image.open(path) as im:
        im=im.convert('RGB')
        if max(im.size)>max_side:im.thumbnail((max_side,max_side),Image.Resampling.LANCZOS)
        buf=io.BytesIO();im.save(buf,format='JPEG',quality=84,optimize=True)
    return {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(buf.getvalue()).decode('ascii')}}

class VisualEditingAgent:
    """Runnable control loop using a real external renderer + injectable model.

    max_rounds counts visual feedback rounds. Tool calls are limited separately.
    The model can never mark approval or overwrite originals via its tools.
    """
    def __init__(self, provider:ChatProvider, tools:EditingTools,*,
                 max_rounds:int=3,max_model_calls:int=12,max_tool_calls:int=30,
                 qa_callback:Callable|None=None):
        if not 1<=max_rounds<=12 or not 1<=max_model_calls<=40 or not 1<=max_tool_calls<=120:
            raise ValidationError('invalid iteration budget')
        self.provider=provider;self.tools=tools;self.max_rounds=max_rounds
        self.max_model_calls=max_model_calls;self.max_tool_calls=max_tool_calls
        self.qa_callback=qa_callback

    def run(self,slide_id:str,design_image:Path,*,instruction:str='',log_path:Path|None=None,
            cancel_event=None)->dict:
        if slide_id not in self.tools.session.policy.allowed_slides:raise ValidationError('slide not authorized')
        if not self.tools.renderer:raise EditingError('real renderer callback is required')
        initial=self.tools.dispatch('render_page',{'slide_id':slide_id})
        if not initial['ok']:raise EditingError('initial real render failed: '+initial.get('error',''))
        r=initial['result']
        mode=self.tools.session.policy.mode
        messages=[{'role':'system','content':SYSTEM},
                  {'role':'user','content':[
                      {'type':'text','text':f'幻灯片 {slide_id}。当前模式={mode}。用户要求：{instruction or "尽量还原设计原稿"}。左图为Design；右图为当前真实PPT渲染。请先读取场景对象再编辑。'},
                      image_block(design_image),image_block(r['image_path'])]}]
        logs=[];rounds=0;calls=0;tool_count=0;pending_pre_render:RenderOutcome|None=self.tools.last_render
        changed_since_render=False;modified_tx_ids=[];cancelled=False
        paused_reason=''
        while calls<self.max_model_calls and tool_count<self.max_tool_calls:
            if cancel_event is not None and cancel_event.is_set():cancelled=True;break
            from ..ai_context import runtime_messages_view
            from ..api_limits import APIPaused
            from ..api_images import ImageUploadError
            try:
                response=self.provider.complete(runtime_messages_view(messages),tools=TOOLS)
            except (APIPaused,ImageUploadError,EditingError) as exc:
                paused_reason=str(exc)
                logs.append({'type':'api_paused','reason':paused_reason})
                break
            calls+=1
            if cancel_event is not None and cancel_event.is_set():
                cancelled=True;logs.append({'type':'cancelled','message':'模型请求返回后停止；未执行本轮工具调用。'});break
            tool_calls=response.get('tool_calls') or []
            # JSON fallback for providers that can't combine vision and native tool_calls.
            if not tool_calls:
                content=response.get('content') or ''
                try:
                    payload=json.loads(content) if isinstance(content,str) else content
                except (ValueError,TypeError):payload={}
                if isinstance(payload,dict) and payload.get('tool') in {t['function']['name'] for t in TOOLS}:
                    tool_calls=[{'id':f'fallback-{calls}','type':'function','function':{
                        'name':payload['tool'],'arguments':json.dumps(payload.get('arguments',{}),ensure_ascii=False)}}]
                else:
                    logs.append({'type':'model_final','content':str(content)[:350]})
                    break
            assistant={'role':'assistant','content':response.get('content'), 'tool_calls':tool_calls}
            # Required by DeepSeek when thinking is enabled with native tools.
            if 'reasoning_content' in response:assistant['reasoning_content']=response['reasoning_content']
            messages.append(assistant)
            crop_feedback=[]
            for call in tool_calls:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled=True;logs.append({'type':'cancelled','message':'收到停止请求；未执行后续工具调用。'});break
                if tool_count>=self.max_tool_calls:break
                tool_count+=1
                func=call.get('function') or {};name=func.get('name','')
                raw=func.get('arguments','{}')
                try:args=json.loads(raw) if isinstance(raw,str) else raw
                except ValueError:args={}
                if name=='batch_edit' and rounds>=self.max_rounds:
                    result={'ok':False,'error':'maximum visual edit rounds reached'}
                else:
                    result=self.tools.dispatch(name,args)
                event={'turn':calls,'tool':name,'ok':result.get('ok',False)}
                if name=='batch_edit' and result.get('ok'):
                    changed_since_render=True
                    modified_tx_ids.append(result['result']['tx_id'])
                    event['transaction_id']=result['result']['tx_id']
                    event['changes']=result['result']['changes']
                elif name in ('undo_edit','redo_edit') and result.get('ok'):
                    changed_since_render=True
                elif name=='render_page' and result.get('ok'):
                    if changed_since_render:
                        rounds+=1
                        verified=self._evaluate_pending(modified_tx_ids,pending_pre_render,self.tools.last_render,slide_id)
                        event['host_qa']=verified
                        modified_tx_ids=[];changed_since_render=False
                    pending_pre_render=self.tools.last_render
                    event['image_sha256']=result['result']['image_sha256']
                if not result.get('ok'):event['error']=result.get('error','')
                logs.append(event)
                if name=='get_region_screenshot' and result.get('ok'):
                    crop_feedback.append(result['result']['image_path'])
                # Tool output may contain object data, but never private API credentials.
                safe_result=result
                if result.get('ok') and name in ('render_page','get_region_screenshot'):
                    safe_result={'ok':True,'result':{k:v for k,v in result['result'].items()
                      if k not in ('image_path','pptx_path')}}
                from ..ai_context import evidence_view
                msg=json.dumps(evidence_view(safe_result),ensure_ascii=False,default=str)
                messages.append({'role':'tool','tool_call_id':call.get('id',''), 'content':msg[:15000]})
            for crop in crop_feedback:
                messages.append({'role':'user','content':[{'type':'text','text':'这是请求放大的真实渲染区域。'},image_block(crop)]})
            if cancelled:break
            # Ensure visually observed feedback after each write group, even if model failed to call render.
            if changed_since_render:
                next_render=self.tools.dispatch('render_page',{'slide_id':slide_id})
                if next_render['ok']:
                    rounds+=1
                    qa=self._evaluate_pending(modified_tx_ids,pending_pre_render,self.tools.last_render,slide_id)
                    logs.append({'type':'automatic_visual_feedback','round':rounds,'image_sha256':next_render['result']['image_sha256'],'host_qa':qa})
                    modified_tx_ids=[];changed_since_render=False;pending_pre_render=self.tools.last_render
                    # Include actual *after* screenshot in next model turn.
                    messages.append({'role':'user','content':[
                        {'type':'text','text':f'这是第{rounds}轮工具修改后的真实渲染图。请观察效果，结合工具错误/QA反馈继续处理或完成。'},
                        image_block(next_render['result']['image_path'])]})
                else:
                    logs.append({'type':'render_failed','error':next_render.get('error')});break
            elif any(x.get('tool')=='render_page' and x.get('ok') for x in logs[-len(tool_calls):]):
                # Explicit model-requested render also gets image feedback.
                messages.append({'role':'user','content':[{'type':'text','text':'这是请求的真实渲染图。'},image_block(self.tools.last_render.image_path)]})
            if rounds>=self.max_rounds and not changed_since_render:
                # Permit final reflection without more writes, then stop.
                if calls>=self.max_model_calls:break
                messages.append({'role':'user','content':'视觉迭代预算已耗尽。不要再编辑，请总结当前待人工复核的事项并结束。'})
                try:
                    result=self.provider.complete(runtime_messages_view(messages),tools=None);calls+=1
                except (APIPaused,ImageUploadError,EditingError) as exc:
                    paused_reason=str(exc)
                    logs.append({'type':'api_paused','reason':paused_reason})
                    break
                logs.append({'type':'final_reflection','content':str(result.get('content',''))[:600]})
                break
        output={'slide_id':slide_id,'rounds':rounds,'model_calls':calls,'tool_calls':tool_count,'cancelled':cancelled,
                'paused_reason':paused_reason,
                'current_draft_sha':self.tools.session.page(slide_id)['draft_sha'],
                'transactions':[{'tx_id':t.tx_id,'issue_id':t.issue_id,'status':t.status,'qa':t.qa}
                                for t in self.tools.session.transactions if t.slide_id==slide_id],
                'events':logs, 'warning':'Draft only. Unaccepted transactions are not included in prepared_commit.'}
        if log_path:
            path=Path(log_path);path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
        return output

    def _evaluate_pending(self,tx_ids:list[str],before:RenderOutcome|None,after:RenderOutcome|None,sid:str):
        """Trusted local QA only. Never auto-accept based on model self-rating."""
        if not tx_ids:return []
        results=[]
        for tx_id in tx_ids:
            verdict=None
            if self.qa_callback and before and after:
                try:verdict=self.qa_callback(before,after,tx_id,self.tools.session)
                except Exception as exc:verdict={'hard_checks_pass':False,'visual_checks_pass':False,'reason':type(exc).__name__}
            if verdict is not None and len(tx_ids)>1 and verdict.get('isolated_transaction_verified') is not True:
                results.append({'tx_id':tx_id,'status':'pending_independent_qa_required'})
                continue
            if verdict is not None:
                good=verdict.get('hard_checks_pass') is True and verdict.get('visual_checks_pass') is True
                decided=self.tools.session.mark(tx_id,good,qa=verdict)
                results.append({'tx_id':tx_id,'status':decided['status']})
            else:
                results.append({'tx_id':tx_id,'status':'pending_host_qa'})
        return results
