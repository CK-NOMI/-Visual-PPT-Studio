"""Local-first API. All user uploads stay on this machine; no cloud image API is invoked."""
from __future__ import annotations
import csv
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image

from .core import (ROOT, DATA, FILENAME_RE, STYLE_PRESETS, all_prompts,
                   build_prompt_zip, create_project, find_slide, list_projects,
                   project_dir, read_project, save_project, status, update_slide,
                   write_image, safe_json_write)
from .quality import audit, check_native_text, check_saved_layout
from .pptx_native import build as build_python_pptx
from .ai import (AIError, analyze_visual, analyze_structure, calibrate_text_layout,
                 configured_api_key, generate_image_prompts, generate_plan,
                 generate_style, review_image_pair, refine_clean_prompt, review_visual, call_deepseek)
from . import reconstruction as rc
from .equations import to_mathml
from .text_layout import layout_elements, layout_project
from .render_acceptance import wps_environment, layout_manifest, import_wps_render

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
class RepairAIRequest(AIRequest):
    slides:list[str]|None=None
class RefineCleanPromptRequest(AIRequest):
    current_prompt:str=Field(default='',max_length=10000)
    review:dict[str,Any]
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
            'model':os.environ.get('DEEPSEEK_MODEL','deepseek-flash'),'python':sys.version.split()[0],
            'node':check('node','--version'),
            'pptxgenjs':check('node',str(ROOT/'renderer'/'render.js'),'--health'),
            'default_pptx_backend':'python-pptx (无需Node依赖)',
            'libreoffice':check(office,'--version') if office else {'ok':False,'info':'未找到libreoffice或soffice'},
            'office_renderer_binary':office,'styles':STYLE_PRESETS,
            'offline_image_mode':True,'image_api_calls':False,
            'reconstruction':rc.available_ocr()}

@app.get('/api/projects')
def projects():return list_projects()

@app.post('/api/projects')
def create(x:ProjectCreate):
    try:return create_project(**x.model_dump())
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
    try:return update_slide(p,sid,changes)
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
        candidate,report=layout_elements(elements,p['design'],sid,s['elements'])
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
        # Supplement visual semantics with low-confidence local contour
        # candidates. These remain unchecked proposals until the user imports.
        remaining=max(0,80-len(result['objects']))
        result['objects'].extend(rc.detect_primitive_candidates(source,min(24,remaining)))
        result['geometry_candidates']=sum(o.get('source')=='opencv_geometry' for o in result['objects'])
        return result
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
        # Keep the exact structured diagnostics sent by the browser as a local
        # JSON artifact. DeepSeek still receives the JSON content in its prompt;
        # a local path alone is not readable by the remote API.
        feedback_dir=project_dir(pid)/'build'/'calibration_requests'
        feedback_path=feedback_dir/f'{sid}_latest.json'
        try:
            safe_json_write(feedback_path,{
                'schema_version':1,'project_id':pid,'slide_id':sid,
                'optimization_round':request.optimization_round,
                'transport':'HTTP application/json',
                'feedback':request.optimization_feedback,
            })
        except OSError:
            # Local archival is useful for inspection but must not block a valid
            # calibration request to DeepSeek.
            pass
        result=await calibrate_text_layout(p,slide,rel_media(p,sid,'design'),rel_media(p,sid,'clean'),preview,
            request.api_key,request.model,request.optimization_feedback,request.optimization_round,True)
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
            builder=build_python_pptx(p,out,mode)
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
        final_previews=[]
        for i,staged_name in enumerate(report['previews'],1):
            final_name=f'{mode}_r{p["revision"]}_{artifact_sha256[:12]}_S{i:03d}_preview.png'
            final_files.append((stage/staged_name,build/final_name));final_previews.append(final_name)
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
    try:return create(p_or_404(pid),body.get('slides'))
    except Exception as e:fail(e)

@app.post('/api/projects/{pid}/repair/smart-candidates')
async def smart_repair_candidate(pid:str,request:RepairAIRequest):
    """One bounded vision pass per selected slide, then evidence-gated local repair."""
    from .repair_pipeline import inspect as inspect_structure,create
    from .ai import propose_missing_text
    p=p_or_404(pid);selected=request.slides
    slides=[s for s in p['slides'] if not selected or s['id'] in selected]
    if len(slides)>12:raise HTTPException(400,'一次智能查漏最多处理12页；请分批执行，避免不必要的API消耗。')
    ai_issues={};ai_review={};calls=0;deterministic=inspect_structure(p,selected)
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
                calls+=1
                try:
                    proposal=await propose_missing_text(p,slide,rel_media(p,slide['id'],'design'),preview,
                        regions.get('regions',[]),[e for e in slide.get('elements',[]) if e.get('kind') in ('text','formula')],
                        request.api_key,request.model)
                    ai_issues[slide['id']]=proposal.get('missing',[])
                    ai_review[slide['id']]=proposal.get('review',[])
                except Exception as e:
                    ai_review[slide['id']]=['AI查漏未完成：'+str(e)[:240]]
        except Exception as e:
            # Deterministic recoveries remain useful if the render/API setup fails.
            for slide in slides:ai_review.setdefault(slide['id'],['AI查漏未完成：'+str(e)[:240]])
    else:
        for slide in slides:ai_review[slide['id']]=['未配置DeepSeek密钥；已运行确定性检查，可自动修复项仍会生成候选。']
    try:
        result=create(p,selected,ai_issues, calls,ai_review)
        result['diagnosis']['deterministic_issue_count']=deterministic['issue_count']
        result['diagnosis']['ai_proposal_count']=sum(len(v) for v in ai_issues.values())
        return result
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

@app.post('/api/projects/{pid}/export')
def export(pid:str,mode:str='editable'):
    try:return render_deck(pid,mode)
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
    try:return {'result':await call_deepseek([{'role':'user','content':'请返回JSON对象 {"ok":true,"message":"连接成功"}'}],x.api_key,x.model)}
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
