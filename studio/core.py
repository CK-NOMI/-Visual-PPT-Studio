"""Project persistence, strict slide identity, geometry and prompts. No external code copied."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
# Bootstrap local .env early enough that PPT_STUDIO_DATA works before importing the API.
_env_file=ROOT/'.env'
if _env_file.exists():
    for _line in _env_file.read_text(encoding='utf-8').splitlines():
        if '=' in _line and not _line.lstrip().startswith('#'):
            _k,_v=_line.split('=',1)
            if _k.strip():os.environ.setdefault(_k.strip(),_v.strip().strip('\"').strip("'"))
DATA = Path(os.getenv('PPT_STUDIO_DATA', str(ROOT / 'workspace'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
ID_RE = re.compile(r'^S\d{3}$')
FILENAME_RE = re.compile(r'^(S\d{3})_(design|clean|asset_\d{2})\.(png|jpe?g|webp)$', re.I)
PROJECT_RE = re.compile(r'^[0-9a-f]{12}$')
ALLOWED_IMG = {'PNG','JPEG','WEBP'}
STYLE_PRESETS = {
    'editorial': {'name':'现代编辑','bg':'F7F8FC','ink':'17243A','accent':'3766CF','muted':'66758E','font':'Microsoft YaHei'},
    'dark': {'name':'未来科技','bg':'0B1430','ink':'F5F8FF','accent':'7DA7FF','muted':'C1CCE1','font':'Microsoft YaHei'},
    'warm': {'name':'温暖商业','bg':'FAF7F0','ink':'2C2925','accent':'D1784F','muted':'776D60','font':'Microsoft YaHei'},
    'mono': {'name':'克制极简','bg':'FFFFFF','ink':'171717','accent':'525252','muted':'777777','font':'Microsoft YaHei'},
}


def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def project_dir(pid: str)->Path:
    if not PROJECT_RE.fullmatch(pid): raise ValueError('非法项目编号')
    p = DATA / pid
    if not p.is_dir(): raise FileNotFoundError(f'项目不存在: {pid}')
    return p

def safe_json_write(path: Path, obj: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, t = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(t, path)
    finally:
        if os.path.exists(t): os.unlink(t)

def read_project(pid: str)->dict:
    with (project_dir(pid)/'project.json').open('r', encoding='utf-8') as f: return json.load(f)

def save_project(p:dict):
    p['updated_at']=now()
    p['revision']=p.get('revision',0)+1
    safe_json_write(project_dir(p['id'])/'project.json',p)


def element(eid:str, kind:str, box:list, text='', **props):
    return {'id':eid,'kind':kind,'box':box,'text':text,'style':props}

def initial_elements(index:int,title:str,body:str,style:dict)->list[dict]:
    is_cover=index==1
    return [
        element('title','text',[0.07,0.15 if is_cover else 0.10,0.83,0.16],title,
                size=38 if is_cover else 28,bold=True,color=style['ink'],font=style['font']),
        element('body','text',[0.08,0.46 if is_cover else 0.35,0.73,0.35],body,
                size=19 if is_cover else 17,color=style['ink'],font=style['font']),
    ]

def create_project(title:str,topic:str,pages:int=6,style='editorial',audience='通用受众')->dict:
    if not 1<=pages<=60: raise ValueError('每套 PPT 页数必须在 1–60 之间')
    if style not in STYLE_PRESETS: style='editorial'
    pid=uuid.uuid4().hex[:12]
    base=DATA/pid
    (base/'images').mkdir(parents=True,exist_ok=True)
    (base/'build').mkdir(exist_ok=True)
    preset=copy.deepcopy(STYLE_PRESETS[style]); slides=[]
    for i in range(1,pages+1):
        sid=f'S{i:03d}'
        # Project metadata is context for the deck, not a slide-by-slide plan.
        # Start with blank copy so rough project details are never mistaken for
        # approved slide titles or body text.
        t=''
        b=''
        slides.append({'id':sid,'number':i,'title':t,'body':b,'intent':('封面主视觉' if i==1 else '信息清晰、版式丰富'),
                       'layout':'cover' if i==1 else 'content','background':preset['bg'],
                       'elements':initial_elements(i,t,b,preset),'images':{},'image_metadata':{},'masks':[],'notes':''})
    project={'id':pid,'title':title.strip() or '未命名演示文稿','topic':topic,'pages':pages,
             'style':style,'design':preset,'audience':audience,'locked':False,
             'slides':slides,'created_at':now(),'updated_at':now(),'revision':0,
             'notes':'','schema_version':1}
    safe_json_write(base/'project.json',project)
    return project

def list_projects():
    out=[]
    for d in DATA.iterdir():
        if (d.is_dir() and PROJECT_RE.fullmatch(d.name) and (d/'project.json').is_file()
                and not (d/'.visual_agent_preview').exists()):
            try:
                p=read_project(d.name)
                out.append({'id':p['id'],'title':p['title'],'pages':len(p['slides']),
                            'locked':p['locked'],'updated_at':p['updated_at'],
                            'images':sum(bool(s['images'].get('design')) for s in p['slides'])})
            except Exception: pass
    return sorted(out,key=lambda x:x['updated_at'],reverse=True)

def find_slide(p:dict,sid:str)->dict:
    for s in p['slides']:
        if s['id']==sid: return s
    raise ValueError('页码不存在: '+sid)

def validate_box(box):
    if not isinstance(box,list) or len(box)!=4: raise ValueError('box 必须是四个数字 [x,y,w,h]')
    if any(not isinstance(v,(int,float)) or isinstance(v,bool) for v in box): raise ValueError('box 必须为数值')
    if any(abs(v)>10 for v in box): raise ValueError('box 数值超出合理范围')
    if box[2]<=0 or box[3]<=0: raise ValueError('box 宽高必须大于 0')

def update_slide(p:dict,sid:str,changes:dict):
    from copy import deepcopy
    from .text_layout import layout_elements
    s=find_slide(p,sid)
    previous=deepcopy(s.get('elements',[]))
    for f in ['title','body','intent','notes','layout','background']:
        if f in changes:
            if not isinstance(changes[f],str): raise ValueError(f'{f} 必须是字符串')
            s[f]=changes[f][:12000]
    if 'elements' not in changes:
        for field in ('title','body'):
            if field not in changes:continue
            members=[e for e in s.get('elements',[]) if e.get('kind')=='text' and (e.get('id')==field or e.get('logical_id')==field)]
            if not members:continue
            base=deepcopy(members[0]);base.update(id=field,text=s[field],box=list(base.get('block_box') or base['box']))
            for key in ('text_layout','source_text','source_box','source_group','ocr_box','line_id','run_id','char_start','char_end','run_order','whitespace'):
                base.pop(key,None)
            first=s['elements'].index(members[0])
            s['elements']=[e for e in s['elements'] if e not in members]
            s['elements'].insert(first,base)
    if 'elements' in changes:
        els=changes['elements']
        if not isinstance(els,list) or len(els)>1000: raise ValueError('elements 必须是最多1000个对象的列表')
        seen=set()
        for e in els:
            if not isinstance(e,dict) or e.get('kind') not in ('text','formula','rect','ellipse','line','image'): raise ValueError('不支持的元素')
            if not isinstance(e.get('id'),str) or len(e['id'])>80 or e['id'] in seen: raise ValueError('元素ID必须唯一')
            seen.add(e['id']);validate_box(e.get('box'))
            if e['kind']=='formula':
                from .equations import to_mathml
                to_mathml(e.get('text',''))
            if e['kind']=='image':
                fn=e.get('asset','')
                if not isinstance(fn,str) or not re.fullmatch(r'S\d{3}_asset_\d{2}\.(png|jpg|jpeg|webp)',fn,re.I):
                    raise ValueError('仅允许使用当前项目已上传的编号素材')
                if not (project_dir(p['id'])/'images'/fn).exists(): raise ValueError('找不到素材：'+fn)
            if not isinstance(e.get('style',{}),dict): raise ValueError('元素style必须是对象')
        # The locked plan remains source of truth unless the UI explicitly
        # submits title/body along with the edited scene. Do not trust OCR alone.
        for field in ('title','body'):
            members=[e for e in els if e.get('kind') in ('text','formula') and (e.get('id')==field or e.get('logical_id')==field)]
            if len(members)==1:
                members[0]['text']=s[field]
            elif members:
                lines={}
                for e in members:lines.setdefault(e.get('line_id',field),[]).append(e)
                content='\n'.join(''.join(e['text'] for e in sorted(line,key=lambda e:e.get('run_order',0))) for line in lines.values())
                if content!=s[field]:raise ValueError(f'{field} 的单行片段与规划原文不一致，请同步标题/正文后保存')
        s['elements']=els
    if 'elements' in changes or 'title' in changes or 'body' in changes:
        s['elements'],s['text_structure']=layout_elements(s['elements'],p['design'],sid,previous)
        if len(s['elements'])>1000:raise ValueError('单行分框后超过1000个对象，请分批复核')
        p['text_layout_version']=2
    save_project(p); return s

def write_image(p:dict,sid:str,role:str,data:bytes,original:str)->dict:
    if not p['locked']: raise ValueError('先锁定逐页规划后才能上传图片')
    s=find_slide(p,sid)
    if role not in ('design','clean') and not re.fullmatch(r'asset_\d{2}',role):
        raise ValueError('角色必须是 design、clean 或 asset_01')
    if len(data)>25*1024*1024: raise ValueError('单张图片不能超过 25MB')
    from io import BytesIO
    try:
        img=Image.open(BytesIO(data));img.verify()
        img=Image.open(BytesIO(data));fmt=img.format;img=ImageOps.exif_transpose(img)
    except Exception as ex: raise ValueError('不是有效图片文件') from ex
    if fmt not in ALLOWED_IMG: raise ValueError('只允许 PNG / JPG / WEBP')
    ext='.png' if fmt=='PNG' else ('.webp' if fmt=='WEBP' else '.jpg')
    if img.width<100 or img.height<100 or img.width*img.height>45000000: raise ValueError('图片尺寸不符合要求')
    fn=f'{sid}_{role}{ext}'
    images=project_dir(p['id'])/'images'
    # Once a new design replaces the old one, the old accepted clean must not
    # silently survive as matching the new source (even though its file remains).
    if role=='design' and s['images'].get('design'):
        old_meta=s.get('image_metadata',{}).get('design',{})
        oldsha=old_meta.get('sha256')
        newsha=hashlib.sha256(data).hexdigest()
        # File-byte SHA is sufficient to conservatively mark the existing clean
        if oldsha and oldsha!=newsha and s['images'].get('clean'):
            s.setdefault('image_metadata',{}).setdefault('clean',{})['stale_source']=True
    # Ensure only one file per role regardless of extension
    for old in images.glob(f'{sid}_{role}.*'):
        old.unlink()
    path=images/fn
    # Normalize EXIF orientation so visual positioning matches presentation coordinates.
    if fmt=='JPEG':
        img.convert('RGB').save(path,format='JPEG',quality=93)
    elif fmt=='WEBP':
        img.save(path,format='WEBP',quality=94)
    else:
        img.save(path,format='PNG')
    s['images'][role]=fn
    s.setdefault('image_metadata',{})[role]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'width':img.width,'height':img.height,'original_filename':original,'uploaded_at':now()}
    if role=='clean':
        s['image_metadata'][role]['source_design_sha256']=s.get('image_metadata',{}).get('design',{}).get('sha256')
        s['image_metadata'][role]['stale_source']=False
    elif role=='design' and s['images'].get('clean'):
        # A manually uploaded clean image is valid only for the design image
        # that was current when the clean image was paired/uploaded. This also
        # catches clean-first batch uploads when the design arrives afterward.
        clean_meta=s.setdefault('image_metadata',{}).setdefault('clean',{})
        clean_meta['stale_source']=clean_meta.get('source_design_sha256')!=s['image_metadata'][role]['sha256']
    save_project(p)
    ratio=img.width/img.height
    return {'slide_id':sid,'role':role,'filename':fn,'original_filename':original,
            'width':img.width,'height':img.height,'ratio':round(ratio,4),
            'aspect_warning':abs(ratio-16/9)>0.06}

def prompt_for_slide(project:dict,slide:dict)->dict:
    sid=slide['id']; d=project['design']
    title=slide.get('title','').strip();body=slide.get('body','').strip()
    copy=(f'【必须逐字呈现的页面原文】标题：{title}\n正文：{body}\n'
          '按原文保留字词、数字、标点、英文术语和公式；只可为排版换行，不得改写、翻译、重复、遗漏或编造其他可读文字/数据。'
          if title or body else
          '本页尚无定稿文案。先按项目主题和本页用途拟定简短、适合单页的标题与说明文字，并直接呈现在完整设计稿中；'
          '不要出现“未填写”等占位词，不要编造精确数字、引文或研究结果。')
    common=(f'制作专业演示文稿第 {slide["number"]:03d} 页。画幅16:9，建议1920×1080或更高，'
            f'整套演示文稿的视觉风格：{project["style"]}，主色 #{d["accent"]}，'
            f'背景色 #{d["bg"]}，文字色 #{d["ink"]}。'
            f'项目主题：{project["topic"]}；本页用途：{slide["intent"]}。'
            f'{copy}采用清楚的标题/正文/标签层级、足够留白和可读字号；不要把密集内容挤成一整块，不要让文字与插画/装饰重叠。'
            '公式、上下标和关键术语必须逐字准确；如果无法可靠绘制公式，保留清晰空位，不要生成近似公式。'
            '避免伪文字、乱码、重复字符、随机字母、无依据的图表刻度、页眉页脚水印和Logo。'
            '这是含有完整可读文字的设计稿；“无文字”要求只用于另一个 clean 底图提示词。')
    design=(f'【{sid} 完整含字设计稿】{common} 生成完整、有艺术设计感的演示文稿单页设计图。'
            f'这是第 {slide["number"]} 页，不要输出多页拼图。将结果保存为 {sid}_design.png。')
    clean=(f'【{sid} clean无字底图】在 Image 编辑模式中直接编辑输入的 {sid}_design.png；禁止从零生成、重绘或重新排版。'
           '严格保持画布比例、背景、渐变/纹理、照片/插画、图标形状、面板、色块、边框、图表、箭头、连线、阴影和所有非文字对象的尺寸、颜色、位置与层级。'
           '只移除标题、正文、数字、图表标签、表格文字、页码、水印及文字阴影；图标和图形本身必须保留。'
           '在文字原位置自然修补背景，不改变周围非文字内容、不新增任何字符。'
           f'导出为 {sid}_clean.png。注意这是无文字、供后续叠加原生PPT元素的底图。')
    asset=(f'【{sid} 独立透明素材】如本页存在突出的大型装饰插画，请基于同一设计稿提取或重新生成该装饰素材，'
           '保持主题与轮廓，透明PNG背景，不要文字，不要棋盘格。'
           f'保存为 {sid}_asset_01.png。可选，不存在合适素材时跳过。')
    return {'slide_id':sid,'design_filename':f'{sid}_design.png','clean_filename':f'{sid}_clean.png',
            'asset_filename':f'{sid}_asset_01.png','design_prompt':design,'clean_prompt':clean,
            'asset_prompt':asset,'title':slide['title'],'body':slide['body']}

def all_prompts(project:dict): return [prompt_for_slide(project,s) for s in project['slides']]

def build_prompt_zip(project:dict)->Path:
    folder=project_dir(project['id'])/'build';folder.mkdir(exist_ok=True)
    out=folder/'manual_image_prompts.zip'
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        guide=('手动生图文件命名规范（禁止省略页码）\n\n'
               '每页：S001_design.png 完整视觉稿；S001_clean.png 无文字背景；可选 S001_asset_01.png 透明素材。\n'
               '按 S001、S002、S003 顺序逐页生成并在保存到本地时重命名。\n'
               '系统只接受已规划页码。先上传 design 再 clean，更利于对比。\n'
               '若没有 clean，混合可编辑导出不会悄悄使用含文字的 design 当背景。\n')
        z.writestr('00_命名规则.txt',guide)
        for x in all_prompts(project):
            t=('完整设计稿：\n'+x['design_prompt']+'\n\n无文字背景：\n'+x['clean_prompt']+
               '\n\n可选透明素材：\n'+x['asset_prompt']+'\n')
            z.writestr(f"prompts/{x['slide_id']}_提示词.txt",t)
        z.writestr('deck_plan.json',json.dumps(project,ensure_ascii=False,indent=2))
    return out

def status(p:dict)->dict:
    return {'locked':p['locked'],'slides':len(p['slides']),
            'design':sum('design' in s['images'] for s in p['slides']),
            'clean':sum('clean' in s['images'] for s in p['slides']),
            'assets':sum(sum(x.startswith('asset_') for x in s['images']) for s in p['slides']),
            'ready_hybrid':sum('clean' in s['images'] and not s.get('image_metadata',{}).get('clean',{}).get('stale_source') for s in p['slides']),
            'partial_raster_pages':sum(bool(s.get('image_metadata',{}).get('clean',{}).get('embedded_text_remaining')) for s in p['slides'])}
