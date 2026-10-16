"""Experimental Stage 03 API for the isolated Visual PPT Editing Runtime."""
from __future__ import annotations
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import core
from .api_usage import set_context as set_api_usage_context, reset_context as reset_api_usage_context
from .api_usage import record_task_outcome
from .ai import configured_api_key, configured_api_base_url, configured_api_provider, configured_model

def _active_model_label():
    provider=configured_api_provider()
    label={'deepseek':'DeepSeek','qwen':'通义千问','glm':'智谱 GLM','opencode':'OpenCode'}.get(provider,provider)
    model=configured_model()
    return f'{label}（{model}）' if model else label
from .core import find_slide, project_dir, read_project, safe_json_write, save_project
from .pptx_native import build as build_python_pptx
from .quality import check_native_text, check_saved_layout

router=APIRouter()
SESSIONS:dict[str,dict[str,Any]]={}

def _restore_run_state(row:dict[str,Any])->None:
    row.setdefault('run_lock',threading.Lock())
    row.setdefault('cancel_event',threading.Event())
    state_path=row['root']/'agent-run-state.json'
    if not row.get('run_state'):
        try:state=json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError):state={}
        if state.get('running'):
            state.update(running=False,phase='interrupted',message='服务重启时运行中断；隔离草稿与已记录事务已保留。')
        row['run_state']=state
    row.setdefault('running',bool(row['run_state'].get('running')))
    row.setdefault('last_run',row['run_state'].get('result'))

def _save_run_state(row:dict[str,Any])->None:
    safe_json_write(row['root']/'agent-run-state.json',row.get('run_state') or {})

class SessionRequest(BaseModel):
    slide_ids:list[str]=Field(min_length=1,max_length=60)
    mode:str='restore'
    allow_text_changes:bool=False

class RunRequest(BaseModel):
    instruction:str=Field(default='',max_length=4000)
    api_key:str|None=None
    model:str|None=None
    cost_budget_cny:float=Field(default=1.0,ge=.1,le=100)
    request_budget:int=Field(default=12,ge=1,le=100)
    transfer_budget_mb:float=Field(default=64,ge=1,le=1024)

class DecisionRequest(BaseModel):
    accepted:bool

def _project(pid:str):
    if not re.fullmatch(r'[a-f0-9]{12}',pid):raise HTTPException(404,'项目不存在')
    try:return read_project(pid)
    except FileNotFoundError as exc:raise HTTPException(404,str(exc)) from exc

def _session(pid:str,sid:str):
    if not re.fullmatch(r'[a-f0-9]{32}',sid):raise HTTPException(404,'编辑会话不存在')
    key=f'{pid}:{sid}'
    if key in SESSIONS:return SESSIONS[key]
    root=(core.DATA/'editing_runtime'/pid/sid).resolve()
    allowed=(core.DATA/'editing_runtime'/pid).resolve()
    if not root.is_relative_to(allowed) or not (root/'session.json').is_file():
        raise HTTPException(404,'编辑会话不存在')
    try:
        from .visual_ppt_runtime.transactions import EditingSession
        from .visual_ppt_runtime.render import StudioLibreOfficeRenderer
        from .visual_ppt_runtime.tools import EditingTools
        project=_project(pid);session=EditingSession.resume(root)
        if session.base.get('id')!=pid:raise ValueError('会话项目不匹配')
        render_root=root/'renders'/uuid.uuid4().hex
        renderer=StudioLibreOfficeRenderer(core.ROOT,project_dir(pid),render_root,python_executable=sys.executable)
        row={'session':session,'root':root,'renderer':renderer,'tools':EditingTools(session,renderer),
             'project_id':pid,'base_signature':session.base_sha,'run_state':{}}
        _restore_run_state(row)
        SESSIONS[key]=row
        return row
    except Exception as exc:
        raise HTTPException(400,str(exc)) from exc

@router.post('/api/projects/{pid}/editing-runtime/sessions')
def create_session(pid:str,request:SessionRequest):
    from .visual_ppt_runtime.permissions import PermissionPolicy
    from .visual_ppt_runtime.transactions import EditingSession
    project=_project(pid)
    if request.mode not in {'restore','free'}:raise HTTPException(400,'mode 必须为 restore 或 free')
    slide_ids=list(dict.fromkeys(request.slide_ids))
    for slide_id in slide_ids:
        if not re.fullmatch(r'S\d{3}',slide_id):raise HTTPException(400,'slide_id 无效')
        page=find_slide(project,slide_id)
        if not page or not page.get('images',{}).get('design'):
            raise HTTPException(400,f'{slide_id} 缺少 Design 原图')
    session_id=uuid.uuid4().hex
    root=core.DATA/'editing_runtime'/pid/session_id
    # The user explicitly delegated full page-local editing to DeepSeek for
    # this isolated runtime. Restore mode still protects the formal project;
    # candidate acceptance remains a separate human decision.
    policy=PermissionPolicy(mode=request.mode,allowed_slides=frozenset(slide_ids),
        allow_text_changes=request.allow_text_changes or request.mode=='restore')
    try:
        session=EditingSession(project,policy,root=root,session_id=session_id)
        from .visual_ppt_runtime.render import StudioLibreOfficeRenderer
        from .visual_ppt_runtime.tools import EditingTools
        renderer=StudioLibreOfficeRenderer(core.ROOT,project_dir(pid),root/'renders'/uuid.uuid4().hex,
            python_executable=sys.executable)
        row={'session':session,'root':root,'renderer':renderer,
            'tools':EditingTools(session,renderer),'project_id':pid,'base_signature':session.base_sha,
            'run_state':{'running':False,'phase':'idle','message':'尚未运行'}}
        _restore_run_state(row);SESSIONS[f'{pid}:{session_id}']=row
        return {'session_id':session_id,'project_id':pid,'base_revision':project.get('revision'),
            'base_signature':session.base_sha,'mode':request.mode,
            'allow_text_changes':request.allow_text_changes,'slide_ids':slide_ids,
            'source_project_unchanged':True}
    except Exception as exc:
        raise HTTPException(400,str(exc)) from exc

@router.get('/api/projects/{pid}/editing-runtime/sessions/{session_id}')
def session_status(pid:str,session_id:str):
    row=_session(pid,session_id);_restore_run_state(row);session=row['session']
    slide_id=next(iter(session.policy.allowed_slides))
    prepared_path=row['root']/'prepared.json'
    prepared=json.loads(prepared_path.read_text(encoding='utf-8')) if prepared_path.is_file() else None
    return {'session_id':session_id,'project_id':pid,'base_revision':session.base.get('revision'),
        'base_signature':session.base_sha,'draft_signature':session.page(slide_id)['draft_sha'],
        'draft_version':session._version,'transactions':[t.to_dict() for t in session.transactions],
        'prepared':prepared is not None,'prepared_report':prepared,
        'running':bool(row.get('running')),'run_state':row.get('run_state') or {},
        'last_run':row.get('last_run')}

@router.post('/api/projects/{pid}/editing-runtime/sessions/{session_id}/run')
def run_session(pid:str,session_id:str,request:RunRequest):
    row=_session(pid,session_id);_restore_run_state(row);api_key=request.api_key or configured_api_key()
    model=request.model or configured_model()
    provider=configured_api_provider();locked_model=provider in {'opencode','glm','qwen'}
    if locked_model and model!=configured_model():
        label='OpenCode 免费试用' if provider=='opencode' else 'GLM 配置' if provider=='glm' else 'Qwen 配置'
        raise HTTPException(400,f'{label}已锁定为当前配置型号；未发送请求。')
    if locked_model:model=configured_model()
    opencode=provider=='opencode'
    allow_keyless=(opencode and model=='deepseek-v4.1-flash-free')
    if not api_key and not allow_keyless:raise HTTPException(400,'没有配置当前 API Key。')
    if not row['run_lock'].acquire(blocking=False):raise HTTPException(409,'该隔离会话正在运行；可恢复进度或请求停止。')
    try:
        from .visual_ppt_runtime.agent import DeepSeekChatProvider,VisualEditingAgent
        project=row['session'].working
        slide_id=next(iter(row['session'].policy.allowed_slides))
        page=find_slide(project,slide_id)
        design=project_dir(pid)/'images'/page['images']['design']
        run_id=uuid.uuid4().hex;cancel_event=threading.Event();row['cancel_event']=cancel_event
        usage_context_token=set_api_usage_context(task_id=run_id,project_id=pid,slide_id=slide_id,module='editing_runtime')
        from .api_limits import RequestBudget, bind as bind_budget, reset as reset_budget
        spending=RequestBudget(max_cost_cny=request.cost_budget_cny,max_requests=request.request_budget,
            max_bytes=int(request.transfer_budget_mb*1024*1024))
        spending_token=bind_budget(spending)
        row['running']=True;row['run_state']={'run_id':run_id,'running':True,'phase':'starting',
            'message':f'{_active_model_label()} 正在检查并编辑隔离草稿','started_at':time.time()};_save_run_state(row)
        agent=VisualEditingAgent(DeepSeekChatProvider(api_key=api_key,model=model,
            base_url=configured_api_base_url(),allow_keyless=allow_keyless),
            row['tools'],max_rounds=3,max_model_calls=12,max_tool_calls=30)
        result=agent.run(slide_id,design,instruction=request.instruction,cancel_event=cancel_event,
            log_path=row['root']/f'agent-{run_id}.json')
        result['cost_control']=spending.snapshot()
        record_task_outcome(task_id=run_id,project_id=pid,slide_id=slide_id,module='editing_runtime',
            status='cancelled' if result.get('cancelled') else 'complete',repair_success=None,
            duration_ms=round((time.time()-row['run_state']['started_at'])*1000),
            details={'model_calls':result.get('model_calls'),'tool_calls':result.get('tool_calls'),
                     'repair_success':'not_measured_until_candidate_validation'})
        row['last_run']=result;row['run_state']={'run_id':run_id,'running':False,
            'phase':'paused' if result.get('paused_reason') else 'stopped' if result.get('cancelled') else 'complete',
            'message':result.get('paused_reason') or ('已停止；修改保留为隔离事务供复核。' if result.get('cancelled') else '编辑回合完成，事务已保存。'),
            'finished_at':time.time(),'result':result};_save_run_state(row)
        return {'run_id':run_id,'session_id':session_id,'result':result,
            'source_project_unchanged':True,
            'note':'Agent 只操作隔离草稿；工具事务仍待人工验收。'}
    except Exception as exc:
        row['run_state']={'run_id':row.get('run_state',{}).get('run_id'),'running':False,
            'phase':'failed','message':str(exc)[:500],'finished_at':time.time()};_save_run_state(row)
        raise HTTPException(400,str(exc)) from exc
    finally:
        if 'spending_token' in locals():reset_budget(spending_token)
        if 'usage_context_token' in locals():reset_api_usage_context(usage_context_token)
        row['running']=False
        if row.get('run_state',{}).get('running'):
            row['run_state'].update(running=False,phase='stopped' if row['cancel_event'].is_set() else 'interrupted',
                message='已停止或运行中断；请刷新事务状态。',finished_at=time.time());_save_run_state(row)
        row['run_lock'].release()

@router.post('/api/projects/{pid}/editing-runtime/sessions/{session_id}/stop')
def stop_session(pid:str,session_id:str):
    row=_session(pid,session_id);_restore_run_state(row)
    if not row.get('running'):
        return {'stop_requested':False,'running':False,'message':f'当前会话没有正在运行的 {_active_model_label()} 任务。'}
    row['cancel_event'].set();row['run_state'].update(phase='stopping',message='已请求停止；当前模型请求返回后不再执行新编辑。')
    _save_run_state(row)
    return {'stop_requested':True,'running':True,'message':row['run_state']['message']}

@router.post('/api/projects/{pid}/editing-runtime/sessions/{session_id}/transactions/{tx_id}/decision')
def decide_transaction(pid:str,session_id:str,tx_id:str,request:DecisionRequest):
    row=_session(pid,session_id)
    try:return row['session'].mark(tx_id,request.accepted,host_approval=True,
        qa={'decision_source':'explicit_host_review','accepted':request.accepted})
    except Exception as exc:raise HTTPException(400,str(exc)) from exc

@router.post('/api/projects/{pid}/editing-runtime/sessions/{session_id}/prepare')
def prepare_candidate(pid:str,session_id:str):
    row=_session(pid,session_id);root=row['root']
    try:
        current=_project(pid);session=row['session']
        proposal=session.prepared_commit(current)
        if proposal['unreviewed_transaction_ids']:
            raise ValueError('仍有未验收事务；请先逐项接受或拒绝。')
        if not proposal['has_changes']:raise ValueError('没有已接受的修改，未生成候选 PPTX。')
        candidate=proposal['project'];candidate['revision']=current.get('revision',0)+1
        out=root/'prepared'
        if out.exists():shutil.rmtree(out)
        out.mkdir(parents=True)
        safe_json_write(out/'candidate_project.json',candidate)
        from .visual_ppt_runtime.render import StudioLibreOfficeRenderer
        renderer=StudioLibreOfficeRenderer(core.ROOT,project_dir(pid),out/'render',python_executable=sys.executable)
        slide_id=next(iter(session.policy.allowed_slides));rendered=renderer(candidate,slide_id)
        native=check_native_text(candidate,rendered.pptx_path)
        layout=check_saved_layout(candidate,rendered.pptx_path)
        if not native['ok'] or not layout['ok']:
            raise ValueError('候选 PPTX 未通过原生对象或精确布局检查：'+json.dumps(
                {'native_text':native,'saved_layout':layout},ensure_ascii=False))
        shutil.copy2(rendered.pptx_path,out/'candidate.pptx')
        shutil.copy2(rendered.image_path,out/'latest-preview.png')
        report={'status':'prepared','project_id':pid,'session_id':session_id,
            'base_revision':current.get('revision'),'candidate_revision':candidate.get('revision'),
            'base_signature':proposal['base_signature'],'candidate_signature':proposal['candidate_signature'],
            'accepted_transaction_ids':proposal['accepted_transaction_ids'],'pptx_sha256':rendered.pptx_sha256,
            'preview_sha256':rendered.image_sha256,'renderer':rendered.renderer,'slide_id':slide_id,
            'native_text':native,'saved_layout_check':layout,'candidate_pptx':'candidate.pptx',
            'preview':'latest-preview.png','source_project_unchanged':True}
        safe_json_write(out/'prepared.json',report);safe_json_write(root/'prepared.json',report)
        return report
    except Exception as exc:raise HTTPException(400,str(exc)) from exc

@router.get('/api/projects/{pid}/editing-runtime/sessions/{session_id}/files/{filename}')
def session_file(pid:str,session_id:str,filename:str):
    row=_session(pid,session_id)
    if filename not in {'candidate.pptx','latest-preview.png'}:raise HTTPException(404,'文件不存在')
    path=row['root']/'prepared'/filename
    if not path.is_file():raise HTTPException(404,'文件不存在')
    return FileResponse(path,headers={'Cache-Control':'no-store'})

@router.post('/api/projects/{pid}/editing-runtime/sessions/{session_id}/apply')
def apply_candidate(pid:str,session_id:str):
    row=_session(pid,session_id);root=row['root'];current=_project(pid)
    path=project_dir(pid)/'project.json';original=path.read_bytes()
    try:
        report=json.loads((root/'prepared.json').read_text(encoding='utf-8'))
        if report.get('status')!='prepared':raise ValueError('没有可应用的候选')
        from .visual_ppt_runtime.scene import signature
        if signature(current)!=report.get('base_signature'):
            raise ValueError('项目已变化，候选已过期；请重新创建编辑会话。')
        candidate=json.loads((root/'prepared'/'candidate_project.json').read_text(encoding='utf-8'))
        candidate['revision']=current.get('revision',0)
        save_project(candidate)
        try:
            from .server import render_deck
            exported=render_deck(pid,'editable')
            if exported.get('project_revision')!=current.get('revision',0)+1 or not exported.get('artifact_sha256'):
                raise ValueError('正式导出版本与候选不一致')
        except Exception:
            temp=path.with_name('.project.runtime-rollback');temp.write_bytes(original);temp.replace(path)
            raise
        report.update(status='applied',applied_revision=exported['project_revision'],
            applied_artifact_sha256=exported['artifact_sha256'],source_project_unchanged=False)
        safe_json_write(root/'prepared.json',report)
        return {'status':'applied','project_revision':exported['project_revision'],
            'artifact_sha256':exported['artifact_sha256'],'export':exported}
    except Exception as exc:raise HTTPException(400,str(exc)) from exc
