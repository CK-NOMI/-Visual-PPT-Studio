"""Local-first API. All user uploads stay on this machine; no cloud image API is invoked."""
from __future__ import annotations
import asyncio
import csv
from copy import deepcopy
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from urllib.parse import quote
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Body, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image
import numpy as np

from .core import (ROOT, DATA, FILENAME_RE, STYLE_PRESETS, all_prompts,
                   build_prompt_zip, create_project, find_slide, list_projects,
                   project_dir, read_project, save_project, status, update_slide,
                   write_image, safe_json_write)
from .quality import audit, check_native_text, check_saved_layout
from .pptx_native import build as build_python_pptx
from .visual_ppt_runtime.export_bridge import build_with_extensions as build_runtime_pptx
from .ai import (AIError, analyze_visual, analyze_structure, recognize_formula_crops, calibrate_text_layout,
                 configured_api_key, configured_api_base_url, configured_api_provider, configured_model,
                 generate_image_prompts, generate_plan, visual_agent_turn,
                 generate_style, review_image_pair, refine_clean_prompt, review_visual, call_deepseek)
from . import reconstruction as rc
from .equations import to_mathml
from .text_layout import layout_elements, layout_project
from .render_acceptance import wps_environment, layout_manifest, import_wps_render
from . import core
from .api_usage import set_context as set_api_usage_context, reset_context as reset_api_usage_context
from .api_usage import summary as api_usage_summary, public_row as public_usage_row, record_task_outcome

# Keep API keys in environment only, never in project.json/zip/logs.
def load_local_env():
    path=ROOT/'.env'
    if not path.exists():return
    for ln in path.read_text(encoding='utf-8').splitlines():
        if '=' in ln and not ln.lstrip().startswith('#'):
            a,b=ln.split('=',1)
            if a.strip() and a.strip() not in os.environ:
                os.environ[a.strip()]=b.strip().strip('"').strip("'")
load_local_env()
app=FastAPI(title='Visual PPT Studio',version='0.3.0',description='Original local-first numbered design-to-editable-PPTX workbench')
app.mount('/static/fonts',StaticFiles(directory=ROOT/'studio'/'assets'/'fonts'),name='fonts')
from .editing_runtime_api import router as editing_runtime_router
app.include_router(editing_runtime_router)

@app.middleware('http')
async def api_usage_context_middleware(request:Request,call_next):
    """Bind model calls to the originating local action without reading its body."""
    path=request.url.path
    request_id=uuid.uuid4().hex
    task_id=request_id
    project_match=re.search(r'/api/projects/([0-9a-f]{12})(?:/|$)',path)
    slide_match=re.search(r'/slides/(S\d{3})(?:/|$)',path)
    module='other'
    for marker,label in (('visual-agent','visual_agent'),('editing-runtime','editing_runtime'),
                         ('smart-candidates','smart_repair'),('recognize-formulas','formula_recognition'),
                         ('calibrate-text','text_calibration'),('analyze-structure','structure_analysis'),
                         ('/analyze','visual_analysis'),('/ocr','ocr'),('prompts','prompt_generation'),
                         ('/plan/ai','planning'),('/style/ai','style_generation'),('/repair/','repair'),
                         ('review-image-pair','image_pair_review'),('refine-clean','clean_prompt_refinement'),
                         ('/api/ai/test','api_test')):
        if marker in path:module=label;break
    if path.startswith('/api/usage/'):module='usage_dashboard'
    context_token=set_api_usage_context(request_id=request_id,task_id=task_id,
        project_id=project_match.group(1) if project_match else None,
        slide_id=slide_match.group(1) if slide_match else None,module=module)
    try:
        response=await call_next(request)
        response.headers['X-Request-ID']=request_id
        return response
    finally:
        reset_api_usage_context(context_token)

@app.get('/api/usage/summary')
def get_api_usage_summary(project_id:str|None=None,task_id:str|None=None):
    if project_id and not re.fullmatch(r'[0-9a-f]{12}',project_id):raise HTTPException(400,'project_id 无效')
    if task_id and not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,80}',task_id):raise HTTPException(400,'task_id 无效')
    return api_usage_summary(project_id=project_id,task_id=task_id)

@app.delete('/api/usage/records')
def clear_api_usage_records(project_id:str|None=None):
    """Clear local API usage and cost telemetry only."""
    if project_id and not re.fullmatch(r'[0-9a-f]{12}',project_id):raise HTTPException(400,'project_id 无效')
    from .api_usage import clear_records
    return clear_records(project_id=project_id)

@app.get('/api/usage/requests')
def get_api_usage_requests(project_id:str|None=None,task_id:str|None=None,limit:int=100):
    if project_id and not re.fullmatch(r'[0-9a-f]{12}',project_id):raise HTTPException(400,'project_id 无效')
    if task_id and not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,80}',task_id):raise HTTPException(400,'task_id 无效')
    from .api_usage import _read
    rows=_read()
    if project_id:rows=[row for row in rows if row.get('project_id')==project_id]
    if task_id:rows=[row for row in rows if row.get('task_id')==task_id]
    return {'requests':[public_usage_row(row) for row in rows[-max(1,min(limit,500)):]]}
_visual_agent_runs:dict[str,dict[str,Any]]={}
_visual_agent_jobs:dict[str,dict[str,Any]]={}
_visual_agent_apply_locks:dict[str,threading.Lock]={}
_editing_runtime_sessions:dict[str,dict[str,Any]]={}
_stage_preview_workers:dict[tuple[str,str],Any]={}
_stage_preview_cache:dict[tuple[str,str],dict[str,Any]]={}

@app.on_event('shutdown')
def close_stage_preview_workers():
    for worker in list(_stage_preview_workers.values()):
        try:worker.close()
        except Exception:pass
    _stage_preview_workers.clear()

@app.get('/api/projects/{pid}/fonts/{identity}.ttf')
def project_font_file(pid:str,identity:str):
    """Only serve a registered face used by this project, never arbitrary paths."""
    from fontTools.ttLib import TTFont
    p=p_or_404(pid)
    if not re.fullmatch(r'[0-9a-f]{16}',identity):raise HTTPException(404,'非法字体编号')
    face=next((e.get('text_layout',{}).get('font') for s in p['slides'] for e in s.get('elements',[])
               if e.get('text_layout',{}).get('font',{}).get('file_identity')==identity),None)
    if not face:
        # A pure candidate has not been persisted. Restrict it to our actual registry.
        from .fonts import font_registry
        for candidate in font_registry():
            path=Path(candidate['path'])
            key=hashlib.sha256((str(path)+str(path.stat().st_mtime_ns)+str(candidate['index'])).encode()).hexdigest()[:16]
            if key==identity:face=candidate;break
    if not face:raise HTTPException(404,'字体文件不存在')
    cache=project_dir(pid)/'build'/'browser_fonts';cache.mkdir(exist_ok=True)
    file=cache/(identity+'.ttf')
    if not file.is_file():
        with TTFont(face['path'],fontNumber=face['index']) as font:font.save(file)
    return FileResponse(file,media_type='font/ttf',headers={'Cache-Control':'public, max-age=31536000, immutable'})

class ProjectCreate(BaseModel):
    title:str=Field(min_length=1,max_length=180)
    topic:str=Field(min_length=1,max_length=10000)
    pages:int=Field(default=8,ge=1,le=60)
    style:str='editorial'
    audience:str='通用受众'

class Flag(BaseModel):locked:bool
class AIRequest(BaseModel):
    api_key:str|None=None
    model:str|None=None
    # Keep visual feedback as structured JSON. A scalar character cap silently
    # blocked whole-slide requests; transport and validation use the HTTP JSON
    # body, while the full round history remains archived in project memory.
    optimization_feedback:dict[str,Any]|str=Field(default_factory=dict)
    optimization_round:int=Field(default=1,ge=1,le=10000)
    layout_observation:dict[str,Any]|None=None
class ProviderSelection(BaseModel):provider:str
class RepairAIRequest(AIRequest):
    slides:list[str]|None=None
class VisualAgentRequest(AIRequest):
    run_id:str|None=None
    slide_id:str
    mode:str='restore'
    instruction:str=Field(default='',max_length=2000)
    max_iterations:int=Field(default=3,ge=1,le=3)
    max_attempts_per_issue:int=Field(default=10,ge=1,le=10)
    confidence_threshold:float=Field(default=.85,ge=.5,le=1)
    diagnose_only:bool=False
    diagnosis_token:str|None=None
    cost_budget_cny:float=Field(default=1.0,ge=.1,le=100)
    request_budget:int=Field(default=12,ge=1,le=100)
    transfer_budget_mb:float=Field(default=64,ge=1,le=1024)
    resume_from_candidate:bool=False
    # Continue from a same-page isolated candidate without writing it to the
    # formal project. The token is independently validated against project,
    # slide and the unchanged formal base signature.
    base_candidate_token:str|None=None
class VisualAgentIssueDecisionRequest(BaseModel):
    status:str='fixed'
class VisualAgentApplyRequest(BaseModel):
    confirm_review:bool=False
class EditingRuntimeSessionRequest(BaseModel):
    slide_ids:list[str]=Field(min_length=1,max_length=60)
    mode:str='restore'
    allow_text_changes:bool=False
class EditingRuntimeRunRequest(BaseModel):
    instruction:str=Field(default='',max_length=4000)
    model:str|None=None
class EditingRuntimeDecisionRequest(BaseModel):
    accepted:bool
class EditingRuntimeApplyRequest(BaseModel):
    slide_id:str|None=None
class RefineCleanPromptRequest(AIRequest):
    current_prompt:str=Field(default='',max_length=10000)
    review:dict[str,Any]
class FormulaRecognitionRequest(AIRequest):
    candidates:list[dict[str,Any]]=Field(default_factory=list,max_length=8)
class BatchExport(BaseModel):project_ids:list[str];mode:str='editable'
class MaskRequest(BaseModel):rects:list[list[float]]
class RegionPayload(BaseModel):regions:list[dict]
class RebuildRequest(BaseModel):
    regions:list[dict]
    mask_mode:str='ink'
    method:str='telea'
    padding:int=4
    threshold:int=32
    source_role:str='design'
class CropRequest(BaseModel):
    box:list[float]
    make_transparent:bool=False
    tolerance:int=26
class FormulaPreviewRequest(BaseModel):latex:str=Field(min_length=1,max_length=2000)

@app.post('/api/formula/preview')
def formula_preview(request:FormulaPreviewRequest):
    try:return {'mathml':to_mathml(request.latex)}
    except Exception as e:fail(e)

def fail(e):
    if isinstance(e,FileNotFoundError):raise HTTPException(404,str(e))
    if isinstance(e,(ValueError,AIError)):raise HTTPException(400,str(e))
    raise e

def p_or_404(pid:str):
    try:return read_project(pid)
    except Exception as e:fail(e)

def rel_media(p,sid,role):
    fn=find_slide(p,sid)['images'].get(role)
    if not fn:raise ValueError(f'{sid} 没有上传 {role}')
    return project_dir(p['id'])/'images'/fn

@app.get('/api/health')
def health():
    def check(*cmd):
        try:
            r=subprocess.run(cmd,capture_output=True,text=True,timeout=6)
            return {'ok':r.returncode==0,'info':(r.stdout+r.stderr).strip()[:220]}
        except Exception as e:return {'ok':False,'info':str(e)}
    office=office_renderer_binary()
    try:deepseek_ready=bool(configured_api_key())
    except AIError:deepseek_ready=False
    return {'version':'0.3.0','data_dir':str(DATA),'deepseek_configured':deepseek_ready,
            'provider':configured_api_provider(),'base_url':configured_api_base_url(),
            'model':configured_model(),'python':sys.version.split()[0],
            'node':check('node','--version'),
            'pptxgenjs':check('node',str(ROOT/'renderer'/'render.js'),'--health'),
            'default_pptx_backend':'python-pptx (无需Node依赖)',
            'libreoffice':check(office,'--version') if office else {'ok':False,'info':'未找到libreoffice或soffice'},
            'office_renderer_binary':office,'styles':STYLE_PRESETS,
            'offline_image_mode':True,'image_api_calls':False,
            'reconstruction':rc.available_ocr()}

_provider_settings_lock=threading.Lock()

def _provider_settings_payload():
    from .ai import configured_api_key
    active=configured_api_provider()
    profiles=[]
    for name,label,model,key_vars in (
        ('deepseek','DeepSeek','',('DEEPSEEK_API_KEY','PPT_STUDIO_DEEPSEEK_API_KEY_FILE','DEEPSEEK_API_KEY_FILE')),
        ('glm','智谱 GLM','glm-4.6v-flash',('PPT_STUDIO_GLM_API_KEY','ZHIPU_API_KEY','GLM_API_KEY',
                                             'PPT_STUDIO_GLM_API_KEY_FILE','GLM_API_KEY_FILE','ZHIPU_API_KEY_FILE')),
        ('qwen','通义千问','qwen3.5-flash',('PPT_STUDIO_QWEN_API_KEY','DASHSCOPE_API_KEY','QWEN_API_KEY',
                                             'PPT_STUDIO_QWEN_API_KEY_FILE','QWEN_API_KEY_FILE','DASHSCOPE_API_KEY_FILE'))):
        ready=any(os.environ.get(k) for k in key_vars if not k.endswith('_FILE'))
        if not ready:
            ready=any(Path(os.environ[k]).expanduser().is_file() for k in key_vars
                      if k.endswith('_FILE') and os.environ.get(k))
        profiles.append({'id':name,'label':label,'model':configured_model() if active==name else
                         (os.environ.get('PPT_STUDIO_DEEPSEEK_MODEL') or os.environ.get('DEEPSEEK_MODEL','deepseek-flash') if name=='deepseek' else model),
                         'configured':bool(ready)})
    try:current_key_ready=bool(configured_api_key())
    except AIError:current_key_ready=False
    return {'provider':active,'model':configured_model(),'profiles':profiles,
            'current_configured':current_key_ready,
            'opencode_external_available':False,
            'opencode_message':'OpenCode 免费档只允许在 OpenCode 客户端内使用；Visual PPT Studio 外部请求会被拒绝（HTTP 403）。'}

@app.get('/api/settings/model-provider')
def model_provider_settings():return _provider_settings_payload()

@app.post('/api/settings/model-provider')
def set_model_provider(x:ProviderSelection):
    provider=x.provider.strip().lower()
    if provider not in {'deepseek','glm','qwen'}:raise HTTPException(400,'仅支持 DeepSeek、GLM 与 Qwen。')
    path=ROOT/'.env'
    try:
        with _provider_settings_lock:
            lines=path.read_text(encoding='utf-8').splitlines() if path.exists() else []
            replaced=False;updated=[]
            for line in lines:
                if line.split('=',1)[0].strip()=='PPT_STUDIO_API_PROVIDER':
                    updated.append('PPT_STUDIO_API_PROVIDER='+provider);replaced=True
                else:updated.append(line)
            if not replaced:updated.append('PPT_STUDIO_API_PROVIDER='+provider)
            payload=('\n'.join(updated)+'\n').encode('utf-8')
            fd,temp_name=tempfile.mkstemp(prefix='.env.',suffix='.tmp',dir=ROOT)
            try:
                os.fchmod(fd,0o600)
                with os.fdopen(fd,'wb') as handle:handle.write(payload);handle.flush();os.fsync(handle.fileno())
                os.replace(temp_name,path)
            finally:
                try:os.unlink(temp_name)
                except FileNotFoundError:pass
            os.environ['PPT_STUDIO_API_PROVIDER']=provider
    except OSError as exc:raise HTTPException(500,'无法保存本机模型选择，请检查项目目录权限。') from exc
    return _provider_settings_payload()

@app.get('/api/projects')
def projects():return list_projects()

@app.post('/api/projects')
def create(x:ProjectCreate):
    try:return create_project(**x.model_dump())
    except Exception as e:fail(e)

@app.delete('/api/projects/{pid}')
def delete(pid:str):
    try:
        directory=project_dir(pid)
        shutil.rmtree(directory)
        return {'id':pid,'deleted':True}
    except FileNotFoundError as e:raise HTTPException(404,str(e))
    except Exception as e:fail(e)

@app.get('/api/projects/{pid}')
def get(pid:str):return p_or_404(pid)

def _calibration_memory_path(pid:str)->Path:
    return project_dir(pid)/'build'/'calibration_memory.json'

@app.get('/api/projects/{pid}/calibration-memory')
def get_calibration_memory(pid:str):
    p_or_404(pid);path=_calibration_memory_path(pid)
    if not path.is_file():return {'version':1,'history':[],'playbook':[]}
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict):raise ValueError('memory must be an object')
        return {'version':1,'history':data.get('history',[])[-80:],'playbook':data.get('playbook',[])[-20:]}
    except Exception as e:raise HTTPException(500,'优化记忆文件损坏：'+str(e)[:200])

@app.post('/api/projects/{pid}/calibration-memory')
def append_calibration_memory(pid:str,entry:dict=Body(...)):
    p_or_404(pid)
    sid=str(entry.get('slide_id',''))
    if not re.fullmatch(r'S\d{3}',sid):raise HTTPException(400,'非法记忆页码')
    outcome=entry.get('outcome')
    if outcome not in ('baseline','improved','no_improvement','rollback'):raise HTTPException(400,'非法优化记忆结果')
    def score_value(key):
        value=entry.get(key)
        if value is None:return None
        if not isinstance(value,(int,float)) or isinstance(value,bool) or not 0<=value<=100:raise HTTPException(400,f'{key} 必须在0–100')
        return round(float(value),2)
    properties=entry.get('properties',[])
    allowed={'box','size','font','eastAsianFont','color','bold','italic','align','char_spacing_pt','line_spacing','vertical_align','margin_left_pt','margin_right_pt','margin_top_pt','margin_bottom_pt','paragraph_space_before_pt','paragraph_space_after_pt'}
    if not isinstance(properties,list) or any(not isinstance(x,str) or x not in allowed for x in properties):raise HTTPException(400,'优化属性列表无效')
    issues=entry.get('issues',[])
    if not isinstance(issues,list) or any(not isinstance(x,str) for x in issues):raise HTTPException(400,'issues必须为字符串数组')
    corrections=entry.get('corrections',[])
    if not isinstance(corrections,list):raise HTTPException(400,'corrections必须为数组')
    safe_corrections=[]
    for item in corrections[:256]:
        if not isinstance(item,dict) or not isinstance(item.get('id'),str):continue
        box=item.get('box')
        if box is not None and (not isinstance(box,list) or len(box)!=4 or any(not isinstance(v,(int,float)) or isinstance(v,bool) or not 0<=v<=1 for v in box)):
            box=None
        style=item.get('style',{})
        if not isinstance(style,dict):style={}
        style={k:v for k,v in style.items() if k in allowed and k!='box' and isinstance(v,(str,int,float,bool))}
        safe_corrections.append({'id':item['id'],'target_text':str(item.get('target_text',''))[:500],
                                 'box':box,'style':style,'reason':str(item.get('reason',''))[:180]})
    def diff_rows(key):
        rows=entry.get(key,[])
        if not isinstance(rows,list):return []
        return [json.loads(json.dumps(row,ensure_ascii=False,default=str)) for row in rows[:256] if isinstance(row,dict)]
    insight=entry.get('prompt_insight','')
    if not isinstance(insight,str):raise HTTPException(400,'prompt_insight必须为字符串')
    path=_calibration_memory_path(pid);path.parent.mkdir(parents=True,exist_ok=True)
    try:data=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'version':1,'history':[],'playbook':[]}
    except Exception:data={'version':1,'history':[],'playbook':[]}
    record={'slide_id':sid,'round':max(0,min(10000,int(entry.get('round',0)))),'before':score_value('before'),
            'after':score_value('after'),'delta':(score_value('after')-score_value('before')) if entry.get('after') is not None and entry.get('before') is not None else None,
            'outcome':outcome,'properties':list(dict.fromkeys(properties)),'issues':[x[:600] for x in issues[:256]],
            'corrections':safe_corrections,'differences_before':diff_rows('differences_before'),
            'differences_after':diff_rows('differences_after'),'candidate_applied':bool(entry.get('candidate_applied')),
            'at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
    data.setdefault('history',[]).append(record);data['history']=data['history'][-80:]
    if outcome=='improved' and insight.strip():
        data.setdefault('playbook',[]).append({'slide_id':sid,'round':record['round'],'delta':record['delta'],
            'properties':record['properties'],'guidance':insight.strip()[:300],'validated_by_render':True,'at':record['at']})
        data['playbook']=data['playbook'][-20:]
    data['version']=1;safe_json_write(path,data)
    return {'saved':True,'history_count':len(data['history']),'playbook_count':len(data.get('playbook',[])),'playbook_version':len(data.get('playbook',[]))}

@app.post('/api/projects/{pid}/lock')
def lock(pid:str,x:Flag):
    p=p_or_404(pid)
    if not x.locked and any(s['images'] for s in p['slides']):
        raise HTTPException(400,'已有编号素材，不允许解锁重新规划页数；可直接编辑当前页内容/坐标。')
    p['locked']=x.locked;save_project(p);return {'locked':x.locked,'status':status(p)}

@app.put('/api/projects/{pid}/slides/{sid}')
def slide_save(pid:str,sid:str,changes:dict=Body(...)):
    p=p_or_404(pid)
    try:
        context=changes.get('_calibration')
        if context is None:return update_slide(p,sid,changes)
        # Stage 03 records actual saved parameters, including layout changes,
        # rather than treating model suggestions as applied modifications.
        if context.get('expected_revision')!=p['revision']:
            raise ValueError('项目版本已变化，样式候选未应用，请重新读取当前项目。')
        before=json.loads(json.dumps(p));folder=project_dir(pid)/'build'/'calibration_changes'
        key=f'r{p["revision"]}_{sid}_{time.time_ns()}'
        safe_json_write(folder/(key+'_before.json'),before)
        if context.get('phase')=='rollback':
            # A rollback restores the previously saved layout verbatim. Sending
            # it through layout_elements again can move its baseline using the
            # rejected candidate's metrics as the previous layout.
            backup=context.get('restore_backup','')
            if not isinstance(backup,str) or not re.fullmatch(r'r\d+_S\d{3}_\d+_before\.json',backup):
                raise ValueError('缺少可信样式备份，未执行回滚。')
            snapshot=json.loads((folder/backup).read_text())
            restored=find_slide(snapshot,sid)
            if snapshot['id']!=pid or snapshot['revision']>=p['revision'] or restored['elements']!=changes.get('elements'):
                raise ValueError('回滚参数与修改前备份不一致，未覆盖当前项目。')
            p['slides'][next(i for i,s in enumerate(p['slides']) if s['id']==sid)]=restored
            save_project(p);result=restored
        else:
            result=update_slide(p,sid,changes)
        old={e['id']:e for e in find_slide(before,sid)['elements']}
        new={e['id']:e for e in result['elements']}
        rows=[{'id':eid,'before':old.get(eid),'after':new.get(eid)}
              for eid in sorted(old.keys()|new.keys()) if old.get(eid)!=new.get(eid)]
        record={'slide_id':sid,'phase':str(context.get('phase','apply'))[:40],
                'round':context.get('round',0),'before_revision':before['revision'],
                'after_revision':p['revision'],'changes':rows,'applied':bool(rows),
                'before_sha256':hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest(),
                'after_sha256':hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest(),
                'backup':key+'_before.json'}
        safe_json_write(folder/(key+'_after.json'),p)
        safe_json_write(folder/(key+'.json'),record)
        return {**result,'calibration_audit':record}
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/text-layout')
def text_layout_candidate(pid:str,sid:str,body:dict=Body(...)):
    """Pure proposal: one layout for browser confirmation and subsequent PPTX."""
    p=p_or_404(pid)
    try:
        s=find_slide(p,sid)
        regions=body.get('regions')
        elements=body.get('elements',s['elements']) if regions is None else [
            {**r,'kind':r.get('kind','text')} for r in regions]
        if not isinstance(elements,list) or len(elements)>1000:
            raise ValueError('需要最多1000个对象的列表')
        previous=s['elements']
        if regions is not None:
            # OCR IDs are positional (text_001, text_002, …) and can shift
            # between recognition runs. Reusing history by ID alone can move a
            # new caption to an unrelated old object's position. Preserve an
            # earlier manual layout only when ID, normalized text, kind, and
            # source geometry all still identify the same visual object.
            def key_text(value):return re.sub(r'\s+','',str(value or '')).casefold()
            prior_by_id={e.get('id'):e for e in previous}
            compatible=[]
            for item in elements:
                run_id=re.sub(r'[^a-zA-Z0-9_.:-]','',str(item.get('run_id') or item.get('id') or ''))[-70:]
                old=prior_by_id.get(item.get('id')) or prior_by_id.get('ocr_'+run_id)
                if not old or key_text(item.get('text'))!=key_text(old.get('text')) or item.get('kind','text')!=old.get('kind','text'):
                    continue
                new_box=item.get('ocr_box') or item.get('source_box') or item.get('box')
                old_box=old.get('ocr_box') or old.get('source_box') or old.get('box')
                if not isinstance(new_box,list) or not isinstance(old_box,list) or len(new_box)!=4 or len(old_box)!=4:
                    continue
                try:new_box=list(map(float,new_box));old_box=list(map(float,old_box))
                except (TypeError,ValueError):continue
                if max(abs(new_box[i]-old_box[i]) for i in range(4))<=.025:
                    # Regions keep their OCR row id (for UI selection), while
                    # persisted native objects use the stable run id. Alias the
                    # history object's id so layout_elements can carry forward
                    # the user's saved geometry/style when the text matches.
                    compatible.append({**old,'id':item.get('id')})
            previous=compatible
        candidate,report=layout_elements(elements,p['design'],sid,previous)
        return {'elements':candidate,'regions':candidate if regions is not None else None,
                'layout':report,'saved':False,'notice':'统一字体测量的布局候选，确认保存后用于预览和导出；未完成 Office 验收。'}
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/plan/offline')
def offline_plan(pid:str):
    p=p_or_404(pid)
    if p['locked']:raise HTTPException(400,'先解锁规划才能重新生成')
    return {'slides':p['slides'],'notice':'请编辑逐页内容再锁定。离线模式不生成真实研究事实。'}

@app.post('/api/projects/{pid}/plan/ai')
async def ai_plan(pid:str,request:AIRequest):
    p=p_or_404(pid)
    if p['locked']:raise HTTPException(400,'规划已锁定，不能批量覆盖')
    try:
        suggested=await generate_plan(p,request.api_key,request.model)
        return {'proposal':suggested,'notice':'仅为建议，点击逐页应用后再锁定规划；不自动覆盖。'}
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/style/ai')
async def ai_style(pid:str,request:AIRequest):
    try:return {'proposal':await generate_style(p_or_404(pid),request.api_key,request.model),
                'notice':'不自动改变已上传图片或页面。'}
    except Exception as e:fail(e)

@app.put('/api/projects/{pid}/design')
def design_save(pid:str, body:dict=Body(...)):
    p=p_or_404(pid)
    from copy import deepcopy
    previous_slides=deepcopy(p['slides'])
    old_design=dict(p['design'])
    for k in ['bg','ink','accent','muted']:
        if k in body:
            v=str(body[k]).replace('#','')
            if not re.fullmatch('[a-fA-F0-9]{6}',v):raise HTTPException(400,'颜色格式应为6位hex')
            p['design'][k]=v.upper()
    if 'font' in body:p['design']['font']=str(body['font'])[:90]
    # Propagate known theme tokens to existing objects, without overriding custom colors.
    for sl in p['slides']:
        if sl['background']==old_design['bg']:sl['background']=p['design']['bg']
        for e in sl.get('elements',[]):
            st=e.get('style',{})
            for k in ('ink','accent','muted'):
                for target in ('color','fill','stroke'):
                    if st.get(target)==old_design[k]:st[target]=p['design'][k]
            if st.get('font')==old_design['font']:st['font']=p['design']['font']
    for sl,previous in zip(p['slides'],previous_slides):
        sl['elements'],sl['text_structure']=layout_elements(sl['elements'],p['design'],sl['id'],previous['elements'])
    save_project(p);return p['design']

@app.get('/api/projects/{pid}/prompts')
def prompts(pid:str):
    p=p_or_404(pid)
    if not p['locked']:raise HTTPException(400,'请先锁定规划，确保S001…编号稳定')
    return {'prompts':all_prompts(p),'status':status(p)}

@app.post('/api/projects/{pid}/slides/{sid}/prompts/ai')
async def ai_image_prompts(pid:str,sid:str,request:AIRequest):
    p=p_or_404(pid)
    if not p['locked']:raise HTTPException(400,'先锁定规划，确保图片和页码对应。')
    try:return await generate_image_prompts(p,find_slide(p,sid),request.api_key,request.model)
    except Exception as e:fail(e)

@app.get('/api/projects/{pid}/prompts.zip')
def prompt_zip(pid:str):
    p=p_or_404(pid)
    if not p['locked']:raise HTTPException(400,'请先锁定逐页规划')
    f=build_prompt_zip(p)
    return FileResponse(f,media_type='application/zip',filename='PPT_逐页手动生图提示词.zip')

@app.post('/api/projects/{pid}/upload')
async def upload(pid:str,slide_id:str=Form(...),role:str=Form(...),file:UploadFile=File(...)):
    p=p_or_404(pid)
    try:
        data=await file.read(26*1024*1024)
        return write_image(p,slide_id,role,data,file.filename or 'unknown')
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/images/{role}/remove')
def remove_uploaded_image(pid:str,sid:str,role:str):
    """Move one explicitly selected upload to project-local undo storage."""
    p=p_or_404(pid)
    try:
        s=find_slide(p,sid)
        if role not in ('design','clean') and not re.fullmatch(r'asset_\d{2}',role):
            raise ValueError('图片角色不合法')
        filename=s.get('images',{}).get(role)
        if not filename:raise ValueError(f'{sid} 没有已上传的 {role} 图片')
        if not re.fullmatch(rf'{re.escape(sid)}_{re.escape(role)}\.(?:png|jpg|jpeg|webp)',filename,re.I):
            raise ValueError('项目记录中的图片名称不合法，拒绝移除')
        source=project_dir(pid)/'images'/filename
        if not source.is_file():raise FileNotFoundError('图片文件不存在，项目记录可能已过期')
        token=__import__('uuid').uuid4().hex
        trash=project_dir(pid)/'build'/'upload_trash'
        trash.mkdir(parents=True,exist_ok=True)
        destination=trash/f'{token}_{filename}'
        os.replace(source,destination)
        manifest={'token':token,'slide_id':sid,'role':role,'filename':filename,
                  'image_metadata':s.get('image_metadata',{}).get(role),
                  'removed_at':time.time()}
        (trash/f'{token}.json').write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
        s['images'].pop(role,None);s.setdefault('image_metadata',{}).pop(role,None)
        if role=='design' and s.get('image_metadata',{}).get('clean'):
            s['image_metadata']['clean']['stale_source']=True
        save_project(p)
        return {'removed':filename,'undo_token':token,'slide_id':sid,'role':role,'status':status(p)}
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/uploads/{token}/restore')
def restore_uploaded_image(pid:str,token:str):
    p=p_or_404(pid)
    try:
        if not re.fullmatch(r'[0-9a-f]{32}',token):raise ValueError('撤回编号不合法')
        trash=project_dir(pid)/'build'/'upload_trash'
        manifest_path=trash/f'{token}.json'
        if not manifest_path.is_file():raise FileNotFoundError('找不到可撤回的图片记录')
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        sid,role,filename=manifest['slide_id'],manifest['role'],manifest['filename']
        if not re.fullmatch(r'S\d{3}',sid) or (role not in ('design','clean') and not re.fullmatch(r'asset_\d{2}',role)):
            raise ValueError('撤回记录中的页面或图片角色不合法')
        if not re.fullmatch(rf'{re.escape(sid)}_{re.escape(role)}\.(?:png|jpg|jpeg|webp)',filename,re.I):
            raise ValueError('撤回记录中的文件名不合法')
        s=find_slide(p,sid)
        if s.get('images',{}).get(role):raise ValueError(f'{sid} 的 {role} 已有新图片；请先移除新图片再撤回')
        image_path=trash/f'{token}_{filename}'
        if not image_path.is_file():raise FileNotFoundError('撤回图片文件不存在')
        destination=project_dir(pid)/'images'/filename
        os.replace(image_path,destination)
        s.setdefault('images',{})[role]=filename
        s.setdefault('image_metadata',{})[role]=manifest.get('image_metadata') or {}
        if role=='clean':
            design_sha=s.get('image_metadata',{}).get('design',{}).get('sha256')
            clean_meta=s['image_metadata'][role]
            clean_meta['stale_source']=clean_meta.get('source_design_sha256')!=design_sha
        elif role=='design' and s.get('image_metadata',{}).get('clean'):
            clean_meta=s['image_metadata']['clean']
            clean_meta['stale_source']=clean_meta.get('source_design_sha256')!=s['image_metadata'][role].get('sha256')
        manifest_path.unlink()
        save_project(p)
        return {'restored':filename,'slide_id':sid,'role':role,'status':status(p)}
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/upload-batch')
async def upload_batch(pid:str,files:list[UploadFile]=File(...)):
    p=p_or_404(pid)
    if not p['locked']:raise HTTPException(400,'先锁定每页规划')
    if len(files)>200:raise HTTPException(400,'单次最多200张')
    # Validate every filename before writing any file.
    normalized=[];seen=set()
    for f in files:
        name=Path(f.filename or '').name
        m=FILENAME_RE.fullmatch(name)
        if not m:raise HTTPException(400,f'{name} 命名错误，示例 S001_design.png')
        sid,role=m.group(1).upper(),m.group(2).lower()
        try:find_slide(p,sid)
        except Exception:raise HTTPException(400,f'{name} 的页码不存在')
        if (sid,role) in seen:raise HTTPException(400,f'本次上传存在同一页同一角色重复文件：{sid}_{role}')
        seen.add((sid,role));normalized.append((sid,role,f))
    receipts=[]
    for sid,role,f in normalized:
        try:receipts.append(write_image(p,sid,role,await f.read(26*1024*1024),f.filename or ''))
        except Exception as e:fail(e)
    return {'uploaded':receipts,'status':status(p)}

@app.get('/media/{pid}/{filename}')
def media(pid:str,filename:str):
    if not re.fullmatch(r'S\d{3}_(?:design|clean|asset_\d{2})\.(?:png|jpg|jpeg|webp)',filename,re.I):
        raise HTTPException(404,'非法图片名称')
    try:
        path=project_dir(pid)/'images'/filename
        if not path.is_file():raise FileNotFoundError('媒体不存在')
        return FileResponse(path,headers={'Cache-Control':'no-cache'})
    except Exception as e:fail(e)


# v0.2 Reconstruction Lab: all operations are explicit, local and inspectable.
@app.get('/api/projects/{pid}/slides/{sid}/reconstruction')
def reconstruction_info(pid:str,sid:str):
    p=p_or_404(pid)
    try:
        rc.source_for(p,sid)
        reportfile=rc.lab_dir(pid)/f'{sid}_candidate_report.json'
        valid_candidate=False
        if reportfile.is_file():
            try:
                met=json.loads(reportfile.read_text(encoding='utf-8'))
                valid_candidate=(rc.candidate_is_current(p,sid,met) and
                                 (rc.lab_dir(pid)/f'{sid}_candidate.png').is_file())
            except Exception:pass
        return {'ocr':rc.get_regions(p,sid),'engines':rc.available_ocr(),
                'candidate_exists':valid_candidate,
                'coverage':rc.reconstruction_coverage(p,sid),
                'source_id':sid}
    except Exception as e:fail(e)

def _reconstruction_progress(p:dict) -> list[dict]:
    """Return slide-scoped second-stage readiness and durable confirmations."""
    root=project_dir(p['id'])/'build'/'reconstruction_confirmations'
    result=[]
    for s in p.get('slides',[]):
        sid=s['id'];images=s.get('images',{});meta=s.get('image_metadata',{})
        dm=meta.get('design') or {};cm=meta.get('clean') or {}
        ready=bool(images.get('design') and images.get('clean') and not cm.get('stale_source')
                   and cm.get('source_design_sha256') and cm.get('source_design_sha256')==dm.get('sha256'))
        native=[e for e in s.get('elements',[]) if str(e.get('id','')).startswith('ocr_') and e.get('kind')=='text' and str(e.get('text') or '').strip()]
        path=root/f'{sid}.json';confirmed=False;legacy=False
        if ready and path.is_file():
            try:
                record=json.loads(path.read_text(encoding='utf-8'))
                design_path=project_dir(p['id'])/'images'/images['design']
                clean_path=project_dir(p['id'])/'images'/images['clean']
                design_hash=rc.sha_file(design_path);clean_hash=rc.sha_file(clean_path)
                confirmed=(record.get('design_sha256')==design_hash and record.get('clean_sha256')==clean_hash)
            except Exception:
                confirmed=False
        # Projects processed before the per-slide confirmation ledger already
        # contain saved OCR text objects; recognize those as completed.
        if ready and not confirmed and not path.exists() and native:
            confirmed=True;legacy=True
        result.append({'slide_id':sid,'ready':ready,'confirmed':confirmed,'legacy':legacy,
                       'native_text_count':len(native),'has_design':bool(images.get('design')),
                       'has_clean':bool(images.get('clean'))})
    return result

@app.get('/api/projects/{pid}/reconstruction-progress')
def reconstruction_progress(pid:str):
    return {'pages':_reconstruction_progress(p_or_404(pid))}

@app.post('/api/projects/{pid}/slides/{sid}/confirm-reconstruction')
def confirm_reconstruction(pid:str,sid:str):
    p=p_or_404(pid)
    try:
        s=find_slide(p,sid);images=s.get('images',{});meta=s.get('image_metadata',{})
        dm=meta.get('design') or {};cm=meta.get('clean') or {}
        if not images.get('design') or not images.get('clean') or cm.get('stale_source') or cm.get('source_design_sha256')!=dm.get('sha256'):
            raise ValueError(f'{sid} 的 design / clean 配对无效，不能记为第二步完成')
        native=[e for e in s.get('elements',[]) if str(e.get('id','')).startswith('ocr_') and e.get('kind')=='text' and str(e.get('text') or '').strip()]
        # The second-stage confirmation is only recorded after the slide PUT
        # has persisted the native text boxes. Zero is allowed for clean-only repair.
        design_path=project_dir(pid)/'images'/images['design'];clean_path=project_dir(pid)/'images'/images['clean']
        digest=lambda value:hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        record={'slide_id':sid,'design_sha256':rc.sha_file(design_path),'clean_sha256':rc.sha_file(clean_path),
                'elements_sha256':digest(s.get('elements',[])),'native_text_count':len(native),'confirmed_at':time.time()}
        target=project_dir(pid)/'build'/'reconstruction_confirmations'/f'{sid}.json'
        safe_json_write(target,record)
        return {'slide_id':sid,'confirmed':True,'native_text_count':len(native),'pages':_reconstruction_progress(p)}
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/ocr')
def reconstruction_ocr(pid:str,sid:str,backend:str='auto'):
    p=p_or_404(pid)
    try:return rc.detect_text(p,sid,backend)
    except Exception as e:fail(e)

@app.put('/api/projects/{pid}/slides/{sid}/regions')
def reconstruction_regions(pid:str,sid:str,body:RegionPayload):
    p=p_or_404(pid)
    try:return rc.persist_regions(p,sid,body.regions)
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/clean-candidate')
def reconstruction_clean(pid:str,sid:str,body:RebuildRequest):
    p=p_or_404(pid)
    try:return rc.generate_clean_candidate(p,sid,body.regions,body.mask_mode,body.method,body.padding,body.threshold,body.source_role)
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/raster-first')
def reconstruction_raster_first(pid:str,sid:str):
    p=p_or_404(pid)
    try:return rc.initialize_raster_first(p,sid)
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/accept-clean')
def reconstruction_accept(pid:str,sid:str):
    p=p_or_404(pid)
    try:return rc.accept_clean(p,sid)
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/extract-asset')
def reconstruction_extract(pid:str,sid:str,body:CropRequest):
    p=p_or_404(pid)
    try:return rc.extract_asset(p,sid,body.box,body.make_transparent,body.tolerance)
    except Exception as e:fail(e)

@app.get('/lab/{pid}/{sid}/{filename}')
def reconstruction_artifact(pid:str,sid:str,filename:str):
    if not re.fullmatch(r'S\d{3}_(?:ocr\.json|candidate\.png|inspection\.png|mask\.png|mask_overlay\.png|candidate_report\.json|diff\.png|diff_report\.json|previous_clean\.png)',filename):
        raise HTTPException(404,'非法实验文件名')
    if not re.fullmatch(r'S\d{3}',sid) or not filename.startswith(sid+'_'):
        raise HTTPException(404,'实验页码不匹配')
    try:
        f=rc.lab_dir(pid)/filename
        if not f.is_file():raise FileNotFoundError('实验文件不存在')
        return FileResponse(f,headers={'Cache-Control':'no-cache'})
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/compare-render')
def reconstruction_compare(pid:str,sid:str):
    p=p_or_404(pid)
    try:
        f=_current_export_preview(pid,p,find_slide(p,sid),'editable')
        return rc.compare_render(p,sid,f)
    except Exception as e:fail(e)

@app.get('/api/projects/{pid}/qa')
def qa(pid:str):return audit(p_or_404(pid))

@app.post('/api/projects/{pid}/slides/{sid}/analyze')
async def visual_analyze(pid:str,sid:str,request:AIRequest):
    p=p_or_404(pid);s=find_slide(p,sid)
    try:return await analyze_visual(p,s,rel_media(p,sid,'design'),request.api_key,request.model)
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/analyze-structure')
async def visual_structure(pid:str,sid:str,request:AIRequest):
    """AI-assisted scene segmentation proposal; does not change the project."""
    p=p_or_404(pid);s=find_slide(p,sid)
    try:
        source=rel_media(p,sid,'design')
        result=await analyze_structure(p,s,source,request.api_key,request.model)
        # Vision text boxes are coarse semantic proposals, not reliable style
        # measurements. Use the paired source pixels for glyph size/ink so a
        # genuinely OCR-missed label does not default to oversized dark text.
        try:
            design_pixels=rc.image_arr(source)
            clean_pixels=rc.image_arr(rel_media(p,sid,'clean'))
        except (ValueError,FileNotFoundError):
            design_pixels=clean_pixels=None
        for obj in result.get('objects',[]):
            if obj.get('kind')!='text' or not isinstance(obj.get('box'),list) or len(obj['box'])!=4:
                continue
            text=str(obj.get('text') or '').strip()
            if not text or design_pixels is None:
                continue
            try:
                measured=rc._estimate_text_style(design_pixels,obj['box'],text,p.get('design',{}).get('font'))
                style=dict(obj.get('style') or {})
                if measured.get('size'):
                    style['size']=measured['size']
                color=rc._paired_text_color(design_pixels,clean_pixels,obj['box']) if clean_pixels is not None else None
                color=color or measured.get('color')
                # Vision-only text boxes may have no reliable sampled ink.
                # Falling back to the project-wide dark ink on a dark raster
                # creates the black duplicate/ghost seen in Step 02. Always
                # select a readable ink against the paired clean pixels.
                if clean_pixels is not None:
                    x1,y1,x2,y2=rc.as_pixels(obj['box'],clean_pixels.shape[1],clean_pixels.shape[0])
                    local_background=np.median(clean_pixels[y1:y2,x1:x2].reshape(-1,3),axis=0) if x2>x1 and y2>y1 else np.median(clean_pixels.reshape(-1,3),axis=0)
                    style['color']=rc._visible_ink_color(color,local_background)
                elif color:
                    style['color']=color
                obj['style']=style
                obj['pixel_style_evidence']={'font_size_pt':measured.get('size'),'color_sampled':bool(color)}
            except (ValueError,TypeError,IndexError):
                continue
        # Supplement visual semantics with low-confidence local contour
        # candidates. These remain unchecked proposals until the user imports.
        remaining=max(0,80-len(result['objects']))
        result['objects'].extend(rc.detect_primitive_candidates(source,min(24,remaining)))
        result['geometry_candidates']=sum(o.get('source')=='opencv_geometry' for o in result['objects'])
        return result
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/recognize-formulas')
async def recognize_slide_formulas(pid:str,sid:str,request:FormulaRecognitionRequest):
    """Use focused source-image crops to preserve displayed equation structure."""
    p=p_or_404(pid);find_slide(p,sid)
    candidates=request.candidates
    if not isinstance(candidates,list) or len(candidates)>8:raise HTTPException(400,'公式候选必须是最多8项的数组。')
    normalized=[]
    for i,item in enumerate(candidates):
        if not isinstance(item,dict):raise HTTPException(400,'公式候选格式无效。')
        box=item.get('box')
        if not isinstance(box,list) or len(box)!=4:raise HTTPException(400,'公式区域坐标无效。')
        try:x,y,w,h=map(float,box)
        except (TypeError,ValueError):raise HTTPException(400,'公式区域坐标无效。') from None
        if not all(math.isfinite(v) for v in (x,y,w,h)) or x<0 or y<0 or w<=0 or h<=0 or x+w>1 or y+h>1:
            raise HTTPException(400,'公式区域必须位于页面范围内。')
        normalized.append({'id':str(item.get('id') or f'formula_{i+1}'),'box':[x,y,w,h],
                           'ocr_text':str(item.get('ocr_text') or '')[:240]})
    try:return await recognize_formula_crops(rel_media(p,sid,'design'),normalized,request.api_key,request.model)
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/review-image-pair')
async def image_pair_review(pid:str,sid:str,request:AIRequest):
    p=p_or_404(pid)
    try:
        slide=find_slide(p,sid)
        design=rel_media(p,sid,'design');clean=rel_media(p,sid,'clean')
        result=await review_image_pair(p,slide,design,clean,request.api_key,request.model)
        meta=slide.get('image_metadata',{}).get('clean',{})
        result['source_pair_current']=not meta.get('stale_source') and meta.get('source_design_sha256')==slide.get('image_metadata',{}).get('design',{}).get('sha256')
        result['repairable_clean']=bool(
            not result['usable_as_clean'] and result['source_pair_current']
            and result['score']>=65 and 0<len(result.get('residual_text',[]))<=12
            and not result.get('layout_changes') and not result.get('nontext_damage')
            and not result.get('schema_warnings')
        )
        if not result['source_pair_current']:
            result['notice']+=' clean 与当前 design 的文件哈希绑定无效，请重新上传正确配对的无字图。'
        return result
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/prompts/refine-clean')
async def refine_clean_image_prompt(pid:str,sid:str,request:RefineCleanPromptRequest):
    p=p_or_404(pid)
    try:
        slide=find_slide(p,sid);design=rel_media(p,sid,'design');clean=rel_media(p,sid,'clean')
        meta=slide.get('image_metadata',{}).get('clean',{})
        design_sha=slide.get('image_metadata',{}).get('design',{}).get('sha256')
        if meta.get('stale_source') or meta.get('source_design_sha256')!=design_sha:
            raise ValueError('clean 底图与当前 design 未配对，请重新上传并审核。')
        return await refine_clean_prompt(p,slide,design,clean,request.review,request.current_prompt,
                                         request.api_key,request.model)
    except Exception as e:fail(e)

def validate_calibration_layout_observation(raw,project,slide):
    """Accept only bounded Chromium geometry matching the saved project state."""
    if not isinstance(raw,dict):return None,'unavailable_no_browser_observation'
    if (raw.get('project_id')!=project.get('id') or raw.get('project_revision')!=project.get('revision')
            or raw.get('slide_id')!=slide.get('id')):
        return None,'ignored_stale_or_wrong_scope'
    stage=raw.get('stage');objects=raw.get('objects')
    if (not isinstance(stage,dict) or not isinstance(objects,list) or len(objects)>512):
        return None,'ignored_invalid_browser_observation'
    try:width=float(stage.get('width'));height=float(stage.get('height'))
    except (TypeError,ValueError):return None,'ignored_invalid_browser_observation'
    if not all(math.isfinite(v) and 100<=v<=12000 for v in (width,height)):
        return None,'ignored_invalid_browser_observation'
    expected={e['id']:e for e in slide.get('elements',[]) if e.get('kind')=='text' and str(e.get('text','')).strip()}
    valid=[];seen=set()
    def numbers(value,keys):
        if not isinstance(value,dict):return None
        try:out={key:float(value[key]) for key in keys}
        except (KeyError,TypeError,ValueError):return None
        return out if all(math.isfinite(v) and abs(v)<24000 for v in out.values()) else None
    for obj in objects:
        if not isinstance(obj,dict):continue
        item=expected.get(obj.get('id'))
        if not item or obj['id'] in seen or obj.get('text')!=item.get('text'):continue
        frame=numbers(obj.get('box_px'),('x','y','width','height'))
        ink=numbers(obj.get('ink_box'),('x','y','width','height'))
        if not frame or not ink or frame['width']<=0 or frame['height']<=0 or ink['width']<=0 or ink['height']<=0:continue
        bx,by,bw,bh=item['box'];tol=max(1.25,width*.0015)
        if any(abs(a-b)>tol for a,b in zip((frame['x'],frame['y'],frame['width'],frame['height']),
                (bx*width,by*height,bw*width,bh*height))):continue
        try:baseline=float(obj['baseline_px']);font_size=float(obj['font_size_px'])
        except (KeyError,TypeError,ValueError):continue
        expected_size=float((item.get('style') or {}).get('size',18))*width/960
        if (not math.isfinite(baseline) or not math.isfinite(font_size) or
                abs(font_size-expected_size)>max(1.5,expected_size*.08)):continue
        expected_weight='700' if (item.get('style') or {}).get('bold') else '400'
        if str(obj.get('font_weight'))!=expected_weight:continue
        expected_color=str((item.get('style') or {}).get('color') or project.get('design',{}).get('ink') or '000000').lstrip('#')
        actual_rgb=[int(value) for value in re.findall(r'\d+',str(obj.get('font_color','')))[:3]]
        if re.fullmatch(r'[0-9a-fA-F]{6}',expected_color):
            expected_rgb=[int(expected_color[index:index+2],16) for index in (0,2,4)]
            if actual_rgb!=expected_rgb:continue
        ranges=[];raw_rects=obj.get('range_rects')
        for rect in (raw_rects[:128] if isinstance(raw_rects,list) else []):
            r=numbers(rect,('x','y','width','height'))
            if r and r['width']>=0 and r['height']>=0:ranges.append(r)
        overflow=obj.get('overflow') if obj.get('overflow') in ('visible','hidden','clip','scroll','auto') else 'visible'
        valid.append({'id':item['id'],'text':item['text'],'box_px':frame,'ink_box':ink,
                      'range_rects':ranges,'baseline_px':baseline,'font_size_px':font_size,'overflow':overflow})
        seen.add(item['id'])
    if not valid:return None,'ignored_no_matching_browser_objects'
    status='revision_bound_chromium_dom' if len(valid)==len(expected) else 'partial_revision_bound_chromium_dom'
    return {'stage':{'width':width,'height':height},'objects':valid},status


@app.post('/api/projects/{pid}/slides/{sid}/calibrate-text')
async def calibrate_slide_text(pid:str,sid:str,request:AIRequest):
    p=p_or_404(pid)
    try:
        slide=find_slide(p,sid)
        if not slide.get('images',{}).get('clean'):raise ValueError('请先上传与当前 design 配对的 clean 无字底图。')
        meta=slide.get('image_metadata',{}).get('clean',{})
        design_sha=slide.get('image_metadata',{}).get('design',{}).get('sha256')
        if meta.get('stale_source') or meta.get('source_design_sha256')!=design_sha:
            raise ValueError('clean 底图与当前设计稿未配对或已过期，请重新上传后校准。')
        from .repair_pipeline import inspect as inspect_structure
        diagnosis=inspect_structure(p,[sid])
        blockers=[i for page in diagnosis['pages'] for i in page['issues'] if i['issue_type'] in
                  ('missing_native_object','duplicate_native_object','background_residual_risk','text_overflow','unexpected_line_break','text_displaced_or_hidden')]
        if blockers:raise ValueError('检测到结构/几何缺陷，请先在03生成并验证修复候选；已阻止无效DeepSeek参数校准。')
        if request.optimization_round>3:raise ValueError('视觉样式诊断达到3轮预算，请先复核具体对象，不继续空转。')
        preview=_current_export_preview(pid,p,slide,'editable')
        layout_observation,observation_status=validate_calibration_layout_observation(
            request.layout_observation,p,slide)
        # Keep the structured feedback and the revision-validated DOM metrics.
        # Never archive API credentials or unchecked browser fields here.
        feedback_dir=project_dir(pid)/'build'/'calibration_requests'
        feedback_path=feedback_dir/f'{sid}_latest.json'
        try:
            safe_json_write(feedback_path,{
                'schema_version':1,'project_id':pid,'slide_id':sid,
                'optimization_round':request.optimization_round,
                'transport':'HTTP application/json',
                'feedback':request.optimization_feedback,
                'layout_observation_status':observation_status,
                'layout_observation':layout_observation,
            })
        except OSError:
            # Local archival is useful for inspection but must not block a valid
            # calibration request to DeepSeek.
            pass
        result=await calibrate_text_layout(p,slide,rel_media(p,sid,'design'),rel_media(p,sid,'clean'),preview,
            request.api_key,request.model,request.optimization_feedback,request.optimization_round,True,
            layout_observation,observation_status)
        allowed={'color','bold','italic','font','eastAsianFont','size'};objects={e['id']:e for e in slide['elements']}
        approved=[];rejected=[]
        for suggestion in result.get('corrections',[]):
            e=objects.get(suggestion.get('id'));style=suggestion.get('style',{})
            if e is None or e.get('kind')!='text' or e.get('layout_locks',{}).get('style'):rejected.append(suggestion);continue
            safe={k:v for k,v in style.items() if k in allowed and not e.get('layout_locks',{}).get(k)}
            if 'size' in safe and (not isinstance(safe['size'],(int,float)) or
                not 6<=safe['size']<=96 or abs(safe['size']-e.get('style',{}).get('size',18))>e.get('style',{}).get('size',18)*.25):safe.pop('size')
            if safe:approved.append({'id':e['id'],'target_text':e['text'],'style':safe,'reason':suggestion.get('reason','视觉样式建议')})
            if suggestion.get('box') or set(style)-set(safe):rejected.append(suggestion)
        result['corrections']=approved;result['restricted_suggestions']=rejected
        result['scope']='style_only; coordinates/content/object changes require repair engine'
        return result
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/slides/{sid}/inpaint')
def inpaint(pid:str,sid:str,x:MaskRequest):
    """Legacy UI route now creates reviewable candidate; never overrides clean."""
    p=p_or_404(pid)
    if len(x.rects)>30:raise HTTPException(400,'最多30个选区')
    try:
        r=rc.generate_clean_candidate(p,sid,[{'box':q} for q in x.rects],mask_mode='rectangle',method='telea')
        r['warning']='已生成候选底图，但尚未覆盖clean；请前往「重建实验室」预览并确认。'
        return r
    except Exception as e:fail(e)


def office_renderer_binary():
    """Return the installed LibreOffice CLI name on common platforms."""
    return shutil.which('libreoffice') or shutil.which('soffice')

def _sha256_file(path:Path)->str:
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()

def _current_export_report(pid:str,project:dict,mode:str='editable')->dict:
    """Return only the report whose PPTX bytes and revision still match the project."""
    folder=project_dir(pid)/'build';report_path=folder/f'report_{mode}.json'
    if not report_path.is_file():raise ValueError('没有当前导出报告，请重新导出PPTX。')
    report=json.loads(report_path.read_text(encoding='utf-8'))
    if report.get('project_revision')!=project.get('revision'):
        raise ValueError('项目内容在导出后已修改，请重新导出以同步PPTX和预览。')
    if report.get('project_sha256') and report['project_sha256']!=hashlib.sha256(json.dumps(project,sort_keys=True).encode()).hexdigest():
        raise ValueError('项目内容哈希与导出时不一致，请重新导出。')
    filename=report.get('filename')
    if not isinstance(filename,str) or Path(filename).name!=filename:
        raise ValueError('导出报告中的PPTX文件名无效，请重新导出。')
    artifact=folder/filename
    if not report.get('artifact_sha256') or not artifact.is_file() or _sha256_file(artifact)!=report['artifact_sha256']:
        raise ValueError('导出的PPTX文件与预览报告不再一致，请重新导出。')
    return report

def _current_export_preview(pid:str,project:dict,slide:dict,mode:str='editable')->Path:
    report=_current_export_report(pid,project,mode)
    previews=report.get('previews') or [];index=int(slide.get('number',0))-1
    if not 0<=index<len(previews):raise ValueError('当前导出没有该页的真实渲染预览，请重新导出。')
    preview=project_dir(pid)/'build'/previews[index]
    if not preview.is_file():raise ValueError('真实渲染预览文件缺失，请重新导出。')
    evidence=next((item for item in report.get('preview_artifacts',[]) if item['filename']==preview.name),None)
    if evidence and _sha256_file(preview)!=evidence['sha256']:
        raise ValueError('真实渲染预览的哈希已变化，请重新导出。')
    return preview


def office_font_environment(lo:str,tmp:str) -> dict[str,str]:
    """Expose bundled open fonts to the actual LibreOffice/fontconfig process."""
    env=os.environ.copy()
    bundled=ROOT/'studio'/'assets'/'fonts'
    if not any(bundled.glob('*.ttf')):return env
    executable=Path(lo).resolve()
    configs=[]
    for parent in (executable,*executable.parents):
        configs.extend((parent/'Contents'/'Resources'/'fontconfig'/'fonts.conf',
                        parent/'Resources'/'fontconfig'/'fonts.conf',
                        parent/'native'/'libreoffice-headless'/'libreoffice'/'LibreOfficeDev.app'/'Contents'/'Resources'/'fontconfig'/'fonts.conf'))
    original=next((path for path in configs if path.is_file()),None)
    cache=Path(tmp)/'fontconfig-cache';cache.mkdir(parents=True,exist_ok=True)
    config=Path(tmp)/'visualppt-fonts.conf'
    include=f'<include>{html.escape(str(original))}</include>' if original else ''
    config.write_text('<fontconfig>'+include+
                      f'<dir>{html.escape(str(bundled))}</dir><cachedir>{html.escape(str(cache))}</cachedir></fontconfig>',
                      encoding='utf-8')
    env['FONTCONFIG_FILE']=str(config)
    return env

def render_deck(pid:str,mode:str='editable'):
    if mode not in ('editable','raster'):raise ValueError('mode必须是editable或raster')
    p=p_or_404(pid)
    if not p['locked']:raise ValueError('导出前必须锁定逐页规划')
    q=audit(p)
    if q['errors']:raise ValueError('存在质量硬错误，请先运行QA并修复')
    build=project_dir(pid)/'build';build.mkdir(exist_ok=True)
    # Build and render in isolation. A failed/interrupted export must never
    # replace the PPTX while leaving the previous preview/report in place.
    with tempfile.TemporaryDirectory(prefix=f'.export-{mode}-',dir=build) as temp:
        stage=Path(temp);out=stage/('presentation_'+mode+'.pptx')
        backend=os.getenv('PPT_RENDERER','python').lower()
        has_formulas=mode=='editable' and any(e.get('kind')=='formula' for s in p['slides'] for e in s.get('elements',[]))
        has_layout_v2=mode=='editable' and any(e.get('text_layout',{}).get('version')==2 for s in p['slides'] for e in s.get('elements',[]))
        if backend=='python' or has_formulas or has_layout_v2:
            # Keep the established Studio exporter as the source of truth, then
            # apply the runtime's native rich-text/rotation/crop extensions.
            # This makes accepted runtime edits survive the *production* export
            # path, not only the runtime's isolated preview renderer.
            builder=build_runtime_pptx(p,out,mode,base_builder=build_python_pptx)
            if has_formulas and backend!='python':builder['notice']='公式页强制使用 python-pptx 后端，以保留 PowerPoint 原生 OMML 数学对象。'
            elif has_layout_v2 and backend!='python':builder['notice']='统一布局 v2 使用 python-pptx 精确写入已确认参数；保留 Node 后端用于旧场景。'
        elif backend=='node':
            cmd=['node',str(ROOT/'renderer'/'render.js'),str(project_dir(pid)/'project.json'),str(out),mode]
            r=subprocess.run(cmd,capture_output=True,text=True,timeout=90,cwd=ROOT)
            if r.returncode or not out.is_file():raise ValueError('PPTX生成失败：'+(r.stderr or r.stdout)[-1500:])
            builder={'backend':'PptxGenJS'}
        else:raise ValueError('PPT_RENDERER仅允许 python 或 node')
        if not out.exists():raise ValueError('导出文件未创建')
        native=check_native_text(p,out) if mode=='editable' else {'ok':None,'notice':'栅格模式不保证文字可编辑'}
        layout_check=check_saved_layout(p,out) if mode=='editable' else {'ok':None}
        if mode=='editable' and not layout_check['ok']:
            raise ValueError('导出与保存的布局不一致：'+str(layout_check['pages']))
        if mode=='editable' and not native['ok']:
            raise ValueError('导出后原生文字完整性/公式校验失败（可能缺字或内容不一致），拒绝标记成功：'+str(native['slides']))
        artifact_sha256=_sha256_file(out)
        report={'filename':None,'mode':mode,'size':out.stat().st_size,'artifact_sha256':artifact_sha256,'native_text':native,
                'previews':[],'visual_comparisons':[],'pdf':None,'render_warning':None,'project_revision':p['revision'],'qa':q,'builder':builder,
                'preview_renderer':None,'stage_visual_renderer':'Chromium browser','compatibility_target':'WPS Office for Mac',
                'wps_visual_verified':False,'wps_editability_verified':False,'saved_layout_check':layout_check,
                'layout_manifest':layout_manifest(p,mode),
                'coverage':[rc.reconstruction_coverage(p,s['id']) for s in p['slides'] if s.get('images',{}).get('design')]}
        report['project_id']=pid
        report['project_sha256']=hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()
        changes_dir=build/'calibration_changes'
        report['calibration_changes']=[]
        for path in sorted(changes_dir.glob('*.json'),key=lambda f:f.stat().st_mtime)[-90:]:
            if path.name.endswith(('_before.json','_after.json')):continue
            entry=json.loads(path.read_text())
            if entry['after_revision']>p['revision']:continue
            report['calibration_changes'].append({**entry,'changes':[
                {'id':row['id'],**{key:{field:(row.get(key) or {}).get(field) for field in ('text','style','box')}
                                  for key in ('before','after')}} for row in entry['changes']]})
        lo=office_renderer_binary()
        if lo and os.getenv('PPT_PREVIEW_DISABLE','0')!='1':
            with tempfile.TemporaryDirectory(prefix='ppt-soffice-') as render_tmp:
                cmd=[lo,'-env:UserInstallation=file://'+render_tmp+'/lo-profile','--headless','--convert-to','pdf',
                     '--outdir',str(stage),str(out)]
                try:
                    pr=subprocess.run(cmd,capture_output=True,text=True,timeout=45,
                                      env=office_font_environment(lo,render_tmp))
                    pdf=stage/out.with_suffix('.pdf').name
                    if pr.returncode==0 and pdf.is_file():
                        import pymupdf as fitz
                        doc=fitz.open(pdf)
                        for i,page in enumerate(doc):
                            filename=f'{mode}_S{i+1:03d}_preview.png'
                            page.get_pixmap(matrix=fitz.Matrix(1.4,1.4),alpha=False).save(stage/filename)
                            report['previews'].append(filename)
                        report['preview_renderer']=f'LibreOffice ({Path(lo).name})'
                        report['_staged_pdf']=pdf.name
                    else:report['render_warning']='LibreOffice 转换失败：'+(pr.stderr or pr.stdout)[-350:]
                except Exception as e:report['render_warning']=str(e)
        else:report['render_warning']=('为单元测试主动关闭Office预览' if os.getenv('PPT_PREVIEW_DISABLE')=='1' else '未检测到LibreOffice/soffice，PPTX已导出但没有真实渲染预览')
        for i,slide in enumerate(p['slides']):
            if i>=len(report['previews']) or not slide.get('images',{}).get('design'):continue
            try:report['visual_comparisons'].append(rc.compare_render(p,slide['id'],stage/report['previews'][i]))
            except Exception as e:report['visual_comparisons'].append({'slide_id':slide['id'],'error':str(e)[:300]})
        # Versioned immutable names bind each preview to these exact PPTX bytes.
        version=f'{mode}_r{p["revision"]}_{artifact_sha256[:12]}'
        report['filename']=f'presentation_{version}.pptx'
        final_files=[(out,build/report['filename'])]
        staged_pdf=report.pop('_staged_pdf',None)
        if staged_pdf:
            report['pdf']=f'presentation_{version}.pdf';final_files.append((stage/staged_pdf,build/report['pdf']))
        final_previews=[];report['preview_artifacts']=[]
        for i,staged_name in enumerate(report['previews'],1):
            final_name=f'{mode}_r{p["revision"]}_{artifact_sha256[:12]}_S{i:03d}_preview.png'
            final_files.append((stage/staged_name,build/final_name));final_previews.append(final_name)
            report['preview_artifacts'].append({'filename':final_name,'slide_id':p['slides'][i-1]['id'],
                'project_revision':p['revision'],'artifact_sha256':artifact_sha256,
                'sha256':_sha256_file(stage/staged_name),'renderer':report['preview_renderer']})
        report['previews']=final_previews
        latest=read_project(pid)
        if latest.get('revision')!=p.get('revision'):
            raise ValueError('导出期间项目内容发生变化；本次文件未发布，请重新导出当前版本。')
        for src,dst in final_files:os.replace(src,dst)
        safe_json_write(build/f'report_{mode}.json',report)
        # Backward-compatible local name points at the immutable version. HTTP
        # downloads still enforce the current revision and hash first.
        alias=build/f'presentation_{mode}.pptx';temp_alias=build/f'.presentation_{mode}.link'
        temp_alias.unlink(missing_ok=True);temp_alias.symlink_to(report['filename']);os.replace(temp_alias,alias)
        return report

@app.post('/api/projects/{pid}/repair/check')
def repair_check(pid:str,body:dict=Body(default={})):
    from .repair_pipeline import inspect
    try:return inspect(p_or_404(pid),body.get('slides'))
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/repair/candidates')
def repair_candidate(pid:str,body:dict=Body(default={})):
    from .repair_pipeline import create
    try:return create(p_or_404(pid),body.get('slides'),only_issue_ids=body.get('only_issue_ids'))
    except Exception as e:fail(e)

def _recent_repair_feedback(pid,project,slide_id):
    try:root=project_dir(pid)/'build'/'repairs'
    except ValueError:return []
    if not root.exists():return []
    rows=[]
    for path in sorted(root.glob('*/status.json'),key=lambda x:x.stat().st_mtime,reverse=True)[:30]:
        try:state=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError):continue
        if state.get('base_revision',0)>project.get('revision',0):continue
        for action in state.get('rejected_actions',[]):
            if action.get('slide_id')!=slide_id:continue
            evidence=action.get('validation_evidence') or {}
            rows.append({'text':str(action.get('text',''))[:120],'action':action.get('action'),
                'result':str(action.get('rollback_reason','验收未通过'))[:220],
                'visual_delta':{'before':evidence.get('source_region_mae_before'),
                    'after':evidence.get('source_region_mae_after')}})
            if len(rows)>=4:return rows
    return rows


def _vision_focus_regions(ocr_regions,native_objects,limit=4):
    """Choose compact Design OCR regions that are high-confidence and unmatched."""
    normalize=lambda value:re.sub(r'\s+','',str(value or '')).casefold()
    def overlap(a,b):
        ix=max(0,min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0]));iy=max(0,min(a[1]+a[3],b[1]+b[3])-max(a[1],b[1]))
        return ix*iy/max(1e-9,min(a[2]*a[3],b[2]*b[3]))
    rows=[]
    for index,row in enumerate(ocr_regions or []):
        text=str(row.get('text','')).strip();confidence=float(row.get('confidence',0) or 0)
        digit=len(normalize(text))==1 and normalize(text).isdigit()
        if not text or confidence<.93 or (len(normalize(text))<3 and not digit):continue
        box=row.get('box') or row.get('source_box')
        if not isinstance(box,list) or len(box)!=4 or box[2]<=0 or box[3]<=0:continue
        near=[e for e in native_objects if isinstance(e.get('box'),list) and len(e['box'])==4 and overlap(box,e['box'])>.25]
        if near and normalize(text) in normalize(''.join(str(x.get('text','')) for x in near)):continue
        rows.append({'id':row.get('id') or f'focus-{index+1}','text':text,'box':list(box),
            'confidence':confidence,'reason':str(row.get('review_reason') or 'Design OCR 与当前原生对象文本未匹配')[:160],
            'native_object_ids':[e.get('id') for e in near],
            'priority':0 if digit else 1})
    return sorted(rows,key=lambda x:(x['priority'],-x['confidence']))[:limit]


def _smart_diagnostic_cache(pid,project,slide,regions,preview,report,verified_issues,model,repair_feedback=None):
    """Content-addressed, revision-bound model cache; invalidate on source, layout, font or renderer changes."""
    native=[]
    for e in slide.get('elements',[]):
        if e.get('kind') not in ('text','formula'):continue
        st=e.get('style') or {};font=e.get('text_layout',{}).get('font') or {}
        native.append({'id':e.get('id'),'kind':e.get('kind'),'text':e.get('text'),'box':e.get('box'),
            'style':{k:st.get(k) for k in ('font','eastAsianFont','size','bold','italic','color','align')},
            'locks':e.get('layout_locks',{}),'font_file_sha256':font.get('file_sha256'),
            'font_identity':font.get('file_identity')})
    images=slide.get('images',{});image_hashes={}
    for role in ('design','clean'):
        name=images.get(role)
        path=project_dir(pid)/'images'/name if name else None
        image_hashes[role]=_sha256_file(path) if path and path.is_file() else None
    code_files=[ROOT/'studio'/'ai.py',ROOT/'studio'/'repair_pipeline.py',ROOT/'studio'/'text_layout.py',
        ROOT/'studio'/'browser_qa.py',ROOT/'scripts'/'qa_runner.py']
    renderer_code={str(path.relative_to(ROOT)):_sha256_file(path) for path in code_files if path.is_file()}
    lo=office_renderer_binary();lo_stat=None
    if lo:
        try:
            st=Path(lo).stat();lo_stat={'size':st.st_size,'mtime_ns':st.st_mtime_ns}
        except OSError:pass
    material={'schema':'stage03-visual-repair-intent-v2','project_revision':project.get('revision'),
        'slide_id':slide.get('id'),'images':image_hashes,'ocr_sha256':regions.get('source_sha256'),
        'ocr_regions':regions.get('regions',[]),'native_objects':native,
        'verified_issues':verified_issues,'preview_sha256':_sha256_file(preview),
        'artifact_sha256':report.get('artifact_sha256'),'renderer':report.get('preview_renderer'),
        'office_renderer':lo_stat,'renderer_code':renderer_code,'model':model or configured_model(),
        'repair_feedback':repair_feedback or []}
    cache_key=hashlib.sha256(json.dumps(material,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    cache=project_dir(pid)/'build'/'ai_diagnostics'/f'{slide["id"]}_{cache_key}.json'
    return cache_key,cache

@app.post('/api/projects/{pid}/repair/smart-candidates')
async def smart_repair_candidate(pid:str,request:RepairAIRequest):
    """One bounded vision pass per selected slide, then evidence-gated local repair."""
    from .repair_pipeline import inspect as inspect_structure,create
    from .ai import propose_missing_text
    p=p_or_404(pid);selected=request.slides
    slides=[s for s in p['slides'] if not selected or s['id'] in selected]
    if len(slides)>12:raise HTTPException(400,'一次智能查漏最多处理12页；请分批执行，避免不必要的API消耗。')
    ai_issues={};ai_review={};ai_intents={};calls=0;cache_hits=0;model_ms=0;cache_meta={}
    deterministic=inspect_structure(p,selected)
    deterministic_pages={x['slide_id']:x for x in deterministic.get('pages',[])}
    key=request.api_key or configured_api_key()
    if key:
        try:
            # Reuse the current versioned PPTX render. If absent, create it once.
            for slide in slides:
                regions=rc.get_regions(p,slide['id']) if slide.get('images',{}).get('design') else None
                if not regions or not regions.get('source_valid'):
                    ai_review[slide['id']]=['Design OCR来源未通过当前图片哈希校验，跳过AI自动补建。'];continue
                try:preview=_current_export_preview(pid,p,slide,'editable')
                except Exception:
                    render_deck(pid,'editable');preview=_current_export_preview(pid,p,slide,'editable')
                report=_current_export_report(pid,p,'editable')
                page=deterministic_pages.get(slide['id'],{})
                verified=[i for i in page.get('issues',[]) if i.get('suggested_action') in ('repair_line','repair_source_style','create_missing_text','create_native_text')]
                focus=_vision_focus_regions(regions.get('regions',[]),[e for e in slide.get('elements',[]) if e.get('kind') in ('text','formula')])
                feedback=_recent_repair_feedback(pid,p,slide['id'])
                cache_key,cache_path=_smart_diagnostic_cache(pid,p,slide,regions,preview,report,verified,request.model,feedback)
                try:
                    cached=None
                    if cache_path.is_file():
                        try:
                            entry=json.loads(cache_path.read_text(encoding='utf-8'))
                            if entry.get('schema')=='stage03-visual-repair-intent-v2' and entry.get('cache_key')==cache_key and isinstance(entry.get('proposal'),dict):cached=entry['proposal']
                        except (OSError,json.JSONDecodeError):cached=None
                    if cached is not None:
                        proposal=cached;cache_hits+=1;cache_meta[slide['id']]={'hit':True,'key':cache_key}
                    else:
                        started=time.perf_counter();calls+=1
                        proposal=await propose_missing_text(p,slide,rel_media(p,slide['id'],'design'),preview,
                            regions.get('regions',[]),[e for e in slide.get('elements',[]) if e.get('kind') in ('text','formula')],
                            request.api_key,request.model,verified_layout_issues=verified,repair_feedback=feedback,focus_regions=focus)
                        elapsed=round((time.perf_counter()-started)*1000);model_ms+=elapsed
                        cache_path.parent.mkdir(parents=True,exist_ok=True)
                        safe_json_write(cache_path,{'schema':'stage03-visual-repair-intent-v2','cache_key':cache_key,
                            'created_at':time.time(),'model_ms':elapsed,'proposal':proposal})
                        cache_meta[slide['id']]={'hit':False,'key':cache_key,'model_ms':elapsed}
                    ai_issues[slide['id']]=proposal.get('missing',[])
                    ai_intents[slide['id']]=proposal.get('repairs',[])
                    ai_review[slide['id']]=proposal.get('review',[])
                except Exception as e:
                    ai_review[slide['id']]=['AI查漏未完成：'+str(e)[:240]]
        except Exception as e:
            # Deterministic recoveries remain useful if the render/API setup fails.
            for slide in slides:ai_review.setdefault(slide['id'],['AI查漏未完成：'+str(e)[:240]])
    else:
        for slide in slides:ai_review[slide['id']]=['未配置DeepSeek密钥；已运行确定性检查，可自动修复项仍会生成候选。']
    try:
        result=create(p,selected,ai_issues,calls,ai_review,ai_intents=ai_intents,max_actions=1)
        result['diagnosis']['deterministic_issue_count']=deterministic['issue_count']
        result['diagnosis']['ai_proposal_count']=sum(len(v) for v in ai_issues.values())
        result['diagnosis']['ai_call_count']=calls
        result['diagnosis']['ai_cache_hits']=cache_hits
        result['diagnosis']['ai_model_ms']=model_ms
        result['diagnosis']['ai_diagnostic_cache']=cache_meta
        return result
    except Exception as e:fail(e)


def _visual_agent_missing_text_evidence(project, slide_id, source_regions, browser_qa):
    """Fuse source OCR with independent render QA for exact missing-text candidates.

    This is evidence for the vision model to confirm, not an instruction to
    create text. The model still has to identify the same missing text from the
    Design/render images before the tool layer will authorize a creation.
    """
    import hashlib
    page=(browser_qa or {}).get('page') or {}
    rows=page.get('unmapped_image_evidence') or []
    slide=find_slide(project,slide_id)
    native=[e for e in slide.get('elements',[]) if e.get('kind') in {'text','formula'}]
    normalize=lambda value:re.sub(r'\s+','',str(value or '')).casefold()
    out=[]
    for observation in rows:
        if not isinstance(observation,dict) or observation.get('type')!='missing_text':continue
        text=str(observation.get('expected_text') or observation.get('expected') or '').strip()
        if not text:continue
        matching=[]
        for source in source_regions or []:
            source_text=str(source.get('source_text') or source.get('text') or '').strip()
            box=source.get('source_box') or source.get('box')
            confidence=source.get('confidence',0)
            if normalize(source_text)!=normalize(text) or not isinstance(box,(list,tuple)) or len(box)!=4:continue
            if not isinstance(confidence,(int,float)) or isinstance(confidence,bool) or confidence<.9:continue
            # OCR may report the same glyph twice at one location. Keep source
            # conflicts attached; the vision model must resolve them visually.
            matching.append(source)
        if len(matching)!=1:continue
        source=matching[0];box=list(map(float,source.get('source_box') or source['box']))
        x,y,w,h=box
        already_present=False
        for element in native:
            if normalize(element.get('text'))!=normalize(text):continue
            ebox=element.get('box') or []
            if len(ebox)!=4:continue
            ex,ey,ew,eh=map(float,ebox)
            intersection=max(0,min(x+w,ex+ew)-max(x,ex))*max(0,min(y+h,ey+eh)-max(y,ey))
            if intersection/max(1e-9,min(w*h,ew*eh))>=.75:
                already_present=True;break
        if already_present:continue
        identity=hashlib.sha256(f'{slide_id}|{normalize(text)}|{box}'.encode()).hexdigest()[:12]
        conflicts=source.get('ocr_conflicts') or []
        out.append({'issue_id':f'DQA-{slide_id}-MISSING-{identity}','priority':'P0',
            'category':'missing_content','source_text':text,'source_box':box,
            'source_region_id':source.get('id'),'source_ocr_confidence':source.get('confidence'),
            'source_style':{k:v for k,v in (source.get('style') or {}).items()
                            if k in {'size','color','font','eastAsianFont','bold','italic'}},
            'source_review_required':bool(source.get('review_required')),
            'source_conflicts':conflicts[:4],
            'render_observation':{'type':'missing_text','expected_text':text,
                                  'actual':observation.get('actual','未检测到')},
            'region':box,'confidence':.99,
            'note':'请模型直接查看Design区域确认；仅当图像明确显示该文字时才执行创建。'})
    return out


def _bind_visual_missing_evidence(issues, verified_evidence):
    """Raise confidence only when the model independently names the same text."""
    normalize=lambda value:re.sub(r'\s+','',str(value or '')).casefold()
    bound=[]
    for issue in issues or []:
        if not isinstance(issue,dict):
            bound.append(issue);continue
        issue=deepcopy(issue)
        description=' '.join(str(issue.get(k,'')) for k in ('category','description','expected_result','evidence'))
        region=issue.get('region')
        for proof in verified_evidence or []:
            text=normalize(proof.get('source_text'))
            if not text or text not in normalize(description):continue
            if not isinstance(region,(list,tuple)) or len(region)!=4:continue
            x,y,w,h=map(float,region);sx,sy,sw,sh=map(float,proof['source_box'])
            intersection=max(0,min(x+w,sx+sw)-max(x,sx))*max(0,min(y+h,sy+sh)-max(y,sy))
            if intersection/max(1e-9,sw*sh)<.5:continue
            issue.update({'priority':'P0','category':'missing_content','confidence':max(.9,float(issue.get('confidence') or 0)),
                'region':proof['source_box'],'expected_result':proof['source_text'],
                'evidence':(str(issue.get('evidence') or '')+'；'+
                    f"Design OCR {proof.get('source_region_id')}={proof['source_text']!r} ({proof.get('source_ocr_confidence'):.3f}); "
                    f"当前真实渲染QA未检测到该字；OCR冲突={json.dumps(proof.get('source_conflicts'),ensure_ascii=False)}").strip('；'),
                'element_ids':[],'verified_source_id':proof.get('source_region_id')})
            bound.append(issue)
            break
        else:
            bound.append(issue)
    return bound


def _merge_visual_agent_issues(existing, updates):
    """Merge model status updates without dropping still-open diagnosed issues."""
    merged=[]
    positions={}
    for row in existing or []:
        if not isinstance(row,dict):
            continue
        item=deepcopy(row)
        issue_id=item.get('issue_id')
        if issue_id:
            positions[issue_id]=len(merged)
        merged.append(item)
    for row in updates or []:
        if not isinstance(row,dict):
            continue
        item=deepcopy(row)
        issue_id=item.get('issue_id')
        if issue_id and issue_id in positions:
            current=merged[positions[issue_id]]
            current.update(item)
        else:
            if issue_id:
                positions[issue_id]=len(merged)
            merged.append(item)
    return merged


def _visual_agent_issue_calls(rows, issue):
    """Accept valid mutations in the current slide's isolated candidate.

    The model may need to touch a neighboring object or create/delete an object
    that was not listed in the issue's OCR linkage. Page scope and tool schema
    are enforced by execute_tool_calls; issue confidence/linkage is advisory.
    """
    allowed={'edit_element','create_element','delete_element','duplicate_element',
             'reorder_element','batch_edit','align_elements','distribute_elements','undo_edit'}
    return [deepcopy(row) for row in rows or []
            if isinstance(row,dict) and row.get('tool') in allowed and isinstance(row.get('arguments') or {},dict)]


def _visual_agent_repeated_accepted_edit(calls, iterations):
    """Return accepted issue/edit evidence when a new proposal repeats it exactly."""
    def normalized(rows):
        result=[]
        for row in rows or []:
            if not isinstance(row,dict):continue
            tool=row.get('tool');args=row.get('arguments') or {}
            if tool in {'edit_element','delete_element','duplicate_element','reorder_element','align_elements','distribute_elements','create_element'}:
                clean=deepcopy(args);clean.pop('evidence_issue_id',None)
                result.append((tool,json.dumps(clean,ensure_ascii=False,sort_keys=True,separators=(',',':'))))
            elif tool=='batch_edit':
                for edit in args.get('edits') or []:
                    if not isinstance(edit,dict):continue
                    clean=deepcopy(edit);clean.pop('evidence_issue_id',None)
                    result.append(('edit_element',json.dumps(clean,ensure_ascii=False,sort_keys=True,separators=(',',':'))))
        return result
    proposed=normalized(calls)
    if not proposed:return None
    accepted={}
    for row in iterations or []:
        if row.get('outcome')!='accept':continue
        issue_id=None
        for call in row.get('tool_calls') or []:
            args=call.get('arguments') or {}
            issue_id=args.get('evidence_issue_id') or issue_id
            for edit in args.get('edits') or []:
                if isinstance(edit,dict):issue_id=edit.get('evidence_issue_id') or issue_id
        for signature in normalized(row.get('tool_calls')):
            accepted.setdefault(signature,issue_id)
    if not all(signature in accepted for signature in proposed):return None
    matching=next((accepted[sig] for sig in proposed if accepted.get(sig)),None)
    return {'issue_id':matching,'edits':[json.loads(value) for _,value in proposed]}


def _render_visual_agent_candidate(source_project, candidate, slide_id, session, label):
    """Render one isolated candidate with LO and Chromium QA, then archive screenshots."""
    temp_id=uuid.uuid4().hex[:12];temp_root=core.DATA/temp_id;artifact_dir=session/'iterations'/label
    artifact_dir.mkdir(parents=True,exist_ok=True)
    try:
        temp_root.mkdir(parents=True)
        (temp_root/'.visual_agent_preview').write_text('temporary validation clone; never show as a user project',encoding='utf-8')
        source_root=project_dir(source_project['id'])
        shutil.copytree(source_root/'images',temp_root/'images')
        clone=json.loads(json.dumps(candidate,ensure_ascii=False));clone['id']=temp_id
        safe_json_write(temp_root/'project.json',clone)
        report=render_deck(temp_id,'editable')
        if not report.get('previews') or not report.get('preview_renderer','').startswith('LibreOffice'):
            raise ValueError(report.get('render_warning') or '没有生成 LibreOffice 实际渲染预览')
        build_dir=temp_root/'build';slide=find_slide(clone,slide_id);index=int(slide.get('number',1))-1
        lo_preview=build_dir/report['previews'][index]
        pptx=build_dir/report['filename']
        shutil.copy2(lo_preview,artifact_dir/'libreoffice.png')
        shutil.copy2(pptx,artifact_dir/'candidate.pptx')
        env=os.environ.copy();env['PPT_STUDIO_DATA']=str(core.DATA);env.pop('PPT_PREVIEW_DISABLE',None)
        qa_dir=artifact_dir/'browser_qa'
        cmd=[sys.executable,str(ROOT/'scripts'/'qa_runner.py'),'--browser','--project',temp_id,
             '--slides',slide_id,'--skip-workflow','--output',str(qa_dir)]
        with (artifact_dir/'browser_qa.log').open('w',encoding='utf-8') as log:
            proc=subprocess.run(cmd,cwd=ROOT,env=env,stdout=log,stderr=log,timeout=600)
        qa_path=qa_dir/'report.json'
        qa=json.loads(qa_path.read_text(encoding='utf-8')) if qa_path.is_file() else {'fatal_error':'Chromium QA 未生成报告'}
        stage=qa_dir/f'{slide_id}_stage.png'
        if stage.is_file():shutil.copy2(stage,artifact_dir/'chromium.png')
        return {'label':label,'libreoffice':'iterations/'+label+'/libreoffice.png',
            'chromium':'iterations/'+label+'/chromium.png' if (artifact_dir/'chromium.png').is_file() else None,
            'candidate_pptx':'iterations/'+label+'/candidate.pptx',
            'artifact_sha256':report.get('artifact_sha256'),'preview_sha256':_sha256_file(artifact_dir/'libreoffice.png'),
            'browser_qa':{'exit_code':proc.returncode,'fatal_error':qa.get('fatal_error'),
                'layout_contract_pass':qa.get('layout_contract_pass'),'pptx_pass':qa.get('pptx_pass'),
                'workflow_pass':qa.get('workflow_pass'),'visual_status':qa.get('visual_status'),
                'page':next((x for x in qa.get('pages',[]) if x.get('slide_id')==slide_id),None)},
            'libreoffice_renderer':report.get('preview_renderer'),'pptx_report':{
                'native_text':report.get('native_text'),'saved_layout_check':report.get('saved_layout_check')}}
    finally:
        shutil.rmtree(temp_root,ignore_errors=True)


def _render_visual_agent_browser_preview(preview_worker, candidate, label):
    """Render an isolated candidate using the run's persistent Chromium page."""
    try:
        result=preview_worker.render(candidate,label)
        source=Path(preview_worker.session)/result['chromium']
        # Preserve each run's evidence paths while sharing the warm Chromium worker.
        session=getattr(preview_worker,'active_run_session',None)
        if session:
            destination=Path(session)/result['chromium'];destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,destination)
        return result
    except Exception as exc:
        raise ValueError('Chromium 快速预览失败：'+str(exc)) from exc


def _get_stage_preview_worker(project,slide_id):
    key=(project['id'],slide_id);worker=_stage_preview_workers.get(key)
    slide=find_slide(project,slide_id);environment=[]
    for role,filename in sorted((slide.get('images') or {}).items()):
        path=project_dir(project['id'])/'images'/str(filename)
        environment.append((role,_sha256_file(path) if path.is_file() else str(filename)))
    for source in (ROOT/'web'/'app.js',ROOT/'web'/'style.css',ROOT/'studio'/'text_layout.py',ROOT/'studio'/'visual_agent_preview.py'):
        environment.append((str(source),_sha256_file(source) if source.is_file() else 'missing'))
    environment_hash=hashlib.sha256(json.dumps(environment,sort_keys=True).encode()).hexdigest()
    if worker is not None and getattr(worker,'preview_environment_hash',None)!=environment_hash:
        worker.close();worker=None;_stage_preview_workers.pop(key,None)
    if worker is None:
        from .visual_agent_preview import VisualAgentBrowserPreview
        session=project_dir(project['id'])/'build'/'stage_preview_worker';session.mkdir(parents=True,exist_ok=True)
        worker=VisualAgentBrowserPreview(project,slide_id,session);worker.preview_environment_hash=environment_hash
        _stage_preview_workers[key]=worker
    worker.source_revision=project.get('revision')
    return worker


@app.post('/api/projects/{pid}/visual-agent/runs/{run_id}/stop')
def stop_visual_agent(pid:str,run_id:str):
    if not re.fullmatch(r'[a-f0-9]{32}',run_id):raise HTTPException(404,'Agent运行不存在')
    run=_visual_agent_runs.get(run_id)
    if not run or run.get('project_id')!=pid or not run.get('running'):
        return {'run_id':run_id,'stop_requested':False,'reason':'运行已结束或不存在'}
    loop=run.get('_event_loop')
    active_task=run.get('_active_model_task')
    if loop and loop.is_running():
        loop.call_soon_threadsafe(run['stop_event'].set)
        if active_task is not None:loop.call_soon_threadsafe(active_task.cancel)
    else:run['stop_event'].set()
    return {'run_id':run_id,'stop_requested':True,
            'reason':'已收到停止请求；正在取消当前模型调用并停止后续修复。已验收候选会保留，未验收的本轮修改会回滚；正式项目不会自动应用。'}


@app.post('/api/projects/{pid}/stage-preview/{slide_id}')
async def stage_preview(pid:str,slide_id:str):
    """Automatically render a revision-bound Chromium preview without PPTX/LO."""
    from .repair_pipeline import signature
    p=p_or_404(pid);slide=find_slide(p,slide_id)
    if not slide.get('images',{}).get('design'):raise HTTPException(400,'当前页缺少 Design 原图')
    key=(pid,slide_id)
    evidence=[signature(p),slide_id,str(p.get('revision'))]
    for role in ('design','clean'):
        filename=slide.get('images',{}).get(role)
        if filename:
            path=rel_media(p,slide_id,role)
            evidence.append(_sha256_file(path) if path.is_file() else filename)
    font_ids=sorted({str(e.get('text_layout',{}).get('font',{}).get('file_identity',''))
        for e in slide.get('elements',[]) if e.get('text_layout',{}).get('font')})
    evidence.append(json.dumps(font_ids))
    for source in (ROOT/'web'/'app.js',ROOT/'web'/'style.css',ROOT/'studio'/'text_layout.py',ROOT/'studio'/'visual_agent_preview.py'):
        evidence.append(_sha256_file(source) if source.is_file() else str(source))
    cache_key=hashlib.sha256('|'.join(evidence).encode()).hexdigest()[:20]
    cached=_stage_preview_cache.get(key)
    if cached and cached.get('key')==cache_key and Path(cached['path']).is_file():
        return {'slide_id':slide_id,'revision':p.get('revision'),'renderer':'Chromium browser',
            'cache_hit':True,'preview_sha256':cached['sha256'],
            'url':f'/api/projects/{pid}/stage-preview/{slide_id}/{slide_id}_{cache_key}.png'}
    out_dir=project_dir(pid)/'build'/'stage_preview';target=out_dir/f'{slide_id}_{cache_key}.png'
    if target.is_file():
        digest=_sha256_file(target);_stage_preview_cache[key]={'key':cache_key,'path':str(target),'sha256':digest}
        return {'slide_id':slide_id,'revision':p.get('revision'),'renderer':'Chromium browser',
            'cache_hit':True,'preview_sha256':digest,
            'url':f'/api/projects/{pid}/stage-preview/{slide_id}/{slide_id}_{cache_key}.png'}
    root=project_dir(pid)/'build'/'stage_preview_worker';root.mkdir(parents=True,exist_ok=True)
    worker=_get_stage_preview_worker(p,slide_id)
    started=time.perf_counter()
    result=await asyncio.to_thread(worker.render,p,'stage-'+cache_key)
    source=root/result['chromium'];out_dir.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    digest=_sha256_file(target)
    _stage_preview_cache[key]={'key':cache_key,'path':str(target),'sha256':digest}
    return {'slide_id':slide_id,'revision':p.get('revision'),'renderer':'Chromium browser',
        'cache_hit':False,'render_seconds':round(time.perf_counter()-started,3),
        'preview_sha256':digest,'url':f'/api/projects/{pid}/stage-preview/{slide_id}/{slide_id}_{cache_key}.png'}


@app.get('/api/projects/{pid}/stage-preview/{slide_id}/{filename}')
def stage_preview_file(pid:str,slide_id:str,filename:str):
    p_or_404(pid)
    if not re.fullmatch(rf'{re.escape(slide_id)}_[0-9a-f]{{20}}\.png',filename):raise HTTPException(404,'预览文件名无效')
    file=project_dir(pid)/'build'/'stage_preview'/filename
    if not file.is_file():raise HTTPException(404,'当前版本的 Chromium 预览尚未生成，请刷新或重新选择本页。')
    return FileResponse(file,media_type='image/png',headers={'Cache-Control':'no-cache'})


def _public_visual_agent_run(run_id,run):
    event=run.get('stop_event')
    return {'run_id':run_id,'stop_requested':bool(event and event.is_set()),**{k:v for k,v in run.items()
        if not k.startswith('_') and k!='stop_event'}}


def _active_visual_agent_run(pid,slide_id):
    for run_id,job in list(_visual_agent_jobs.items()):
        if (job.get('project_id')==pid and job.get('slide_id')==slide_id
                and not job['task'].done()):
            row=_visual_agent_runs.get(run_id) or {
                'project_id':pid,'slide_id':slide_id,'mode':job.get('mode'),
                'running':True,'progress':{'phase':'starting','slide_id':slide_id,
                    'message':'后台优化任务正在启动','issues':[]}}
            return _public_visual_agent_run(run_id,row)
    for run_id,row in list(_visual_agent_runs.items()):
        if row.get('project_id')==pid and row.get('slide_id')==slide_id and row.get('running'):
            return _public_visual_agent_run(run_id,row)
    return None


@app.get('/api/projects/{pid}/visual-agent/active-run')
def visual_agent_active_run(pid:str,slide_id:str):
    p=p_or_404(pid)
    if not re.fullmatch(r'S\d{3}',slide_id):raise HTTPException(400,'slide_id 无效')
    find_slide(p,slide_id)
    return _active_visual_agent_run(pid,slide_id)


@app.get('/api/projects/{pid}/visual-agent/runs/{run_id}')
def visual_agent_run_progress(pid:str,run_id:str):
    if not re.fullmatch(r'[a-f0-9]{32}',run_id):raise HTTPException(404,'Agent运行不存在')
    run=_visual_agent_runs.get(run_id)
    if not run or run.get('project_id')!=pid:raise HTTPException(404,'Agent运行不存在')
    return _public_visual_agent_run(run_id,run)


async def _bounded_visual_agent_turn(*args,**kwargs):
    """Bound one model request; keep this separate from the provider's HTTP timeout."""
    feedback=kwargs.get('tool_feedback') or {}
    current_issue=feedback.get('current_issue') if isinstance(feedback,dict) else None
    issue_id=(current_issue or {}).get('issue_id') if isinstance(current_issue,dict) else None
    usage_context_token=set_api_usage_context(issue_id=issue_id)
    from .ai import configured_api_provider, configured_model
    provider=configured_api_provider()
    provider_labels={'deepseek':'DeepSeek','qwen':'通义千问','glm':'智谱 GLM','opencode':'OpenCode'}
    model_label=f"{provider_labels.get(provider,provider)}（{configured_model()}）"
    # call_deepseek allows Qwen up to 180s for thinking-mode visual turns. The
    # old outer 45s wait_for cancelled those requests long before their own
    # timeout, so Step 03 reported false timeouts while the provider was still
    # working. Leave a small transport/serialization margin around that limit.
    default_timeout=210.0 if provider=='qwen' else 105.0
    try:
        timeout_seconds=float(os.environ.get('VISUAL_AGENT_TURN_TIMEOUT_SECONDS',str(default_timeout)))
    except (TypeError,ValueError):
        timeout_seconds=default_timeout
    timeout_seconds=max(5.0,min(240.0,timeout_seconds))
    try:
        from .api_limits import current as current_budget
        if current_budget() is not None:current_budget().check()
        return await asyncio.wait_for(visual_agent_turn(*args,**kwargs),timeout=timeout_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        from .api_images import ImageUploadError
        if isinstance(exc,ImageUploadError) and current_budget() is not None:
            current_budget().paused_reason=str(exc)
        detail=str(exc) or exc.__class__.__name__
        return {'agent_error':detail,'reason':f'{model_label} 请求失败或超时（{timeout_seconds:g}秒）：{detail[:300]}',
            'tool_calls':[],'diagnosis':{'issues':[]},'decision':'review'}
    finally:
        reset_api_usage_context(usage_context_token)


@app.post('/api/projects/{pid}/visual-agent/run')
async def run_visual_agent(pid:str,request:VisualAgentRequest,background:bool=False):
    """Keep the job alive when its browser disconnects or refreshes."""
    p_or_404(pid)
    run_id=request.run_id or uuid.uuid4().hex
    if not re.fullmatch(r'[a-f0-9]{32}',run_id):raise HTTPException(400,'run_id 必须是32位十六进制字符')
    if not re.fullmatch(r'S\d{3}',request.slide_id):raise HTTPException(400,'slide_id 无效')
    active=_active_visual_agent_run(pid,request.slide_id)
    if active:
        raise HTTPException(409,{'message':'当前页已有优化任务，已提供原任务编号以恢复进度。',
            'run_id':active['run_id'],'slide_id':request.slide_id})
    if run_id in _visual_agent_jobs:raise HTTPException(409,'任务编号已存在')
    # A run ID is idempotent: repeating a POST must not spend on a second job.
    previous=_visual_agent_runs.get(run_id)
    if previous:
        if previous.get('project_id')!=pid or previous.get('slide_id')!=request.slide_id:
            raise HTTPException(409,'任务编号已存在')
        return JSONResponse(status_code=202,content=_public_visual_agent_run(run_id,previous))
    request=request.model_copy(update={'run_id':run_id})
    task=asyncio.create_task(_execute_visual_agent_run(pid,request))
    _visual_agent_jobs[run_id]={'task':task,'project_id':pid,'slide_id':request.slide_id,'mode':request.mode}
    def finished(done):
        job=_visual_agent_jobs.pop(run_id,None)
        if job is None:return
        row=_visual_agent_runs.setdefault(run_id,{'project_id':pid,'slide_id':request.slide_id,'mode':request.mode})
        row['running']=False
        if done.cancelled():
            error='后台优化被服务中断；可以重新启动，已写入磁盘的候选仍保留。'
        elif done.exception() is not None:
            error='后台优化未完成：'+str(getattr(done.exception(),'detail',None) or done.exception())[:300]
        else:
            result=done.result()
            row['result_token']=result.get('token') if isinstance(result,dict) else None
            return
        row['error']=error
        row['progress']={**row.get('progress',{}),'phase':'failed','slide_id':request.slide_id,'message':error}
    task.add_done_callback(finished)
    if background:
        return JSONResponse(status_code=202,content={'run_id':run_id,'project_id':pid,
            'slide_id':request.slide_id,'mode':request.mode,'running':True,
            'progress':{'phase':'starting','slide_id':request.slide_id,'message':'后台优化任务已启动','issues':[]}})
    # Compatibility for callers waiting for the final record. Cancelling this
    # waiter never cancels the separately owned optimization job.
    return await asyncio.shield(task)


async def _execute_visual_agent_run(pid:str,request:VisualAgentRequest):
    """Run a bounded vision → tool edit → real render → vision reflection loop.

    The formal project is never written by this route. A candidate must be
    explicitly applied through the separate /apply endpoint.
    """
    run_wall_started=time.perf_counter()
    from .repair_pipeline import signature
    from .visual_agent_tools import execute_tool_calls
    p=p_or_404(pid)
    if request.mode not in {'restore','free'}:raise HTTPException(400,'mode 必须为 restore 或 free')
    if not re.fullmatch(r'S\d{3}',request.slide_id):raise HTTPException(400,'slide_id 无效')
    run_id=request.run_id or uuid.uuid4().hex
    if not re.fullmatch(r'[a-f0-9]{32}',run_id):raise HTTPException(400,'run_id 必须是32位十六进制字符')
    if run_id in _visual_agent_runs and _visual_agent_runs[run_id].get('running'):
        raise HTTPException(409,'该 Agent run_id 正在运行')
    if any(row.get('project_id')==pid and row.get('slide_id')==request.slide_id and row.get('running')
           for row in _visual_agent_runs.values()):
        raise HTTPException(409,'当前页面已有优化任务；同一页不能并行写入候选，不同页面可以同时优化。')
    slide=find_slide(p,request.slide_id)
    if not p.get('locked'):raise HTTPException(400,'请先锁定逐页规划后再运行 Visual Agent。')
    if not slide.get('images',{}).get('design'):raise HTTPException(400,'当前页缺少 Design 原图。')
    key=request.api_key or configured_api_key()
    if not key:raise HTTPException(400,f'没有配置当前模型 {configured_model()} 的 API Key。')
    stop_event=asyncio.Event()
    _visual_agent_runs[run_id]={'project_id':pid,'slide_id':slide['id'],'mode':request.mode,
        'instruction':request.instruction,'stop_event':stop_event,'running':True,
        '_event_loop':asyncio.get_running_loop(),
        'progress':{'phase':'starting','message':'准备隔离候选','slide_id':slide['id'],'issues':[],'iteration':0}}
    usage_context_token=set_api_usage_context(task_id=run_id,project_id=pid,slide_id=slide['id'],module='visual_agent')
    from .api_limits import RequestBudget, bind as bind_budget, reset as reset_budget
    spending=RequestBudget(max_cost_cny=request.cost_budget_cny,max_requests=request.request_budget,
        max_bytes=int(request.transfer_budget_mb*1024*1024))
    spending_token=bind_budget(spending)
    provider_labels={'deepseek':'DeepSeek','qwen':'通义千问','glm':'智谱 GLM','opencode':'OpenCode'}
    active_model_label=f"{provider_labels.get(configured_api_provider(),configured_api_provider())}（{configured_model()}）"
    active_batch_issues=[]
    def set_progress(phase,message,issue=None,attempt=0,issues=None,iteration=None):
        row=_visual_agent_runs.get(run_id)
        if row is None:return
        message=str(message).replace('DeepSeek',active_model_label)
        row['progress']={'phase':phase,'message':message,'model_label':active_model_label,'slide_id':slide['id'],
            'current_issue_id':(issue or {}).get('issue_id'),'current_issue':(issue or {}).get('description') or (issue or {}).get('category'),
            'attempt':attempt,'max_attempts':min(2,request.max_attempts_per_issue),
            'cost_control':spending.snapshot(),
            'batch_issue_ids':[item.get('issue_id') for item in active_batch_issues if item.get('issue_id')],
            'batch_size':len(active_batch_issues),'max_rounds':2,
            'issues':deepcopy(issues if issues is not None else row.get('progress',{}).get('issues',[])),
            'iteration':iteration if iteration is not None else row.get('progress',{}).get('iteration',0)}
        if row.get('_live_preview'):
            row['progress']['preview']=deepcopy(row['_live_preview'])
    async def visual_turn(*args,**kwargs):
        """Run one model turn that the stop endpoint can cancel immediately."""
        if stop_event.is_set():
            return {'agent_error':'stop_requested','stop_requested':True,
                'reason':'用户已请求停止；没有开始新的模型请求。','tool_calls':[],
                'diagnosis':{'issues':[]},'decision':'review'}
        task=asyncio.create_task(_bounded_visual_agent_turn(*args,**kwargs))
        run_row=_visual_agent_runs.get(run_id)
        if run_row is not None:run_row['_active_model_task']=task
        try:
            try:return await asyncio.shield(task)
            except asyncio.CancelledError:
                if stop_event.is_set():
                    return {'agent_error':'stop_requested','stop_requested':True,
                        'reason':'用户已请求停止；当前模型请求已取消。','tool_calls':[],
                        'diagnosis':{'issues':[]},'decision':'review'}
                raise
        finally:
            if run_row is not None and run_row.get('_active_model_task') is task:
                run_row.pop('_active_model_task',None)
    preview_worker=None
    phase_timings={'browser_startup_and_baseline':0.0,'verified_evidence':0.0,
        'object_tool_execution_total':0.0,'chromium_update_and_capture_total':0.0,
        'chromium_dom_inspection_total':0.0}
    try:
        design_path=rel_media(p,slide['id'],'design')
        regions=rc.get_regions(p,slide['id'])
        source_regions=(regions or {}).get('regions',[]) if (regions or {}).get('source_valid',True) else []
        token=uuid.uuid4().hex;session=project_dir(pid)/'build'/'visual_agent'/token;session.mkdir(parents=True)
        signature_before=signature(p)
        safe_json_write(session/'original_project.json',p)
        candidate=json.loads(json.dumps(p,ensure_ascii=False));history=[];iterations=[]
        prior_record=None
        if request.base_candidate_token:
            if not re.fullmatch(r'[a-f0-9]{32}',request.base_candidate_token):
                raise HTTPException(400,'候选基线编号无效')
            prior_folder=project_dir(pid)/'build'/'visual_agent'/request.base_candidate_token
            try:
                prior_record=json.loads((prior_folder/'status.json').read_text(encoding='utf-8'))
                prior_candidate=json.loads((prior_folder/'candidate_project.json').read_text(encoding='utf-8'))
            except (OSError,json.JSONDecodeError):
                raise HTTPException(404,'找不到上一轮隔离候选；请先重新识别问题。')
            if (prior_record.get('project_id')!=pid or prior_record.get('slide_id')!=slide['id']
                    or (prior_record.get('base_slide_signature')!=signature({'slide':slide})
                        and prior_record.get('base_signature')!=signature_before)
                    or prior_record.get('status') not in {'diagnosis_complete','diagnosis_failed','candidate_ready','requires_review',
                        'rolled_back','stopped','iteration_limit'}):
                raise HTTPException(409,'上一轮候选与当前项目版本或页面不匹配；请重新识别。')
            if signature(prior_candidate)!=prior_record.get('candidate_signature'):
                raise HTTPException(409,'上一轮隔离候选校验失败；未将其用于新一轮。')
            # Page-scoped graft: preserve only this slide's prior candidate
            # elements. A different slide's independent candidate is never
            # copied into this run.
            prior_slide=find_slide(prior_candidate,slide['id'])
            find_slide(candidate,slide['id'])['elements']=deepcopy(prior_slide.get('elements',[]))
        # Baseline comparison is a fast Chromium capture; the full PPTX/LO gate
        # runs once, after the model accepts the issue-scoped candidate.
        from .visual_agent_preview import VisualAgentBrowserPreview
        preview_worker=_get_stage_preview_worker(p,slide['id'])
        preview_worker.active_run_session=session
        set_progress('rendering_baseline','正在生成当前页 Chromium 基线截图')
        phase_started=time.perf_counter()
        before_render=await asyncio.to_thread(_render_visual_agent_browser_preview,preview_worker,candidate,'baseline')
        phase_timings['browser_startup_and_baseline']=time.perf_counter()-phase_started
        baseline_path=session/before_render['chromium']
        phase_started=time.perf_counter()
        verified_evidence=_visual_agent_missing_text_evidence(
            candidate,slide['id'],source_regions,before_render.get('browser_qa'))
        phase_timings['verified_evidence']=time.perf_counter()-phase_started
        diagnose_started=time.perf_counter()
        prior_diagnosis=None
        if request.diagnosis_token:
            if not re.fullmatch(r'[a-f0-9]{32}',request.diagnosis_token):raise HTTPException(400,'诊断编号无效')
            prior_path=project_dir(pid)/'build'/'visual_agent'/request.diagnosis_token/'status.json'
            try:prior_diagnosis=json.loads(prior_path.read_text(encoding='utf-8'))
            except Exception:raise HTTPException(404,'找不到可复用的 DeepSeek 诊断')
            if (prior_diagnosis.get('slide_id')!=slide['id'] or
                    (prior_diagnosis.get('base_slide_signature')!=signature({'slide':slide}) and
                     prior_diagnosis.get('base_signature')!=signature_before)):
                raise HTTPException(409,'项目或页面已变化；旧诊断失效，请重新识别问题。')
            if (request.base_candidate_token==request.diagnosis_token and
                    prior_diagnosis.get('candidate_signature')!=signature(candidate)):
                raise HTTPException(409,'诊断记录与隔离候选基线不一致；请重新识别问题。')
        if request.resume_from_candidate:
            if not prior_record or request.mode!='restore':
                raise HTTPException(400,'续修需要当前页有效的原稿还原候选。')
            old_issues=deepcopy(prior_record.get('issues') or [])
            resolved={'已改善','已修复','已解决','通过','关闭','resolved','fixed','closed','accepted','false_positive','误报'}
            if any(str(i.get('status','')).strip().casefold() not in resolved for i in old_issues):
                start={'diagnosis':{'issues':old_issues},
                    'tool_calls':[],'reason':'从已保存候选及剩余问题继续，不重复执行已验收修改。'}
                prior_diagnosis={'issues':old_issues,'initial_tool_calls':[]}
                diagnose_elapsed=0.0
                set_progress('using_cached_diagnosis','已恢复候选及剩余问题，准备续修',issues=old_issues)
            else:
                set_progress('diagnosing','恢复候选，补做最终整页视觉复核',issues=old_issues)
                start=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,baseline_path,
                    mode=request.mode,user_instruction=request.instruction,phase='diagnose',page_history=[],
                    tool_feedback={'accepted_issues':[i.get('issue_id') for i in old_issues],
                        'instruction':'候选的已知问题已验收，请补做整页检查，确认没有漏项和回归；发现问题时直接给出修复。'},
                    source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model)
                diagnose_elapsed=time.perf_counter()-diagnose_started
        elif prior_diagnosis and isinstance(prior_diagnosis.get('diagnosis_response'),dict):
            start=prior_diagnosis['diagnosis_response'];diagnose_elapsed=0.0
            set_progress('using_cached_diagnosis','复用当前 revision 的诊断结果',issues=prior_diagnosis.get('issues',[]))
        else:
            set_progress('diagnosing','DeepSeek 正在对照 Design、真实渲染和原生对象识别问题')
            start=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,baseline_path,
                mode=request.mode,user_instruction=request.instruction,phase='diagnose',page_history=[],
                source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model,
                diagnose_only=request.diagnose_only)
            diagnose_elapsed=time.perf_counter()-diagnose_started
        issues=(start.get('diagnosis') or {}).get('issues',[])
        if not isinstance(issues,list):issues=[]
        issues=_bind_visual_missing_evidence(issues,verified_evidence)
        if request.mode=='free' and not request.diagnose_only:
            # Free-edit mode is a user-directed page transaction, not a
            # restore-mode issue repair. Keep the transaction page-scoped while
            # allowing edits/create/delete without OCR confidence or issue IDs.
            visual_issues=[{'issue_id':f'FREE-{slide["id"]}-{run_id[:8]}','priority':'P1',
                'category':'free_edit_instruction','description':request.instruction[:500],
                'expected_result':request.instruction[:1000],
                'evidence':'用户在自由编辑模式直接授权；范围仅限当前页。',
                'confidence':1.0,'element_ids':[e.get('id') for e in slide.get('elements',[])],
                'region':[0,0,1,1],'status':'open'}]
        else:
            visual_issues=issues
            if request.resume_from_candidate:
                visual_issues=_merge_visual_agent_issues(deepcopy(prior_record.get('issues') or []),issues)
        calls=[] if (stop_event.is_set() or request.diagnose_only) else start.get('tool_calls',[])
        if prior_diagnosis:
            calls=prior_diagnosis.get('initial_tool_calls',calls)
        models=[{'phase':'diagnose','elapsed_seconds':round(diagnose_elapsed,3),'response':start}]
        status='stopped' if stop_event.is_set() else ('requires_review' if start.get('agent_error') else 'diagnosis_complete')
        last_render=before_render
        last_reason='收到停止请求；诊断已返回，未执行对象修改。' if stop_event.is_set() else str(start.get('reason','') or '')
        accepted_any=bool(request.resume_from_candidate and prior_record and
            prior_record.get('candidate_signature')!=signature_before)
        global_visual_check_passed=bool(not start.get('agent_error') and
            not (start.get('diagnosis') or {}).get('issues') and not start.get('tool_calls'))
        last_accepted_candidate=deepcopy(candidate)
        last_accepted_render=deepcopy(last_render)
        priority_rank={'P0':0,'P1':1,'P2':2,'P3':3,'0':0,'1':1,'2':2,'3':3}
        resolved_states={'已改善','已修复','已解决','通过','关闭','resolved','fixed','closed','accepted','false_positive','误报'}
        attempts_by_issue={};completed_issue_ids=set();targeted={};no_op_retry_ids=set()
        no_progress_counts={}
        from .ai_plans import RepairPlanQueue
        queued_plans=RepairPlanQueue()
        if os.environ.get('PPT_STUDIO_QUEUED_REPAIR_PLANS','1')!='0':
            queued_plans.add(start.get('repair_plans'),find_slide(candidate,slide['id']))
        seen_candidate_states={}
        repeated_candidate_states={}
        consecutive_model_failures=0
        def issue_sort_key(issue):
            return (priority_rank.get(str(issue.get('priority','P3')).upper(),3),
                    -float(issue.get('confidence') or 0),str(issue.get('issue_id','')))
        def unresolved_issues(rows):
            return [row for row in rows if isinstance(row,dict) and
                    str(row.get('status','')).strip().casefold() not in {x.casefold() for x in resolved_states|{'待定','pending'}} and
                    row.get('issue_id') not in completed_issue_ids]
        def issue_calls(rows,issue):
            if request.mode=='free':
                allowed={'edit_element','create_element','delete_element','duplicate_element','reorder_element',
                    'batch_edit','align_elements','distribute_elements','undo_edit'}
                return [deepcopy(row) for row in rows or [] if isinstance(row,dict) and row.get('tool') in allowed]
            return _visual_agent_issue_calls(rows,issue)
        def take_queued_batch(rows):
            bundle=queued_plans.peek_batch(rows,find_slide(candidate,slide['id']),
                limit=len(rows or []),max_calls=1000)
            if not bundle['calls']:
                return {'issues':[],'calls':[]}
            filtered=issue_calls(bundle['calls'],bundle['issues'][0])
            # Never silently run only a subset of an atomic visual batch.
            if len(filtered)!=len(bundle['calls']):
                return {'issues':[],'calls':[]}
            # Every current diagnosis stays in the reflection batch, including
            # issues whose individual plan needs a second-round proposal.
            return {'issues':deepcopy(rows or bundle['issues']),'calls':filtered}
        def retarget_misbound_edits(rows,current_issue):
            """Route edits to one uniquely matching, diagnosed next issue."""
            if not current_issue or not rows:return None,None
            per_call=[]
            for row in rows:
                args=row.get('arguments') or {};tool=row.get('tool')
                if tool=='edit_element':
                    ids=[args.get('element_id')]
                elif tool=='batch_edit':
                    edits=args.get('edits') or []
                    ids=[edit.get('element_id') for edit in edits if isinstance(edit,dict)]
                    if len(ids)!=len(edits) or not ids:return None,None
                else:return None,None
                matches=[]
                for issue in visual_issues:
                    if issue.get('issue_id')==current_issue.get('issue_id'):continue
                    confidence=issue.get('confidence')
                    if (isinstance(confidence,(int,float)) and not isinstance(confidence,bool) and confidence>=.65
                            and all(element_id in (issue.get('element_ids') or []) for element_id in ids)
                            and str(issue.get('status','')).casefold() not in {'fixed','accepted','已修复','已解决','误报','false_positive','待定'}):
                        matches.append(issue)
                if len(matches)!=1:return None,None
                per_call.append((row,matches[0]))
            target_ids={issue.get('issue_id') for _,issue in per_call}
            if len(target_ids)!=1:return None,None
            target=per_call[0][1];retargeted=[]
            for row,_ in per_call:
                clone=deepcopy(row);args=clone.setdefault('arguments',{})
                args['evidence_issue_id']=target['issue_id']
                if clone.get('tool')=='batch_edit':
                    for edit in args.get('edits') or []:edit['evidence_issue_id']=target['issue_id']
                retargeted.append(clone)
            return target,retargeted
        async def skip_and_advance(skip_reason,render_info):
            nonlocal visual_issues,active_issue,calls,last_reason,consecutive_model_failures,active_batch_issues
            if stop_event.is_set():
                calls=[];last_reason='已停止本轮修复；保留此前已验收候选，未验收修改不应用到正式项目。'
                return False
            if active_issue and active_issue.get('issue_id') not in completed_issue_ids:
                active_issue.update(status='待复核',skip_reason=str(skip_reason)[:1200])
                completed_issue_ids.add(active_issue.get('issue_id'))
                visual_issues=_merge_visual_agent_issues(visual_issues,[active_issue])
            while True:
                if spending.paused_reason:
                    last_reason=spending.paused_reason+'；已暂停后续 API 请求，候选和剩余问题已保留，可续修。'
                    calls=[];active_issue=None
                    return False
                pending_now=sorted(unresolved_issues(visual_issues),key=issue_sort_key)
                if not pending_now:
                    active_issue=None;calls=[];return False
                target_issue=pending_now[0]
                queued=take_queued_batch(pending_now)
                if queued['calls']:
                    active_batch_issues=queued['issues'];active_issue=active_batch_issues[0];calls=queued['calls']
                    last_reason=f'复用修复计划，纳入全部 {len(active_batch_issues)} 项诊断问题；合并修改后统一真实渲染复核。'
                    return True
                set_progress('skipping_stalled_issue',f'已跳过无法继续的 {active_issue.get("issue_id") if active_issue else "当前问题"}，正在处理下一项',
                    target_issue,0,visual_issues,len(iterations))
                started=time.perf_counter()
                next_result=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                    session/(render_info.get('chromium') or render_info.get('libreoffice') or before_render['chromium']),
                    mode=request.mode,user_instruction=request.instruction,phase='diagnose',page_history=iterations,
                    tool_feedback={'current_issue':target_issue,'remaining_issues':pending_now,
                        'accepted_issues':list(completed_issue_ids),'max_attempts_per_issue':request.max_attempts_per_issue,
                        'instruction':'前一个问题已因停滞/无有效编辑被跳过。请只针对当前问题给出一组可执行的首轮编辑；不要重试已跳过问题。'},
                    source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model)
                models.append({'phase':'diagnose_after_skip','elapsed_seconds':round(time.perf_counter()-started,3),
                    'response':next_result,'target_issue_id':target_issue.get('issue_id')})
                if next_result.get('agent_error'):
                    error_reason=str(next_result.get('reason') or next_result['agent_error'])
                    target_issue.update(status='待复核',skip_reason=error_reason)
                    completed_issue_ids.add(target_issue.get('issue_id'))
                    visual_issues=_merge_visual_agent_issues(visual_issues,[target_issue])
                    consecutive_model_failures+=1
                    if consecutive_model_failures>=2:
                        # Do not label unrequested issues as timed out. Preserve them as
                        # reviewable work, and stop spending a full timeout on each one.
                        untouched=[row for row in pending_now if row.get('issue_id') not in completed_issue_ids]
                        for row in untouched:
                            row.update(status='待复核',skip_reason='未逐项调用：DeepSeek 连续请求失败，为避免整页长时间阻塞，本轮暂停在此。')
                        visual_issues=_merge_visual_agent_issues(visual_issues,untouched)
                        active_issue=None;calls=[]
                        last_reason=(f'{target_issue.get("issue_id")} 请求失败并已跳过；DeepSeek 连续两次请求失败，'
                            f'其余 {len(untouched)} 项未逐项尝试，保留待复核。'+error_reason)
                        return False
                    last_reason=f'{target_issue.get("issue_id")} 请求失败，已单独跳过并继续下一项。'+error_reason
                    continue
                consecutive_model_failures=0
                queued_plans.add(next_result.get('repair_plans'),find_slide(candidate,slide['id']))
                discovered=(next_result.get('diagnosis') or {}).get('issues',[])
                if isinstance(discovered,list):
                    visual_issues=_merge_visual_agent_issues(visual_issues,
                        [row for row in discovered if isinstance(row,dict) and row.get('issue_id') not in completed_issue_ids])
                target_issue=next((row for row in visual_issues if row.get('issue_id')==target_issue.get('issue_id')),target_issue)
                current_pending=sorted(unresolved_issues(visual_issues),key=issue_sort_key)
                queued=take_queued_batch(current_pending)
                if queued['calls']:
                    active_batch_issues=queued['issues'];active_issue=active_batch_issues[0];calls=queued['calls']
                    last_reason=f'针对剩余问题合并复用 {len(active_batch_issues)} 项兼容修复计划。'
                    return True
                calls=issue_calls(next_result.get('tool_calls',[]),target_issue)
                if calls:
                    active_issue=target_issue;active_batch_issues=current_pending or [active_issue];last_reason=str(next_result.get('reason') or skip_reason)[:600]
                    return True
                target_issue.update(status='待复核',skip_reason=str(next_result.get('reason') or 'DeepSeek 未给出可执行编辑；已跳过此项并继续。')[:1200])
                completed_issue_ids.add(target_issue.get('issue_id'))
                visual_issues=_merge_visual_agent_issues(visual_issues,[target_issue])
                last_reason=f'{target_issue.get("issue_id")} 没有可执行编辑，已跳过；继续下一项。'
        pending=sorted(unresolved_issues(visual_issues),key=issue_sort_key)
        active_issue=pending[0] if pending else None
        active_batch_issues=[active_issue] if active_issue else []
        calls=issue_calls(calls,active_issue)
        if active_issue and not calls:
            queued=take_queued_batch(pending)
            if queued['calls']:
                active_batch_issues=queued['issues'];active_issue=active_batch_issues[0];calls=queued['calls']
        elif active_issue:
            # Even when the model only supplied direct calls for the highest
            # priority issue, keep the full diagnosis as one review batch.
            active_batch_issues=deepcopy(pending)
            # Prefer the diagnosis' complete compatible plans so the first
            # pass repairs the page as one batch instead of starting serially.
            queued=take_queued_batch(pending)
            if queued['calls']:
                active_batch_issues=queued['issues'];active_issue=active_batch_issues[0];calls=queued['calls']
        if prior_diagnosis and active_issue and not calls:
            active_batch_issues=deepcopy(pending)
            set_progress('preparing_batch',f'正在为全部 {len(pending)} 项诊断问题生成统一修复方案',active_issue,0,visual_issues)
            targeted_started=time.perf_counter()
            targeted=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                session/last_render['chromium'],mode=request.mode,user_instruction=request.instruction,
                phase='diagnose',page_history=[],tool_feedback={'current_issue':active_issue,
                    'remaining_issues':pending,'batch_issues':pending,'max_attempts_per_issue':request.max_attempts_per_issue,
                    'instruction':'这是独立自动优化阶段。请一次为remaining_issues中的全部问题提出修复计划；不要局限于最高优先级问题。所有兼容编辑都要放入同一批次，以便统一应用并进行一次真实渲染复核。'},
                source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model)
            consecutive_model_failures=1 if targeted.get('agent_error') else 0
            models.append({'phase':'diagnose_targeted_issue','elapsed_seconds':round(time.perf_counter()-targeted_started,3),'response':targeted})
            queued_plans.add(targeted.get('repair_plans'),find_slide(candidate,slide['id']))
            calls=issue_calls(targeted.get('tool_calls',[]),active_issue)
            active_batch_issues=deepcopy(pending) if active_issue and calls else ([active_issue] if active_issue else [])
            if isinstance((targeted.get('diagnosis') or {}).get('issues'),list):
                visual_issues=_merge_visual_agent_issues(visual_issues,(targeted.get('diagnosis') or {}).get('issues'))
            last_reason=str(targeted.get('reason') or last_reason)[:600]
        set_progress('diagnosis_complete' if request.diagnose_only else 'queued',
            '问题识别完成；尚未执行任何修改' if request.diagnose_only else f'已识别 {len(pending)} 项问题，准备合并处理',
            active_issue,issues=visual_issues)
        if request.diagnose_only:
            candidate['id']=pid;safe_json_write(session/'candidate_project.json',candidate)
            diagnosis_failed=bool(start.get('agent_error'))
            # A failed model request is not a successful zero-issue diagnosis.
            # Preserve the previous finding list as historical context so a
            # transient API outage cannot erase the page's known problems.
            retained_issues=deepcopy(prior_record.get('issues',[])) if diagnosis_failed and prior_record else visual_issues
            record={'token':token,'project_id':pid,'slide_id':slide['id'],'mode':request.mode,'run_id':run_id,
                'provider_model_label':active_model_label,
                'base_revision':p['revision'],'base_signature':signature_before,'candidate_signature':signature(candidate),
                'base_slide_signature':signature({'slide':slide}),
                'base_candidate_token':request.base_candidate_token,
                'status':'diagnosis_failed' if diagnosis_failed else 'diagnosis_complete','issues':retained_issues,'issue_count':len(retained_issues),
                'diagnosis_error':str(start.get('reason') or '') if diagnosis_failed else None,
                'diagnosis_is_fresh':not diagnosis_failed,
                'models':models,'iterations':[],'last_render':before_render,'reason':str(start.get('reason') or ''),
                'created_at':time.time(),'applied':False,'user_confirmation_required':False,
                'diagnosis_response':start,'initial_tool_calls':[]}
            record['cost_control']=spending.snapshot()
            safe_json_write(session/'status.json',record)
            _visual_agent_runs[run_id]['running']=False
            _visual_agent_runs[run_id]['progress']={'phase':record['status'],
                'message':('DeepSeek 诊断请求失败；没有得出本轮结论，已保留上一轮问题。' if diagnosis_failed and prior_record else
                    'DeepSeek 诊断请求失败；没有得出本轮结论。' if diagnosis_failed else last_reason or '问题识别完成，未编辑候选'),
                'slide_id':slide['id'],'issues':retained_issues,'iteration':0,'cost_control':spending.snapshot()}
            return record
        # Process all diagnosed page issues together, with at most two edit/render/
        # reflection rounds. The final rendered candidate is retained for user
        # review even if the model does not accept every issue.
        max_total_attempts=2
        for index in range(1,max_total_attempts+1):
            mutation_names={'edit_element','create_element','delete_element','duplicate_element','reorder_element','batch_edit','align_elements','distribute_elements','undo_edit'}
            if not calls:
                if stop_event.is_set():
                    status='candidate_ready' if accepted_any else 'stopped'
                    last_reason='收到停止请求；保留此前已验收候选，未应用到正式项目。' if accepted_any else '收到停止请求；未执行对象修改。'
                    break
                if active_issue and active_issue.get('issue_id') not in completed_issue_ids:
                    if await skip_and_advance(last_reason or '当前问题没有有效编辑，已跳过。',last_render):continue
                    status='candidate_ready' if accepted_any else 'requires_review'
                    if not last_reason:last_reason='所有剩余问题均已标记待复核；未修改正式项目。'
                else:
                    status='candidate_ready' if accepted_any else ('requires_review' if start.get('agent_error') else ('diagnosis_complete' if index==1 else 'requires_review'))
                    if start.get('agent_error') and not last_reason:
                        last_reason='DeepSeek 诊断请求失败或超时；没有开始修改，当前页保留待复核状态。'
                if accepted_any and not last_reason:last_reason='当前问题没有可执行参数修改；已保留此前验收候选。'
                break
            if active_issue is None:
                status='candidate_ready' if accepted_any else 'diagnosis_complete'
                last_reason='没有剩余的已确认问题需要自动处理。'
                break
            issue_id=active_issue.get('issue_id');attempt_no=attempts_by_issue.get(issue_id,0)+1
            if attempt_no>request.max_attempts_per_issue:
                if await skip_and_advance(f'问题达到单项尝试上限 {request.max_attempts_per_issue}，已跳过。',last_render):continue
                status='candidate_ready' if accepted_any else 'requires_review';break
            attempts_by_issue[issue_id]=attempt_no
            if len(active_batch_issues)>1:
                for row in active_batch_issues:
                    attempts_by_issue[row.get('issue_id')]=max(attempts_by_issue.get(row.get('issue_id'),0),attempt_no)
            set_progress('editing',f'正在合批优化 {len(active_batch_issues)} 个互不冲突的问题' if len(active_batch_issues)>1 else f'正在优化 {issue_id}',active_issue,attempt_no,visual_issues,index)
            prior=deepcopy(candidate)
            state_key=signature({'ids':sorted(active_issue.get('element_ids') or []),
                'region':active_issue.get('region'),'expected':active_issue.get('expected_result')})
            seen_candidate_states.setdefault(state_key,set()).add(signature(find_slide(prior,slide['id'])))
            attempted_calls=deepcopy(calls)
            tool_started=time.perf_counter()
            candidate,events,history=execute_tool_calls(candidate,slide['id'],calls,mode=request.mode,
                source_regions=source_regions,visual_issues=visual_issues,history=history)
            tool_elapsed=time.perf_counter()-tool_started
            phase_timings['object_tool_execution_total']+=tool_elapsed
            mutated=any(e.get('status')=='applied' and e.get('tool') in mutation_names for e in events)
            # The tool dispatcher already rolls back a rejected operation and
            # retains successful siblings. A no_change is a normal outcome,
            # not a validation failure. Render the actual candidate and send
            # every event to reflection, rather than discarding all progress
            # because one proposal was redundant or invalid.
            if not mutated:
                iterations.append({'round':index,'tool_calls':calls,'tool_events':events,'outcome':'no_supported_mutation'})
                candidate=prior
                progress_key=signature({'ids':sorted(active_issue.get('element_ids') or []),
                    'region':active_issue.get('region'),'expected':active_issue.get('expected_result')})
                if any(e.get('tool') in mutation_names for e in events):
                    no_progress_counts[progress_key]=no_progress_counts.get(progress_key,0)+1
                if no_progress_counts.get(progress_key,0)>=2:
                    last_reason='同一问题连续两次没有实际对象变化；已保留证据并暂停此项，避免重复收费。'
                    if await skip_and_advance(last_reason,last_render):continue
                    status='candidate_ready' if accepted_any else 'requires_review'
                    break
                duplicate_event=next((e for e in events if e.get('tool')=='create_element' and
                    isinstance((e.get('result') or {}).get('duplicate_element'),dict)),None)
                duplicate_object=((duplicate_event or {}).get('result') or {}).get('duplicate_element')
                unchanged_event=next((e for e in events if e.get('status')=='no_change' and
                    isinstance((e.get('result') or {}).get('element'),dict)),None)
                if not unchanged_event and duplicate_object:
                    create_call=next((row for row in calls if row.get('tool')=='create_element'),None)
                    requested=((create_call or {}).get('arguments') or {}).get('element') or {}
                    requested_text=re.sub(r'\s+','',str(requested.get('text') or ''))
                    existing_text=re.sub(r'\s+','',str(duplicate_object.get('text') or ''))
                    if requested_text and requested_text==existing_text:
                        inspect_started=time.perf_counter()
                        inspection=await asyncio.to_thread(preview_worker.inspect_element,duplicate_object['id'])
                        phase_timings['chromium_dom_inspection_total']+=time.perf_counter()-inspect_started
                        duplicate_event['result']['chromium_inspection']=inspection
                        if inspection.get('visible') and requested_text in re.sub(r'\s+','',str(inspection.get('text') or '')):
                            unchanged_event={'result':{'element':duplicate_object}}
                if unchanged_event:
                    unchanged=unchanged_event['result']['element']
                    category=str(active_issue.get('category','')).casefold()
                    expected=re.sub(r'\s+','',str(active_issue.get('expected_result') or ''))
                    actual=re.sub(r'\s+','',str(unchanged.get('text') or ''))
                    is_missing='缺失' in category or 'missing' in category or 'content' in category
                    if is_missing and actual and actual in expected:
                        inspect_started=time.perf_counter()
                        inspection=await asyncio.to_thread(preview_worker.inspect_element,unchanged['id'])
                        phase_timings['chromium_dom_inspection_total']+=time.perf_counter()-inspect_started
                        unchanged_event['result']['chromium_inspection']=inspection
                        if inspection.get('visible') and re.sub(r'\s+','',str(inspection.get('text') or '')).find(actual)>=0:
                            active_issue['status']='false_positive'
                            active_issue['evidence']=(str(active_issue.get('evidence') or '')+
                                f"；Chromium DOM确认原生对象 {unchanged['id']} 含完整目标文字且有非零可见字形区域")
                            completed_issue_ids.add(issue_id)
                            visual_issues=_merge_visual_agent_issues(visual_issues,[active_issue])
                            iterations[-1]['outcome']='false_positive_text_visible_in_chromium'
                            last_reason=f'问题 {issue_id} 的原生文字对象参数未改变；Chromium DOM 已确认完整文字实际可见，已将其标记为误报，没有伪报修复。'
                            pending=sorted(unresolved_issues(visual_issues),key=issue_sort_key)
                            if index<max_total_attempts:
                                audit_started=time.perf_counter()
                                audit=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                                    session/last_render['chromium'],mode=request.mode,user_instruction=request.instruction,
                                    phase='diagnose',page_history=iterations,
                                    tool_feedback={'accepted_issues':list(completed_issue_ids),'remaining_issues':pending,
                                        'current_issue':None,'max_attempts_per_issue':request.max_attempts_per_issue},
                                    source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model)
                                models.append({'phase':'diagnose_after_false_positive',
                                    'elapsed_seconds':round(time.perf_counter()-audit_started,3),'response':audit})
                                discovered=(audit.get('diagnosis') or {}).get('issues',[])
                                if isinstance(discovered,list) and discovered:
                                    visual_issues=_merge_visual_agent_issues(visual_issues,
                                        _bind_visual_missing_evidence(discovered,verified_evidence))
                                pending=sorted(unresolved_issues(visual_issues),key=issue_sort_key)
                                active_issue=pending[0] if pending else None
                                calls=issue_calls(audit.get('tool_calls',[]),active_issue)
                                if active_issue and not calls:
                                    targeted_started=time.perf_counter()
                                    targeted=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                                        session/last_render['chromium'],mode=request.mode,user_instruction=request.instruction,
                                        phase='diagnose',page_history=iterations,
                                        tool_feedback={'accepted_issues':list(completed_issue_ids),'remaining_issues':pending,
                                            'current_issue':active_issue,'max_attempts_per_issue':request.max_attempts_per_issue,
                                            'instruction':'全页问题已重新排序；请只为当前最高优先级问题提出第一组可执行修改，不需要绑定issue_id。'},
                                        source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model)
                                    models.append({'phase':'diagnose_targeted_issue',
                                        'elapsed_seconds':round(time.perf_counter()-targeted_started,3),'response':targeted})
                                    discovered=(targeted.get('diagnosis') or {}).get('issues',[])
                                    if isinstance(discovered,list) and discovered:
                                        visual_issues=_merge_visual_agent_issues(visual_issues,
                                            _bind_visual_missing_evidence(discovered,verified_evidence))
                                    pending=sorted(unresolved_issues(visual_issues),key=issue_sort_key)
                                    active_issue=pending[0] if pending else None
                                    calls=issue_calls(targeted.get('tool_calls',[]),active_issue)
                                    last_reason=str(targeted.get('reason') or last_reason)[:600]
                                if active_issue and calls:
                                    continue
                                last_reason=str(audit.get('reason') or last_reason)[:600]
                                if audit.get('agent_error'):
                                    for row in pending:
                                        row.update(status='待复核',skip_reason=last_reason)
                                        completed_issue_ids.add(row.get('issue_id'))
                                    visual_issues=_merge_visual_agent_issues(visual_issues,pending)
                                    active_issue=None;calls=[]
                                    status='candidate_ready' if accepted_any else 'requires_review'
                                    last_reason='DeepSeek 请求失败或超时；为避免拖慢整页，其余问题已标为待复核并结束本轮。'+last_reason
                                    break
                                if active_issue and await skip_and_advance('该问题已被判为误报，继续处理其余问题。',last_render):continue
                                status='candidate_ready' if accepted_any else ('requires_review' if pending else 'diagnosis_complete')
                                break
                repeated=_visual_agent_repeated_accepted_edit(calls,iterations[:-1])
                if repeated and not stop_event.is_set():
                    # A repeated patch is not evidence that this issue is fixed.
                    # An accepted batch may have changed a sibling while this
                    # exact target returned no_change. Keep the issue active and
                    # let the focused no-op review below choose a different edit.
                    active_issue.update({'duplicate_of':repeated.get('issue_id'),
                        'duplicate_edits':repeated.get('edits'),'repeat_requires_recheck':True})
                    visual_issues=_merge_visual_agent_issues(visual_issues,[active_issue])
                    iterations[-1]['outcome']='repeated_parameters_reopened'
                    iterations[-1]['duplicate_of']=repeated.get('issue_id')
                observed=any(e.get('status')=='observed' for e in events)
                if (observed or duplicate_object) and not stop_event.is_set():
                    attempts_by_issue[issue_id]=max(0,attempts_by_issue.get(issue_id,1)-1)
                    if duplicate_object:
                        ids=list(active_issue.get('element_ids') or [])
                        if duplicate_object.get('id') not in ids:ids.append(duplicate_object.get('id'))
                        active_issue['element_ids']=ids
                        active_issue['evidence']=(str(active_issue.get('evidence') or '')+
                            f"；工具确认同一目标位置已存在对象 {duplicate_object.get('id')}，禁止重复创建")
                    read_feedback={'current_issue':active_issue,'issues':[active_issue],
                        'tool_events':events,'attempt_number':attempts_by_issue.get(issue_id,0),
                        'max_attempts_per_issue':request.max_attempts_per_issue,
                        'confidence_threshold':request.confidence_threshold,
                        'duplicate_object':duplicate_object,
                        'instruction':('该文字对象已存在且参数已附在duplicate_object中。请重新看当前真实截图；'
                            '若文字已清晰可见且符合Design，就标记false_positive；若不可见/裁切/样式不符，可编辑或删除重建。'
                            if duplicate_object else '对象参数已读取。请基于刚返回的真实对象数据，为当前问题提出可执行修改。')}
                    followup_started=time.perf_counter()
                    followup=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                        session/last_render['chromium'],mode=request.mode,user_instruction=request.instruction,
                        phase='diagnose',page_history=iterations,tool_feedback=read_feedback,
                        source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model)
                    models.append({'phase':'diagnose_after_object_read',
                        'elapsed_seconds':round(time.perf_counter()-followup_started,3),'response':followup})
                    followup_issues=(followup.get('diagnosis') or {}).get('issues',[])
                    if isinstance(followup_issues,list):
                        visual_issues=_merge_visual_agent_issues(visual_issues,followup_issues)
                        rejected=next((x for x in followup_issues if isinstance(x,dict) and
                            x.get('issue_id')==issue_id and str(x.get('status','')).casefold() in
                            {'false_positive','false positive','误报'}),None)
                        if rejected:
                            active_issue['status']='false_positive';completed_issue_ids.add(issue_id)
                            last_reason='当前页存在相同文字对象；DeepSeek 复核后判定其已可见，已将本项标记为误报，没有重复创建对象。'
                            calls=[]
                            if await skip_and_advance(last_reason,last_render):continue
                            status='candidate_ready' if accepted_any else 'diagnosis_complete';break
                    calls=issue_calls(followup.get('tool_calls',[]),active_issue)
                    if calls:continue
                if any(e.get('status')=='no_change' for e in events) and issue_id not in no_op_retry_ids and not stop_event.is_set():
                    # A no-op is not evidence that a visually verified issue is
                    # fixed. Give DeepSeek the exact unchanged objects and one
                    # focused chance to choose a real edit/delete, or explicitly
                    # reclassify the finding after re-reading Chromium.
                    no_op_retry_ids.add(issue_id)
                    unchanged_objects=[]
                    seen_unchanged=set()
                    for event in events:
                        element=(event.get('result') or {}).get('element')
                        if event.get('status')!='no_change' or not isinstance(element,dict):continue
                        if element.get('id') in seen_unchanged:continue
                        seen_unchanged.add(element.get('id'))
                        unchanged_objects.append({k:deepcopy(element.get(k)) for k in
                            ('id','kind','text','box','style','source_text','source_group','run_id') if k in element})
                    issue_element_ids=set(active_issue.get('element_ids') or [])
                    for element in find_slide(candidate,slide['id']).get('elements',[]):
                        if element.get('id') in issue_element_ids and element.get('id') not in seen_unchanged:
                            unchanged_objects.append({k:deepcopy(element.get(k)) for k in
                                ('id','kind','text','box','style','source_text','source_group','run_id') if k in element})
                    # Include exact-text siblings occupying the same visual
                    # footprint even when DeepSeek's no-op call named only one
                    # half of a duplicated phrase.
                    def compact_text(value):return re.sub(r'\s+','',str(value or '')).casefold()
                    def glyph_overlap(left,right):
                        try:
                            ax,ay,aw,ah=map(float,left);bx,by,bw,bh=map(float,right)
                            ix=max(0.0,min(ax+aw,bx+bw)-max(ax,bx));iy=max(0.0,min(ay+ah,by+bh)-max(ay,by))
                            return ix*iy/max(1e-9,min(aw*ah,bw*bh))
                        except (TypeError,ValueError):return 0.0
                    page_elements=find_slide(candidate,slide['id']).get('elements',[])
                    for target in list(unchanged_objects):
                        target_text=compact_text(target.get('text'))
                        if not target_text or not isinstance(target.get('box'),list):continue
                        for element in page_elements:
                            if element.get('id') in seen_unchanged or compact_text(element.get('text'))!=target_text:
                                continue
                            if glyph_overlap(target['box'],element.get('box'))<.70:continue
                            seen_unchanged.add(element.get('id'))
                            unchanged_objects.append({k:deepcopy(element.get(k)) for k in
                                ('id','kind','text','box','style','source_text','source_group','run_id') if k in element})
                    retry_started=time.perf_counter()
                    retry=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                        session/(last_render.get('chromium') or before_render['chromium']),mode=request.mode,
                        user_instruction=request.instruction,phase='diagnose',page_history=iterations,
                        tool_feedback={'current_issue':deepcopy(active_issue),'unchanged_objects':unchanged_objects,
                            'previous_tool_calls':attempted_calls,'previous_tool_events':deepcopy(events),
                            'instruction':('上一轮工具调用没有产生任何变化。请重新查看 Design 与最新 Chromium 截图，'
                                '核对 unchanged_objects；如果有重复/错误对象，请直接 delete_element 删除多余对象，'
                                '如果文字内容错误请实际修改文字或位置。如果文字内容已经正确但视觉间距仍不对，'
                                '不要再提交相同 text；请调整相关文本框 box 或相邻对象位置。如果同一短语由多个重叠对象绘出，'
                                '删除多余对象。若最新截图确实已符合 Design，再明确将当前 issue 标为 false_positive。'
                                '不要重复提交相同参数，也不要仅因工具返回 no_change 就声称已修复。')},
                        source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model)
                    models.append({'phase':'diagnose_after_noop','elapsed_seconds':round(time.perf_counter()-retry_started,3),
                        'response':retry,'issue_id':issue_id})
                    retry_issues=(retry.get('diagnosis') or {}).get('issues',[])
                    if isinstance(retry_issues,list):
                        visual_issues=_merge_visual_agent_issues(visual_issues,retry_issues)
                        rejected=next((x for x in retry_issues if isinstance(x,dict) and
                            x.get('issue_id')==issue_id and str(x.get('status','')).casefold() in
                            {'false_positive','false positive','误报'}),None)
                        if rejected:
                            active_issue.update(status='false_positive',
                                evidence=(str(active_issue.get('evidence') or '')+'；DeepSeek 在查看最新截图及原对象后复核为误报。'))
                            completed_issue_ids.add(issue_id)
                            iterations[-1]['outcome']='false_positive_after_noop_review'
                            if await skip_and_advance('DeepSeek 复核后判定当前画面符合预期，已标记为误报。',last_render):continue
                            status='candidate_ready' if accepted_any else 'diagnosis_complete';break
                    calls=issue_calls(retry.get('tool_calls',[]),active_issue)
                    if calls:
                        attempts_by_issue[issue_id]=max(0,attempts_by_issue.get(issue_id,1)-1)
                        iterations[-1]['noop_retry']='DeepSeek 已收到未修改对象和可执行修复提示，正在重新选择实际编辑。'
                        continue
                    last_reason='工具调用未产生实际变化；已把原对象与截图交给 DeepSeek 重新判断一次，但仍未返回不同参数的修复动作，问题继续待复核。'
                status='candidate_ready' if accepted_any else 'requires_review'
                failures=[str(e.get('error')) for e in events if e.get('status') in {'rejected','unsupported'} and e.get('error')]
                if failures:
                    detail='；'.join(dict.fromkeys(failures))[:350]
                    last_reason=('本轮修改未通过工具约束，已回滚；保留此前已验收的候选。' if accepted_any else '模型提出的编辑未通过工具约束，未修改项目。')+detail
                elif any(e.get('status')=='no_change' for e in events):
                    if issue_id not in no_op_retry_ids:
                        last_reason='模型提出的参数与当前对象一致，没有产生实际修改。'+('保留此前已验收的候选。' if accepted_any else '')
                else:
                    last_reason='模型没有成功执行受支持的对象修改；保留此前已验收的候选。' if accepted_any else '模型没有成功执行受支持的对象修改'
                if await skip_and_advance(last_reason,last_render):continue
                status='candidate_ready' if accepted_any else 'requires_review'
                break
            preview_started=time.perf_counter()
            set_progress('rendering_feedback',f'{issue_id} 参数已修改，Chromium 正在重新渲染',active_issue,attempt_no,visual_issues,index)
            after_render=await asyncio.to_thread(_render_visual_agent_browser_preview,preview_worker,candidate,
                f'issue-{issue_id}-attempt-{attempt_no:02d}')
            preview_elapsed=time.perf_counter()-preview_started
            phase_timings['chromium_update_and_capture_total']+=preview_elapsed
            if after_render.get('chromium'):
                live_preview={'url':f'/api/projects/{pid}/visual-agent/{token}/files/{quote(after_render["chromium"],safe="/")}'
                    f'?sha={quote(str(after_render.get("preview_sha256") or ""))}',
                    'renderer':'Chromium browser','revision':p.get('revision'),'issue_id':issue_id,
                    'iteration':index,'preview_sha256':after_render.get('preview_sha256'),'candidate':True}
                _visual_agent_runs[run_id]['_live_preview']=live_preview
                _visual_agent_runs[run_id]['progress']['preview']=live_preview
            if stop_event.is_set():
                iterations.append({'round':index,'tool_calls':calls,'tool_events':events,'before_render':last_render,
                    'after_render':after_render,'outcome':'stopped_before_reflection'})
                candidate=prior
                status='candidate_ready' if accepted_any else 'stopped'
                last_reason='收到停止请求；本轮尚未完成视觉反思，已回滚未验收修改。'+('保留此前已验收候选。' if accepted_any else '')
                break
            feedback={'current_issue':active_issue,'attempt_number':attempt_no,
                'max_attempts_per_issue':2,
                'confidence_threshold':request.confidence_threshold,
                'tool_events':[{'tool':e.get('tool'),'status':e.get('status'),'error':e.get('error'),
                    'result':e.get('result')} for e in events],
                'browser_preview':after_render,'issues':deepcopy(active_batch_issues),
                'batch_issues':deepcopy(active_batch_issues)}
            model_started=time.perf_counter()
            set_progress('model_feedback',f'正在把 {issue_id} 的真实新截图交给 DeepSeek 评估',active_issue,attempt_no,visual_issues,index)
            reflection=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                session/after_render['chromium'],mode=request.mode,user_instruction=request.instruction,
                phase='reflect',before_render=session/(last_render.get('chromium') or last_render['libreoffice']),page_history=iterations,
                tool_feedback=feedback,source_regions=source_regions,verified_evidence=verified_evidence,
                api_key=key,model=request.model)
            model_elapsed=time.perf_counter()-model_started
            models.append({'phase':'reflect','elapsed_seconds':round(model_elapsed,3),'response':reflection})
            if stop_event.is_set():
                iterations.append({'round':index,'tool_calls':calls,'tool_events':events,'before_render':last_render,
                    'after_render':after_render,'reflection':reflection,'outcome':'stopped_after_reflection'})
                candidate=prior
                status='candidate_ready' if accepted_any else 'stopped'
                last_reason='收到停止请求；本轮未作为已验收结果保留，已回滚本轮修改。'+('保留此前已验收候选。' if accepted_any else '')
                break
            if request.mode!='free':
                if isinstance(reflection.get('issues'),list):
                    visual_issues=_merge_visual_agent_issues(visual_issues,reflection['issues'])
                elif isinstance((reflection.get('reflection') or {}).get('issues'),list):
                    visual_issues=_merge_visual_agent_issues(visual_issues,(reflection.get('reflection') or {}).get('issues'))
                visual_issues=_bind_visual_missing_evidence(visual_issues,verified_evidence)
            set_progress('reviewing',f'DeepSeek 正在决定接受、继续修改或回滚 {issue_id}',active_issue,attempt_no,visual_issues,index)
            decision=reflection.get('decision');last_reason=str(reflection.get('reason',''))[:600]
            row={'round':index,'tool_calls':calls,'tool_events':events,'before_render':last_render,
                'after_render':after_render,'reflection':reflection,'outcome':decision,
                'timings_seconds':{'tool_and_object_edit':round(tool_elapsed,3),
                    'chromium_update_and_capture':round(preview_elapsed,3),
                    'deepseek_reflection_request_and_inference':round(model_elapsed,3)}}
            iterations.append(row)
            if decision!='accept' and index>=2:
                # Keep the latest fully rendered candidate after the second
                # round. Do not label unresolved issues as fixed; the user can
                # inspect and decide whether to save the candidate.
                last_render=after_render
                retained=[]
                for pending_issue in (active_batch_issues or [active_issue]):
                    if str(pending_issue.get('status','')).casefold() not in {x.casefold() for x in resolved_states}:
                        pending_issue.update(status='待复核',skip_reason='已完成两轮合批修改与渲染；最终候选已保留，模型未确认该项完全修复。')
                    retained.append(pending_issue)
                visual_issues=_merge_visual_agent_issues(visual_issues,retained)
                queued_plans.consume(active_batch_issues)
                accepted_any=accepted_any or signature(find_slide(candidate,slide['id']))!=signature(find_slide(prior,slide['id']))
                if accepted_any:
                    last_accepted_candidate=deepcopy(candidate);last_accepted_render=deepcopy(after_render)
                    status='candidate_ready'
                else:
                    status='requires_review'
                row['outcome']='retained_after_two_rounds'
                last_reason='已完成两轮修改与渲染并保留最终隔离候选；未获模型确认的问题仍标为待复核。正式项目尚未保存。'
                break
            if len(active_batch_issues)>1 and decision!='accept' and index<2:
                if decision=='rollback':
                    candidate=prior
                    last_render=last_accepted_render
                else:
                    last_render=after_render
                retry_feedback={'current_issue':active_issue,'batch_issues':deepcopy(active_batch_issues),
                    'remaining_issues':deepcopy(active_batch_issues),'issues':deepcopy(active_batch_issues),
                    'accepted_issues':list(completed_issue_ids),'attempt_number':attempt_no+1,
                    'max_attempts_per_issue':2,
                    'instruction':'这是第二轮整批修复。请一次覆盖batch_issues中所有仍未解决的问题，不要只修改第一个问题；保持互不冲突的编辑合并。'}
                # Reflection already sees the latest screenshot and can return
                # the next batch in the same response. Avoid another full-page
                # request just to ask for the very same plan again. Rollback
                # proposals need a fresh plan against the restored baseline.
                reflected_calls=issue_calls(reflection.get('tool_calls',[]),active_issue)
                if decision=='continue' and not reflected_calls:
                    reflected_plans=RepairPlanQueue()
                    reflected_plans.add(reflection.get('repair_plans'),find_slide(candidate,slide['id']))
                    reflected_calls=reflected_plans.peek_batch(active_batch_issues,
                        find_slide(candidate,slide['id']),limit=len(active_batch_issues),max_calls=1000)['calls']
                if decision=='continue' and reflected_calls:
                    calls=reflected_calls
                    last_reason='已复用本轮截图复核同时给出的整批修复方案，直接执行第二轮。'
                    continue
                retry_started=time.perf_counter()
                retry=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                    session/(last_render.get('chromium') or last_render.get('libreoffice') or before_render['chromium']),
                    mode=request.mode,user_instruction=request.instruction,phase='diagnose',page_history=iterations,
                    tool_feedback=retry_feedback,source_regions=source_regions,verified_evidence=verified_evidence,
                    api_key=key,model=request.model)
                models.append({'phase':'batch_retry_plan','elapsed_seconds':round(time.perf_counter()-retry_started,3),
                    'response':retry,'batch_issue_ids':[x.get('issue_id') for x in active_batch_issues]})
                if retry.get('agent_error'):
                    for pending_issue in active_batch_issues:
                        pending_issue.update(status='待复核',skip_reason='第二轮合批计划请求失败；已保留上一版隔离候选。')
                    accepted_any=accepted_any or signature(find_slide(candidate,slide['id']))!=signature(find_slide(prior,slide['id']))
                    if accepted_any:
                        last_accepted_candidate=deepcopy(candidate)
                        last_accepted_render=deepcopy(last_render)
                    status='candidate_ready' if accepted_any else 'requires_review'
                    last_reason='第二轮合批计划请求失败；保留当前候选并停止。'+str(retry.get('reason') or '')
                    break
                retry_calls=issue_calls(retry.get('tool_calls',[]),active_issue)
                # Prefer the second-round per-issue plans and collect the
                # compatible set against the now-current candidate state.
                queued_plans.add(retry.get('repair_plans'),find_slide(candidate,slide['id']))
                retry_bundle=queued_plans.peek_batch(active_batch_issues,
                    find_slide(candidate,slide['id']),limit=len(active_batch_issues),max_calls=1000)
                if retry_bundle['calls']:
                    retry_calls=retry_bundle['calls']
                if retry_calls:
                    calls=retry_calls
                    attempts_by_issue[issue_id]=max(0,attempts_by_issue.get(issue_id,1)-1)
                    last_reason='第一轮未全部通过复核；已为整个问题批次生成第二轮方案。'
                    continue
                for pending_issue in active_batch_issues:
                    pending_issue.update(status='待复核',skip_reason='第二轮未返回可执行的合批修改；保留当前候选。')
                accepted_any=accepted_any or signature(find_slide(candidate,slide['id']))!=signature(find_slide(prior,slide['id']))
                if accepted_any:
                    last_accepted_candidate=deepcopy(candidate)
                    last_accepted_render=deepcopy(last_render)
                status='candidate_ready' if accepted_any else 'requires_review'
                last_reason='第一轮未通过，第二轮也没有返回合批修改；保留当前候选供人工复核。'
                break
            if decision=='rollback':
                candidate=prior
                status='candidate_ready' if accepted_any else 'rolled_back'
                active_issue['status']='待复核';completed_issue_ids.add(issue_id)
                last_reason='当前尝试使效果变差，已回滚该次修改；转向下一个问题。'
                calls=[]
            if decision=='accept':
                reported_confidence=float(reflection.get('confidence') or 0)
                reflected_issues=reflection.get('issues') or (reflection.get('reflection') or {}).get('issues') or []
                reflected=next((x for x in reflected_issues if isinstance(x,dict) and x.get('issue_id')==issue_id),{})
                reported_confidence=max(reported_confidence,float(reflected.get('confidence') or 0))
                # DeepSeek's explicit accept follows its inspection of the
                # newly rendered screenshot. Confidence is retained for audit,
                # but a numeric threshold must not veto that decision.
                accepted_batch=active_batch_issues or [active_issue]
                for accepted_issue in accepted_batch:
                    accepted_issue['status']='已修复'
                    completed_issue_ids.add(accepted_issue.get('issue_id'))
                queued_plans.consume(accepted_batch)
                accepted_any=True
                last_render=after_render
                last_accepted_candidate=deepcopy(candidate)
                last_accepted_render=deepcopy(after_render)
                last_reason=(f'{len(accepted_batch)} 个问题已由当前模型在同一张真实截图复核后确认改善（信心 {reported_confidence:.2f}）；继续处理下一项。'
                    if len(accepted_batch)>1 else f'问题 {issue_id} 已由当前模型在真实截图复核后确认改善（信心 {reported_confidence:.2f}）；继续处理下一项。')
                calls=[]
                active_batch_issues=[]
                if request.mode=='free':
                    status='candidate_ready' if accepted_any else 'requires_review'
                    if accepted_any:last_reason=f'自由编辑指令已通过真实渲染复核，候选保存在隔离区：{last_reason}'
                    break
                pending=sorted(unresolved_issues(visual_issues),key=issue_sort_key)
                # A combined issue + whole-page review is sufficient evidence
                # for the visual gate. Older responses without this explicit
                # field still receive the separate full-page check below.
                if reflection.get('page_review_passed') is True and not pending:
                    global_visual_check_passed=True
                    status='candidate_ready'
                    last_reason='本批修改与整页视觉检查已在同一次截图复核中通过；接下来执行本地 PPTX 验收。'
                    break
                queued=take_queued_batch(pending) if pending else {'issues':[],'calls':[]}
                if queued['calls']:
                    active_batch_issues=queued['issues'];active_issue=active_batch_issues[0];calls=queued['calls']
                    set_progress('using_queued_plan',f'合并复用 {len(active_batch_issues)} 项已提出的修复计划，统一渲染复核',active_issue,issues=visual_issues,iteration=index)
                    continue
                # Re-audit the whole page even when the first diagnosis listed
                # only the accepted issue. This catches omitted problems after
                # each fix instead of treating an incomplete list as complete.
                if index<max_total_attempts:
                    next_started=time.perf_counter()
                    next_turn=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                        session/after_render['chromium'],mode=request.mode,user_instruction=request.instruction,
                        phase='diagnose',page_history=iterations,
                        tool_feedback={'accepted_issues':list(completed_issue_ids),'remaining_issues':pending,
                            'current_issue':pending[0] if pending else None,
                            'instruction':('已完成上一项；请直接给出队列下一项的具体修复，观察整页是否回归。' if pending else
                                '已知问题已处理完，请重新检查整页，确认没有漏项和新增回归后才能返回done。'),
                            'max_attempts_per_issue':request.max_attempts_per_issue},
                        source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model)
                    models.append({'phase':'diagnose','elapsed_seconds':round(time.perf_counter()-next_started,3),'response':next_turn})
                    if not pending:
                        found=(next_turn.get('diagnosis') or {}).get('issues') or []
                        global_visual_check_passed=bool(not next_turn.get('agent_error') and
                            not next_turn.get('tool_calls') and all(str(i.get('status','')).strip().casefold()
                            in {x.casefold() for x in resolved_states} for i in found if isinstance(i,dict)))
                    if next_turn.get('agent_error'):
                        error_reason=str(next_turn.get('reason') or next_turn.get('agent_error'))
                        for row in pending:
                            row.update(status='待复核',skip_reason=error_reason)
                            completed_issue_ids.add(row.get('issue_id'))
                        visual_issues=_merge_visual_agent_issues(visual_issues,pending)
                        active_issue=None;calls=[]
                        status='candidate_ready' if accepted_any else 'requires_review'
                        last_reason='DeepSeek 请求失败或超时；为避免拖慢整页，其余问题已标为待复核并结束本轮。'+error_reason
                        break
                    newly=(next_turn.get('diagnosis') or {}).get('issues',[])
                    queued_plans.add(next_turn.get('repair_plans'),find_slide(candidate,slide['id']))
                    if isinstance(newly,list) and newly:
                        visual_issues=_merge_visual_agent_issues(visual_issues,
                            _bind_visual_missing_evidence(newly,verified_evidence))
                    pending=sorted(unresolved_issues(visual_issues),key=issue_sort_key)
                    active_issue=pending[0] if pending else None
                    next_bundle=take_queued_batch(pending) if pending else {'issues':[],'calls':[]}
                    if next_bundle['calls']:
                        calls=next_bundle['calls'];active_batch_issues=next_bundle['issues'];active_issue=active_batch_issues[0]
                    else:
                        calls=issue_calls(next_turn.get('tool_calls',[]),active_issue)
                        active_batch_issues=deepcopy(pending) if active_issue and calls else ([active_issue] if active_issue else [])
                    last_reason=str(next_turn.get('reason') or last_reason)[:600]
                    if active_issue and not calls:
                        # Ask once for actions constrained to the selected issue; never execute another issue's edits.
                        next_started=time.perf_counter()
                        next_turn=await visual_turn(candidate,find_slide(candidate,slide['id']),design_path,
                            session/after_render['chromium'],mode=request.mode,user_instruction=request.instruction,
                            phase='diagnose',page_history=iterations,
                            tool_feedback={'current_issue':active_issue,'remaining_issues':pending,
                                'accepted_issues':list(completed_issue_ids),'max_attempts_per_issue':request.max_attempts_per_issue},
                            source_regions=source_regions,verified_evidence=verified_evidence,api_key=key,model=request.model)
                        models.append({'phase':'diagnose','elapsed_seconds':round(time.perf_counter()-next_started,3),'response':next_turn})
                        queued_plans.add(next_turn.get('repair_plans'),find_slide(candidate,slide['id']))
                        calls=issue_calls(next_turn.get('tool_calls',[]),active_issue)
                        active_batch_issues=deepcopy(pending) if active_issue and calls else ([active_issue] if active_issue else [])
                    if active_issue and calls:continue
                status='candidate_ready' if accepted_any else 'requires_review'
                break
            if decision!='continue':
                # A non-accept reflection is not permission for the application
                # to erase edits the model already made. Only an explicit
                # `rollback` decision above restores the previous candidate;
                # otherwise keep the rendered edits for the user's save/review.
                changed_this_round=(signature(find_slide(candidate,slide['id'])) !=
                                    signature(find_slide(prior,slide['id'])))
                if changed_this_round:
                    last_render=after_render
                    accepted_any=True
                    last_accepted_candidate=deepcopy(candidate)
                    last_accepted_render=deepcopy(after_render)
                status='candidate_ready' if accepted_any else 'requires_review'
                last_reason=('AI 本轮修改已保留并完成渲染，未被自动回滚；请查看候选后手动保存。' +
                    (f' {last_reason}' if last_reason else ''))
                if request.mode=='restore' and not accepted_any:
                    active_issue['status']='待复核'
                break
            next_state=signature(find_slide(candidate,slide['id']))
            seen=seen_candidate_states.setdefault(state_key,set())
            if next_state in seen:
                repeated_candidate_states[state_key]=repeated_candidate_states.get(state_key,0)+1
            seen.add(next_state)
            if repeated_candidate_states.get(state_key,0)>=2:
                last_render=after_render
                accepted_any=accepted_any or signature(find_slide(candidate,slide['id']))!=signature(find_slide(prior,slide['id']))
                if accepted_any:
                    last_accepted_candidate=deepcopy(candidate);last_accepted_render=deepcopy(after_render)
                last_reason='本轮候选与此前状态重复；保留最新渲染结果并停止继续调用，避免重复收费。可手动复核并保存。'
                status='candidate_ready' if accepted_any else 'requires_review'
                break
            last_render=after_render
            calls=issue_calls(reflection.get('tool_calls',[]),active_issue)
            if calls and reflection.get('decision')=='continue':
                next_issue,next_calls=retarget_misbound_edits(calls,active_issue)
                reflection_confidence=float(reflection.get('confidence') or 0)
                if next_issue and next_calls:
                    previous_issue=active_issue
                    previous_issue.update(status='已修复',ai_reason=(
                        f'DeepSeek 视觉复核信心 {reflection_confidence:.2f}，并明确给出了属于下一问题 '
                        f'{next_issue.get("issue_id")} 的对象编辑；系统按对象ID安全切换问题。'))
                    completed_issue_ids.add(previous_issue.get('issue_id'))
                    visual_issues=_merge_visual_agent_issues(visual_issues,[previous_issue])
                    accepted_any=True
                    last_accepted_candidate=deepcopy(candidate)
                    last_accepted_render=deepcopy(after_render)
                    active_issue=next_issue;calls=next_calls
                    attempts_by_issue.setdefault(active_issue.get('issue_id'),0)
                    last_reason=(f'{previous_issue.get("issue_id")} 已通过视觉复核；模型的下一组编辑实际对应 '
                        f'{active_issue.get("issue_id")}，已自动切换继续优化。')
                    set_progress('editing',f'已完成 {previous_issue.get("issue_id")}，正在优化 {active_issue.get("issue_id")}',
                        active_issue,1,visual_issues,index)
                    continue
            if not calls:
                last_reason='当前问题仍未达接受门槛，且模型未提供新的参数尝试；已标为待复核。'
                if await skip_and_advance(last_reason,after_render):continue
                status='candidate_ready' if accepted_any else 'requires_review';break
        if spending.paused_reason:
            candidate=deepcopy(last_accepted_candidate)
            last_render=deepcopy(last_accepted_render)
        final_validation_elapsed=0.0
        remaining_visual=[i for i in visual_issues if str(i.get('status','')).strip().casefold()
            not in {x.casefold() for x in resolved_states}]
        final_candidate_changed=(signature({'slide':find_slide(candidate,slide['id'])}) !=
                                 signature({'slide':find_slide(p,slide['id'])}))
        if final_candidate_changed and not stop_event.is_set():
            set_progress('final_validation','问题迭代已完成，正在做最终 PPTX/LibreOffice 整页验收',issues=visual_issues)
            final_validation_started=time.perf_counter()
            final_render=await asyncio.to_thread(_render_visual_agent_candidate,p,candidate,slide['id'],session,'final-validation')
            final_validation_elapsed=time.perf_counter()-final_validation_started
            gate=final_render.get('browser_qa') or {};pptx_gate=final_render.get('pptx_report') or {}
            final_ok=(not gate.get('fatal_error') and gate.get('layout_contract_pass') is True and
                gate.get('pptx_pass') is True and gate.get('workflow_pass') is True and
                (pptx_gate.get('native_text') or {}).get('ok') is True and
                (pptx_gate.get('saved_layout_check') or {}).get('ok') is True and
                not remaining_visual and (request.mode=='free' or global_visual_check_passed))
            if final_ok:
                last_render=final_render;status='candidate_ready'
                last_reason='逐问题浏览器迭代完成；整页 PPTX、Chromium 和 LibreOffice 最终验收通过。'+last_reason
            else:
                last_render=final_render;status='requires_review'
                last_reason=('逐问题候选尚未通过最终交付门槛；已接受的问题修改保留在隔离候选中，正式项目未改。'
                    if unresolved_issues(visual_issues) else
                    '逐问题候选未通过最终整页 PPTX/Chromium 验收；已接受的问题修改保留在隔离候选中，正式项目未改。')
                if request.mode=='restore' and not global_visual_check_passed:
                    last_reason='候选尚未完成最终整页视觉复核；已验收修改已保留，可续修补做检查。'
        # A successfully rendered candidate is the user's requested final
        # result even when the model did not explicitly mark every issue as
        # fixed. Rollbacks and stop requests already restore `candidate` at
        # their decision points above; resetting it here silently discarded
        # valid first-round edits after a no-op/rejected second round.
        if spending.paused_reason:
            last_reason=spending.paused_reason+'；已暂停收费调用，已验收候选和剩余问题已保存，可从断点续修。'
            status='requires_review'
        candidate['id']=pid
        # User-facing run details must name the provider actually selected in
        # settings. The legacy call_deepseek function serves several vendors,
        # so old canned status text can otherwise mislabel Qwen/GLM runs.
        def actual_provider_text(value):
            if isinstance(value,str):return value.replace('DeepSeek',active_model_label)
            if isinstance(value,list):return [actual_provider_text(item) for item in value]
            if isinstance(value,dict):return {key:actual_provider_text(item) for key,item in value.items()}
            return value
        visual_issues=actual_provider_text(visual_issues)
        models=actual_provider_text(models)
        iterations=actual_provider_text(iterations)
        last_reason=actual_provider_text(last_reason)
        safe_json_write(session/'candidate_project.json',candidate)
        model_total=round(sum(float(x.get('elapsed_seconds') or 0) for x in models),3)
        run_wall_total=round(time.perf_counter()-run_wall_started,3)
        measured_total=(model_total+final_validation_elapsed+sum(phase_timings.values()))
        phase_timings={key:round(value,3) for key,value in phase_timings.items()}
        phase_timings.update({'model_requests_total':model_total,
            'final_pptx_and_full_qa':round(final_validation_elapsed,3),
            'run_wall_total':run_wall_total,
            'other_unattributed':round(max(0.0,run_wall_total-measured_total),3)})
        record={'token':token,'project_id':pid,'slide_id':slide['id'],'mode':request.mode,
            'provider_model_label':active_model_label,
            'run_id':run_id,
            'base_revision':p['revision'],'base_signature':signature_before,'candidate_signature':signature(candidate),
            'base_slide_signature':signature({'slide':slide}),
            'base_candidate_token':request.base_candidate_token,
            'status':status,'issues':visual_issues,'issue_count':len(visual_issues),'models':models,'iterations':iterations,
            'timings_seconds':phase_timings,
            'last_render':last_render,'reason':last_reason,'created_at':time.time(),
            'applied':False,'user_confirmation_required':True}
        record['cost_control']=spending.snapshot()
        record['queued_plan_hits']=queued_plans.hits
        record['final_visual_check_passed']=global_visual_check_passed
        record['can_continue']=request.mode=='restore' and bool(remaining_visual or spending.paused_reason or not global_visual_check_passed)
        record['remaining_issue_count']=len(remaining_visual)
        safe_json_write(session/'status.json',record)
        record_task_outcome(task_id=run_id,project_id=pid,slide_id=slide['id'],module='visual_agent',
            status=status,repair_success=(status in {'candidate_ready','applied'}),duration_ms=round(run_wall_total*1000),
            details={'issue_count':len(visual_issues),'accepted_issue_count':sum(1 for issue in visual_issues if issue.get('status')=='已修复'),
                     'validated':bool(record.get('final_visual_check_passed')),
                     'formal_project_applied':bool(record.get('applied'))})
        _visual_agent_runs[run_id]['running']=False
        terminal_progress={'phase':status,'message':last_reason or status,
            'slide_id':slide['id'],'issues':visual_issues,'iteration':len(iterations),
            'cost_control':spending.snapshot()}
        latest_preview=_visual_agent_runs[run_id].get('_live_preview')
        if latest_preview:
            terminal_progress['preview']=deepcopy(latest_preview)
        _visual_agent_runs[run_id]['progress']=terminal_progress
        return record
    except HTTPException:
        _visual_agent_runs[run_id]['running']=False
        raise
    except Exception as e:
        _visual_agent_runs[run_id]['running']=False
        fail(e)
    finally:
        if preview_worker is not None:
            preview_worker.active_run_session=None
        reset_budget(spending_token)
        reset_api_usage_context(usage_context_token)


@app.get('/api/projects/{pid}/visual-agent/latest')
def visual_agent_latest(pid:str,slide_id:str|None=None):
    from .repair_pipeline import signature
    p=p_or_404(pid);root=project_dir(pid)/'build'/'visual_agent'
    if not root.is_dir():return None
    current=signature(p)
    current_slide_signature=(signature({'slide':find_slide(p,slide_id)}) if slide_id else None)
    for path in sorted(root.glob('*/status.json'),key=lambda item:item.stat().st_mtime,reverse=True):
        try:row=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError):continue
        same_slide=not slide_id or row.get('slide_id')==slide_id
        page_baseline_matches=bool(current_slide_signature and row.get('base_slide_signature')==current_slide_signature)
        legacy_baseline_matches=row.get('base_signature')==current
        if same_slide and (((page_baseline_matches or legacy_baseline_matches) and row.get('status') not in {'discarded','applied','undone'}) or
                (row.get('status')=='applied' and (row.get('applied_revision')==p.get('revision') or
                    (current_slide_signature and row.get('applied_slide_signature')==current_slide_signature)))):return row
    return None


@app.get('/api/projects/{pid}/visual-agent/{token}')
def visual_agent_status(pid:str,token:str):
    if not re.fullmatch('[a-f0-9]{32}',token):raise HTTPException(404,'候选不存在')
    folder=project_dir(pid)/'build'/'visual_agent'/token
    if not folder.is_dir():raise HTTPException(404,'候选不存在')
    return json.loads((folder/'status.json').read_text(encoding='utf-8'))


@app.get('/api/projects/{pid}/visual-agent/{token}/page-download')
def visual_agent_page_download(pid:str,token:str):
    """Build a verified one-slide PPTX from an isolated page candidate."""
    from .repair_pipeline import signature
    if not re.fullmatch('[a-f0-9]{32}',token):raise HTTPException(404,'候选不存在')
    folder=project_dir(pid)/'build'/'visual_agent'/token
    try:
        record=json.loads((folder/'status.json').read_text(encoding='utf-8'))
        if record.get('project_id')!=pid:raise ValueError('候选与当前项目不匹配')
        status_value=record.get('status')
        if status_value in {'discarded','undone'}:raise ValueError('该候选已丢弃或撤销，不能下载')
        if status_value=='applied':
            # After manual save, download the currently saved formal page,
            # including any later edits to that page.
            page_project=p_or_404(pid)
            source=find_slide(page_project,record.get('slide_id'))
        else:
            candidate=json.loads((folder/'candidate_project.json').read_text(encoding='utf-8'))
            if signature(candidate)!=record.get('candidate_signature'):raise ValueError('候选数据哈希校验失败')
            source=find_slide(candidate,record.get('slide_id'))
            page_project=deepcopy(candidate)
        page_project=deepcopy(page_project);page_project['slides']=[deepcopy(source)];page_project['pages']=1
        page_project['slides'][0]['number']=1
        output=folder/'page_candidate.pptx'
        build_runtime_pptx(page_project,output,'editable',base_builder=build_python_pptx)
        native=check_native_text(page_project,output)
        layout=check_saved_layout(page_project,output)
        if not native.get('ok'):raise ValueError('单页候选 PPTX 原生文字检查失败：'+str(native.get('slides')))
        if not layout.get('ok'):raise ValueError('单页候选 PPTX 布局参数检查失败：'+str(layout.get('pages')))
        digest=_sha256_file(output)
        record['page_download']={'filename':output.name,'artifact_sha256':digest,'slide_id':source['id'],
            'candidate_signature':record['candidate_signature'],'native_text':native,'saved_layout_check':layout}
        safe_json_write(folder/'status.json',record)
        download_name=(f"{source['id']}_saved.pptx" if status_value=='applied' else f"{source['id']}_optimized_candidate.pptx")
        return FileResponse(output,filename=download_name,
            media_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
            headers={'Cache-Control':'no-store','X-Artifact-SHA256':digest,'X-Slide-ID':source['id']})
    except Exception as e:fail(e)


@app.post('/api/projects/{pid}/visual-agent/{token}/issues/{issue_id}/decision')
def decide_visual_agent_issue(pid:str,token:str,issue_id:str,request:VisualAgentIssueDecisionRequest):
    if not re.fullmatch('[a-f0-9]{32}',token):raise HTTPException(404,'候选不存在')
    if request.status!='fixed':raise HTTPException(400,'目前只支持人工标记为 fixed')
    folder=project_dir(pid)/'build'/'visual_agent'/token
    try:
        record=json.loads((folder/'status.json').read_text(encoding='utf-8'))
        issue=next((row for row in record.get('issues',[]) if isinstance(row,dict) and row.get('issue_id')==issue_id),None)
        if issue is None:raise HTTPException(404,'问题不存在')
        if issue.get('status') not in {'待定','pending','open','待复核'}:
            raise HTTPException(409,'该问题当前状态不支持人工标记')
        issue.update(status='已修复',human_status='fixed',human_reviewed=True,human_reviewed_at=time.time())
        safe_json_write(folder/'status.json',record)
        return record
    except HTTPException:raise
    except Exception as e:fail(e)


@app.get('/api/projects/{pid}/visual-agent/{token}/files/{filename:path}')
def visual_agent_file(pid:str,token:str,filename:str):
    if not re.fullmatch('[a-f0-9]{32}',token):raise HTTPException(404,'候选不存在')
    folder=(project_dir(pid)/'build'/'visual_agent'/token).resolve();path=(folder/filename).resolve()
    if not path.is_relative_to(folder) or not path.is_file() or path.suffix.lower() not in {'.png','.pptx','.pdf','.json','.html'}:
        raise HTTPException(404,'候选文件不存在')
    return FileResponse(path,headers={'Cache-Control':'no-cache'})


@app.post('/api/projects/{pid}/visual-agent/{token}/discard')
def discard_visual_agent_candidate(pid:str,token:str):
    if not re.fullmatch('[a-f0-9]{32}',token):raise HTTPException(404,'候选不存在')
    folder=project_dir(pid)/'build'/'visual_agent'/token
    try:
        record=json.loads((folder/'status.json').read_text(encoding='utf-8'))
        if record.get('applied'):raise ValueError('已应用的候选不能丢弃')
        record.update(status='discarded',discarded_at=time.time())
        safe_json_write(folder/'status.json',record)
        return record
    except Exception as e:fail(e)


@app.post('/api/projects/{pid}/visual-agent/{token}/apply')
def apply_visual_agent_candidate(pid:str,token:str,request:VisualAgentApplyRequest):
    lock=_visual_agent_apply_locks.setdefault(pid,threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409,'当前项目正在应用另一页的候选；本次没有写入，请稍后重试。')
    try:
        return _apply_visual_agent_candidate_locked(pid,token,request.confirm_review)
    finally:
        lock.release()


def _apply_visual_agent_candidate_locked(pid:str,token:str,confirm_review:bool=False):
    from .repair_pipeline import signature
    if not re.fullmatch('[a-f0-9]{32}',token):raise HTTPException(404,'候选不存在')
    folder=project_dir(pid)/'build'/'visual_agent'/token
    try:
        record=json.loads((folder/'status.json').read_text(encoding='utf-8'))
        current=p_or_404(pid)
        status_value=record.get('status')
        if status_value not in {'candidate_ready','requires_review','iteration_limit','stopped','rolled_back'}:
            raise ValueError('当前没有可保存的优化候选')
        if status_value!='candidate_ready' and not confirm_review:
            raise ValueError('候选仍需人工复核；请在确认提示中明确同意保存')
        candidate=json.loads((folder/'candidate_project.json').read_text(encoding='utf-8'))
        if signature(candidate)!=record.get('candidate_signature'):raise ValueError('候选数据已变化，不能应用')
        slide_id=record.get('slide_id')
        candidate_slide=find_slide(candidate,slide_id)
        current_slide=find_slide(current,slide_id)
        if record.get('base_slide_signature'):
            if signature({'slide':current_slide})!=record['base_slide_signature']:
                raise ValueError('当前页正式内容自生成候选后已变化；为避免覆盖该页新修改，请重新优化。其他页面的保存不会阻止本页提交。')
        elif signature(current)!=record.get('base_signature'):
            raise ValueError('正式项目版本已变化，旧候选缺少逐页基线校验；请重新生成当前页候选。')
        if signature({'slide':candidate_slide})==signature({'slide':current_slide}):
            raise ValueError('候选与正式页完全相同，没有本页优化修改可保存。')
        backup=folder/'preapply_backup.json';safe_json_write(backup,current)
        expected_revision=current['revision']+1
        # Page-scoped commit: graft only this slide onto the latest project so
        # concurrently optimized/saved pages are preserved.
        target_index=next(i for i,row in enumerate(current['slides']) if row['id']==slide_id)
        merged=deepcopy(current);merged['slides'][target_index]=deepcopy(candidate_slide)
        merged['id']=pid
        save_project(merged)
        applied_project=read_project(pid)
        safe_json_write(folder/'postapply_snapshot.json',applied_project)
        try:report=render_deck(pid,'editable')
        except Exception:
            safe_json_write(project_dir(pid)/'project.json',current)
            raise
        if report.get('project_revision')!=expected_revision or not report.get('artifact_sha256'):
            safe_json_write(project_dir(pid)/'project.json',current)
            raise ValueError('正式版本导出与候选版本不一致，已恢复原项目文件')
        record.update(status='applied',applied=True,
            human_approved_review=bool(confirm_review),
            applied_revision=report['project_revision'],
            applied_signature=signature(applied_project),
            applied_slide_signature=signature({'slide':find_slide(applied_project,slide_id)}),export_filename=report['filename'],
            export_sha256=report['artifact_sha256'],applied_at=time.time())
        safe_json_write(folder/'status.json',record)
        return {'status':'applied','project_revision':report['project_revision'],'export':report}
    except Exception as e:fail(e)


@app.post('/api/projects/{pid}/visual-agent/{token}/undo')
def undo_visual_agent_candidate(pid:str,token:str):
    """Restore the exact pre-apply project only while the applied revision is still current."""
    from .repair_pipeline import signature
    if not re.fullmatch('[a-f0-9]{32}',token):raise HTTPException(404,'候选不存在')
    folder=project_dir(pid)/'build'/'visual_agent'/token
    try:
        record=json.loads((folder/'status.json').read_text(encoding='utf-8'))
        current=p_or_404(pid)
        if record.get('status')!='applied' or not record.get('applied'):
            raise ValueError('只有最近已应用的 Visual Agent 候选可以撤销')
        if current.get('revision')!=record.get('applied_revision') or signature(current)!=record.get('applied_signature'):
            raise ValueError('项目在应用候选后又发生变化；为避免覆盖新修改，无法自动撤销')
        backup=json.loads((folder/'preapply_backup.json').read_text(encoding='utf-8'))
        backup['id']=pid
        backup['revision']=current['revision']
        save_project(backup)
        try:
            report=render_deck(pid,'editable')
        except Exception:
            safe_json_write(project_dir(pid)/'project.json',current)
            raise
        if report.get('project_revision')!=current['revision']+1 or not report.get('artifact_sha256'):
            safe_json_write(project_dir(pid)/'project.json',current)
            raise ValueError('撤销版本导出与项目修订号不一致；已恢复撤销前项目')
        record.update(status='undone',undone=True,undo_revision=report['project_revision'],
            undo_export_filename=report['filename'],undo_export_sha256=report['artifact_sha256'],
            undone_at=time.time())
        safe_json_write(folder/'status.json',record)
        return {'status':'undone','project_revision':report['project_revision'],'export':report}
    except Exception as e:fail(e)

@app.get('/api/projects/{pid}/repair/latest')
def latest_repair(pid:str):
    from .repair_pipeline import signature
    p=p_or_404(pid);root=project_dir(pid)/'build'/'repairs'
    if not root.exists():return None
    for file in sorted(root.glob('*/status.json'),key=lambda x:x.stat().st_mtime,reverse=True):
        data=json.loads(file.read_text())
        if data.get('candidate_signature') and (data.get('base_signature')==signature(p) or
            data.get('committed') and data.get('committed_signature')==signature(p)):return data
    return None

@app.get('/api/projects/{pid}/repair/{token}')
def repair_status(pid:str,token:str):
    from .repair_pipeline import get
    try:return get(pid,token)
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/repair/{token}/validate')
def repair_validate(pid:str,token:str):
    from .repair_pipeline import validate
    try:return validate(pid,token)
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/repair/{token}/commit')
def repair_commit(pid:str,token:str):
    from .repair_pipeline import commit
    try:return commit(pid,token)
    except Exception as e:fail(e)

@app.get('/api/projects/{pid}/repair/{token}/files/{filename:path}')
def repair_file(pid:str,token:str,filename:str):
    from .repair_pipeline import session_dir
    try:
        folder=session_dir(pid,token).resolve();file=(folder/filename).resolve()
        if not file.is_relative_to(folder) or not file.is_file() or file.suffix not in ('.html','.json','.png','.pptx','.pdf'):
            raise ValueError('修复文件不可访问')
        return FileResponse(file,headers={'Cache-Control':'no-cache'})
    except Exception as e:fail(e)

def _visual_agent_export_blocker(pid:str,project:dict[str,Any])->str|None:
    """Require pending page candidates to be saved or discarded before export."""
    from .repair_pipeline import signature
    for slide in project.get('slides',[]):
        active=_active_visual_agent_run(pid,slide.get('id'))
        if active and active.get('running'):
            return f"{slide.get('id')} 的 AI 优化仍在运行；请等两轮完成后保存或停止，再导出正式 PPTX。"
    root=project_dir(pid)/'build'/'visual_agent'
    if not root.is_dir():return None
    current_project_signature=signature(project)
    current_slides={row.get('id'):row for row in project.get('slides',[])}
    seen=set()
    terminal={'applied','discarded','undone'}
    for status_file in sorted(root.glob('*/status.json'),key=lambda item:item.stat().st_mtime,reverse=True):
        try:row=json.loads(status_file.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError):continue
        sid=row.get('slide_id')
        if row.get('project_id')!=pid or sid not in current_slides or sid in seen:continue
        slide=current_slides[sid]
        base_matches=(row.get('base_slide_signature')==signature({'slide':slide}) if row.get('base_slide_signature')
                      else row.get('base_signature')==current_project_signature)
        if not base_matches:continue
        seen.add(sid)
        if row.get('status') in terminal:continue
        candidate_file=status_file.parent/'candidate_project.json'
        if not candidate_file.is_file():continue
        try:candidate=json.loads(candidate_file.read_text(encoding='utf-8'))
        except (OSError,json.JSONDecodeError):continue
        try:changed=signature({'slide':find_slide(candidate,sid)})!=signature({'slide':slide})
        except Exception:changed=False
        if changed:
            return f'{sid} 有尚未保存的 AI 优化候选。请先点击“保存修复结果”；若不采用，请先丢弃候选，再导出。'
    return None


@app.post('/api/projects/{pid}/export')
def export(pid:str,mode:str='editable'):
    try:
        project=p_or_404(pid)
        pending=_visual_agent_export_blocker(pid,project)
        if pending:raise HTTPException(409,pending)
        return render_deck(pid,mode)
    except Exception as e:fail(e)

@app.get('/api/projects/{pid}/export-download')
def export_download(pid:str,mode:str='editable'):
    """Generate the current deck and return the verified PPTX as a download.

    This native navigation endpoint keeps the browser's user-initiated
    download permission alive while a potentially long export is running.
    """
    if mode not in {'editable','raster'}:raise HTTPException(400,'mode 必须是 editable 或 raster')
    try:
        project=p_or_404(pid)
        pending=_visual_agent_export_blocker(pid,project)
        if pending:raise HTTPException(409,pending)
        report=render_deck(pid,mode)
        project=p_or_404(pid)
        verified=_current_export_report(pid,project,mode)
        if (verified.get('filename')!=report.get('filename') or
                verified.get('artifact_sha256')!=report.get('artifact_sha256')):
            raise ValueError('导出文件与最新报告不一致，未提供下载。')
        path=project_dir(pid)/'build'/verified['filename']
        return FileResponse(path,filename=verified['filename'],media_type=
            'application/vnd.openxmlformats-officedocument.presentationml.presentation' if mode=='editable' else 'application/octet-stream',
            headers={'Cache-Control':'no-store'})
    except Exception as e:fail(e)

@app.get('/api/wps-environment')
def get_wps_environment():return wps_environment()

@app.post('/api/projects/{pid}/wps-render')
async def upload_wps_render(pid:str,file:UploadFile=File(...),artifact_sha256:str=Form(...),
                            slide_id:str|None=Form(None),wps_version:str=Form(''),crop_json:str=Form('')):
    p=p_or_404(pid)
    try:
        report=_current_export_report(pid,p,'editable')
        result=import_wps_render(p,report,await file.read(50*1024*1024+1),file.filename,
                                artifact_sha256,slide_id,wps_version,json.loads(crop_json) if crop_json else None)
        return result
    except Exception as e:fail(e)

@app.get('/api/projects/{pid}/wps-render/{import_id}')
def read_wps_render(pid:str,import_id:str):
    p=p_or_404(pid)
    if not re.fullmatch(r'[0-9a-f]{16}',import_id):raise HTTPException(404,'非法验收编号')
    path=project_dir(pid)/'build'/'wps_acceptance'/import_id/'report.json'
    if not path.is_file():raise HTTPException(404,'WPS导入报告不存在')
    result=json.loads(path.read_text(encoding='utf-8'))
    try:
        export=_current_export_report(pid,p)
        result['current']=result['artifact_sha256']==export['artifact_sha256'] and result['project_revision']==p['revision']
    except ValueError:result['current']=False
    return result

@app.get('/wps-preview/{pid}/{import_id}/{filename}')
def wps_preview(pid:str,import_id:str,filename:str):
    p_or_404(pid)
    if not re.fullmatch(r'[0-9a-f]{16}',import_id) or not re.fullmatch(r'S\d{3}(?:_diff)?\.png',filename):
        raise HTTPException(404,'非法验收图片')
    path=project_dir(pid)/'build'/'wps_acceptance'/import_id/filename
    if not path.is_file():raise HTTPException(404,'验收图片不存在')
    return FileResponse(path,headers={'Cache-Control':'no-cache'})

@app.get('/build/{pid}/{filename}')
def build_media(pid:str,filename:str):
    if not re.fullmatch(r'(?:editable|raster)(?:_r\d+_[0-9a-f]{12})?_S\d{3}_preview\.png|presentation_(?:editable|raster)(?:_r\d+_[0-9a-f]{12})?\.(?:pptx|pdf)|report_(?:editable|raster)\.json|manual_image_prompts\.zip',filename):
        raise HTTPException(404,'文件类型不允许')
    try:
        f=project_dir(pid)/'build'/filename
        if filename in ('report_editable.json','report_raster.json') and not f.is_file():
            mode='raster' if 'raster' in filename else 'editable';current=p_or_404(pid)
            return JSONResponse({'project_id':pid,'mode':mode,'project_revision':current.get('revision'),
                'artifact_valid_current':False,'artifact_status':'尚未导出当前项目版本','previews':[]})
        if not f.is_file():raise FileNotFoundError('文件不存在')
        if filename in ('report_editable.json','report_raster.json'):
            mode='raster' if 'raster' in filename else 'editable'
            report=json.loads(f.read_text(encoding='utf-8'))
            try:
                _current_export_report(pid,p_or_404(pid),mode)
                report['artifact_valid_current']=True;report['artifact_status']='当前项目版本与PPTX哈希校验一致'
            except ValueError as e:
                report['artifact_valid_current']=False;report['artifact_status']=str(e)
            return JSONResponse(report,headers={'Cache-Control':'no-cache'})
        if filename.startswith(('presentation_editable','presentation_raster','editable_','raster_')):
            mode='raster' if 'raster' in filename else 'editable'
            try:report=_current_export_report(pid,p_or_404(pid),mode)
            except ValueError as e:raise HTTPException(409,str(e))
            if filename==f'presentation_{mode}.pptx':
                f=project_dir(pid)/'build'/report['filename']
            elif filename not in {report.get('filename'),report.get('pdf'),*(report.get('previews') or [])}:
                raise HTTPException(409,'此文件不是当前报告对应的PPTX/预览，请刷新页面并重新导出。')
            preview=next((item for item in report.get('preview_artifacts',[]) if item['filename']==filename),None)
            if preview and _sha256_file(f)!=preview['sha256']:
                raise HTTPException(409,'预览图片与本次导出哈希不一致，请重新导出。')
        return FileResponse(f,filename=(filename if filename.endswith(('.pptx','.pdf','.zip')) else None),headers={'Cache-Control':'no-cache'})
    except HTTPException:raise
    except Exception as e:fail(e)

@app.get('/api/projects/{pid}/debug')
def debug(pid:str):
    p=p_or_404(pid);folder=project_dir(pid)
    return {'project':{'id':p['id'],'revision':p['revision'],'locked':p['locked']},
            'status':status(p),'qa':audit(p),
            'reconstruction':[rc.reconstruction_coverage(p,s['id']) for s in p['slides'] if s.get('images',{}).get('design')],
            'images':[{'name':f.name,'bytes':f.stat().st_size} for f in sorted((folder/'images').iterdir()) if f.is_file()],
            'build':[f.name for f in sorted((folder/'build').iterdir()) if f.is_file()]}

@app.post('/api/projects/{pid}/slides/{sid}/visual-qa')
async def visual_qa(pid:str,sid:str,x:AIRequest):
    p=p_or_404(pid)
    try:
        slide=find_slide(p,sid);preview=_current_export_preview(pid,p,slide,'editable')
        return await review_visual(p,slide,rel_media(p,sid,'design'),preview,x.api_key,x.model)
    except Exception as e:fail(e)

@app.post('/api/ai/test')
async def ai_test(x:AIRequest):
    # Keep the explicit connection test cheap and bounded. A timed-out test is
    # not retried automatically, avoiding a second billable request; normal
    # model-backed workflows retain call_deepseek's default retry behavior.
    try:return {'result':await call_deepseek(
        [{'role':'user','content':'只返回JSON：{"ok":true,"message":"连接成功"}'}],
        x.api_key,x.model,max_tokens=80,request_timeout=45,network_retry_limit=0,
        qwen_thinking=False)}
    except Exception as e:fail(e)

@app.post('/api/batch/create')
async def batch_create(file:UploadFile=File(...)):
    data=await file.read(1000000)
    try:
        txt=data.decode('utf-8-sig');rows=list(csv.DictReader(io.StringIO(txt)))
        if not rows or len(rows)>100:raise ValueError('CSV 必须包含1–100条记录')
        for n,r in enumerate(rows,1):
            if not r.get('title') or not r.get('topic'):raise ValueError(f'CSV第{n}行缺少 title/topic')
            if not (1<=int(r.get('pages') or 8)<=60):raise ValueError(f'CSV第{n}行页数必须1–60')
        output=[]
        for n,r in enumerate(rows,1):
            obj=create_project(r['title'],r['topic'],int(r.get('pages') or 8),r.get('style') or 'editorial',r.get('audience') or '通用受众')
            output.append({'id':obj['id'],'title':obj['title'],'pages':len(obj['slides'])})
        return {'created':output,'note':'已创建全部项目；进入每个项目编辑、确认并锁定，再按编号上传图片。'}
    except Exception as e:fail(e)

@app.post('/api/batch/export')
def batch_export(request:BatchExport):
    if len(request.project_ids)>100:raise HTTPException(400,'超过100个项目')
    fn=DATA/f'batch_{int(time.time())}.zip';success=[];errors=[]
    with zipfile.ZipFile(fn,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for pid in dict.fromkeys(request.project_ids):
            try:
                result=render_deck(pid,request.mode)
                folder=project_dir(pid)/'build'
                z.write(folder/result['filename'],f'{pid}/{result["filename"]}')
                if result['pdf']:z.write(folder/result['pdf'],f'{pid}/{result["pdf"]}')
                success.append(pid)
            except Exception as e:errors.append({'id':pid,'error':str(e)[:250]})
        z.writestr('result.json',json.dumps({'success':success,'errors':errors},ensure_ascii=False,indent=2))
    return {'url':'/batch-download/'+fn.name,'success':success,'errors':errors}

@app.get('/batch-download/{filename}')
def batch_download(filename:str):
    if not re.fullmatch(r'batch_\d+\.zip',filename):raise HTTPException(404,'非法名称')
    f=DATA/filename
    if not f.is_file():raise HTTPException(404,'文件不存在')
    return FileResponse(f,filename=filename,media_type='application/zip')

web=ROOT/'web'
app.mount('/static',StaticFiles(directory=web),name='static')
@app.get('/')
def index():return FileResponse(web/'index.html')
