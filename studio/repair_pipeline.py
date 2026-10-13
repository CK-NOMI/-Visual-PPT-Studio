"""Stage 03: evidence-based diagnosis, isolated candidates and explicit commit.

No model call and no image mutation. Unknown OCR remains a review issue.
"""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import hashlib,json,re,subprocess,sys,os,threading,uuid
from collections import defaultdict
import numpy as np
from PIL import Image
from . import core,reconstruction as rc
from .text_layout import layout_elements,diagnose_lines,repair_line
from .fonts import measure_text,resolve_font
from PIL import ImageDraw,ImageFont
from .quality import visible_glyph_box,intersects,check_saved_layout
from .pptx_native import build

_LOCK=threading.RLock()
_RUNNING=set()

def signature(p):return hashlib.sha256(json.dumps(p,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def plain(t):return rc._plain_text(str(t))
def overlap(a,b):return intersects(a,b)/max(1e-9,min(a[2]*a[3],b[2]*b[3]))
def session_dir(pid,token):
    if not re.fullmatch('[a-f0-9]{32}',token):raise ValueError('修复候选ID无效')
    f=core.project_dir(pid)/'build'/'repairs'/token
    if not f.is_dir():raise ValueError('找不到修复候选')
    return f

def clean_evidence(p,s,box,text,clean_ocr):
    dm=s.get('image_metadata',{}).get('design',{});cm=s.get('image_metadata',{}).get('clean',{})
    if not s['images'].get('clean') or cm.get('stale_source') or cm.get('source_design_sha256')!=dm.get('sha256'):
        return {'safe':False,'reason':'clean未配对'}
    d=rc.image_arr(rc.source_for(p,s['id']));c=rc.image_arr(rc.source_for(p,s['id'],'clean'))
    if c.shape!=d.shape:return {'safe':False,'reason':'配对图片尺寸不同，需复核'}
    x,y,x2,y2=rc.as_pixels(box,d.shape[1],d.shape[0]);a=d[y:y2,x:x2].astype(float);b=c[y:y2,x:x2].astype(float)
    if not a.size:return {'safe':False,'reason':'空源区域'}
    border=np.concatenate([a[0],a[-1],a[:,0],a[:,-1]]);bg=np.median(border,axis=0)
    ink=np.linalg.norm(a-bg,axis=2)>38;count=int(ink.sum())
    residual=float(np.mean(np.linalg.norm(b-bg,axis=2)[ink]>38)) if count else 1.
    change=float(np.mean(np.linalg.norm(a-b,axis=2)[ink]>24)) if count else 0.
    residual_ocr=[r for r in clean_ocr or [] if overlap(box,r['box'])>.4]
    safe=clean_ocr is not None and count>=12 and residual<.08 and change>.9 and not residual_ocr
    return {'safe':bool(safe),'clean_ocr_available':clean_ocr is not None,'source_ink_pixels':count,
            'remaining_ink_ratio':round(residual,4),'removed_ink_ratio':round(change,4),'residual_ocr':residual_ocr,
            'meaning':'保守像素证据；复杂背景或残字不自动补建'}

def inspect(p,slides=None):
    pages=[]
    for s in p['slides']:
        if slides and s['id'] not in slides:continue
        issues=[];regions=rc.get_regions(p,s['id']) if s['images'].get('design') else None
        native=[e for e in s['elements'] if e.get('kind') in ('text','formula') and str(e.get('text','')).strip()]
        clean_ocr=None
        if s['images'].get('clean') and rc.available_ocr().get('rapidocr'):
            clean_ocr=rc._rapidocr(rc.image_arr(rc.source_for(p,s['id'],'clean')))
        def issue(kind,text,box,matches,confidence,evidence,action='review',**extra):
            item={'issue_id':hashlib.sha256(json.dumps([s['id'],kind,text,box],ensure_ascii=False).encode()).hexdigest()[:20],
                'issue_type':kind,'slide_id':s['id'],'text':text,'source_region':box,'matched_elements':matches,
                'confidence':confidence,'evidence':evidence,'suggested_action':action,**extra}
            issues.append(item)
        if not regions or not regions.get('source_valid'):
            issue('uncertain_ocr','缺少与当前Design哈希匹配的OCR清单',None,[],0,{'source_valid':False})
        else:
            groups=defaultdict(list)
            for r in regions['regions']:groups[r.get('line_id') or r['id']].append(r)
            for rows in groups.values():
                rows.sort(key=lambda r:r.get('run_order',0));r=rows[0]
                text=''.join(x['text'] for x in rows);box=r.get('ocr_box') or r.get('source_box') or r['box']
                near=[e for e in native if overlap(box,e.get('ocr_box') or e.get('source_box') or visible_glyph_box(e))>.35]
                joined=''.join(e['text'] for e in sorted(near,key=lambda e:e.get('run_order',e['box'][0]*1000)))
                covered=plain(text) in plain(joined) or any(plain(text) in plain(e['text']) for e in near)
                if covered:continue
                elsewhere=[e['id'] for e in native if plain(e['text'])==plain(text)]
                if elsewhere:
                    issue('text_displaced_or_hidden',text,box,elsewhere,.9,{'ocr_rows':[x['id'] for x in rows]},'geometry_review');continue
                partial=[e['id'] for e in near if plain(e['text']) in plain(text) or plain(text) in plain(e['text'])]
                if partial:
                    issue('uncertain_ocr',text,box,partial,.5,{'reason':'部分匹配，不能直接补建完整行'});continue
                trusted=plain(text)==plain(s['title']) or (len(plain(text))>=8 and plain(text) in plain(s.get('body','')))
                conf=min(float(x.get('confidence',0)) for x in rows)
                ce=clean_evidence(p,s,box,text,clean_ocr)
                certain=trusted and conf>=.97 and len(plain(text))>=4 and ce['safe'] and not any(x.get('ocr_conflicts') for x in rows)
                kind='missing_native_object' if trusted and conf>=.97 else 'uncertain_ocr'
                if not ce['safe'] and kind=='missing_native_object':kind='background_residual_risk'
                issue(kind,text,box,[],conf,{'design_source_sha256':regions['source_sha256'],'plan_text_match':trusted,
                    'ocr_rows':[x['id'] for x in rows],'clean':ce,'native_object_absent':True,
                    'browser_visibility':'pending validation'},'create_native_text' if certain else 'review',source_rows=rows)
        for index,e in enumerate(native):
            for other in native[index+1:]:
                if plain(e['text'])==plain(other['text']) and overlap(visible_glyph_box(e),visible_glyph_box(other))>.7:
                    issue('duplicate_native_object',e['text'],visible_glyph_box(e),[e['id'],other['id']],.9,
                          {'same_text':True,'glyph_overlap':True,'manual_locks':[e.get('layout_locks'),other.get('layout_locks')]},'review_duplicate')
            warnings=[w for w in e.get('text_layout',{}).get('layout_warning',[]) if w.startswith('layout_conflict:')]
            if warnings:issue('text_overflow',e['text'],e.get('ocr_box') or e['box'],[e['id']],1,{'warnings':warnings,'locks':e.get('layout_locks',{})},'geometry_review')
            if '\n' in e['text']:issue('unexpected_line_break',e['text'],e['box'],[e['id']],1,{},'layout_engine')
        geometry=diagnose_lines(native,p.get('design'))
        for line in geometry:
            hard=[kind for kind in line['issues'] if kind!='visible_overlap']
            if 'visible_overlap' in line['issues']:
                # Separate source lines can be restored jointly; duplicates/ambiguous
                # source regions remain review only, never rewritten or removed.
                others=[x for x in geometry if x['line_id'] in line.get('overlap_lines',[])]
                if others and all(overlap(line['source_box'],x['source_box'])<.05 for x in others):hard.append('visible_overlap')
            members=[e for e in native if e['id'] in line['element_ids']]
            uncertain=any(e.get('ocr_conflicts') or e.get('review_required') for e in members)
            if hard:
                issue('line_geometry',line['text'],line['source_box'],line['element_ids'],1,
                      {'line':line,'constraints':hard},'repair_line' if not uncertain else 'review',line_id=line['line_id'],run_ids=line['run_ids'])
            # Existing fonts remain fixed. Compare only bold/regular silhouettes
            # in an isolated, high confidence single Latin line.
            if hard or len(members)!=1:continue
            e=members[0];st=e.get('style',{});locks=e.get('layout_locks',{})
            if (e.get('text_class')!='latin' or len(e['text'])<10 or float(e.get('confidence',0))<.97 or
                uncertain or locks.get('style') or locks.get('bold') or e.get('kind')!='text'):continue
            box=e.get('source_box') or e.get('ocr_box')
            if not box or not clean_evidence(p,s,box,e['text'],clean_ocr)['safe']:continue
            family=st.get('font') or p['design'].get('font','Arial');scores=[];tight=box
            for bold in [False,True]:
                _,rank,tight=source_font_candidates(p,s,box,e['text'],{**st,'bold':bold},[family])
                if rank:scores.append({'bold':bold,'distance':rank[0]['glyph_mask_distance']})
            if len(scores)!=2:continue
            best=min(scores,key=lambda x:x['distance']);current=next(x for x in scores if x['bold']==bool(st.get('bold')))
            if best['bold']==bool(st.get('bold')) or current['distance']-best['distance']<.025:continue
            issue('source_weight_alignment',e['text'],box,[e['id']],.98,
                  {'font_preserved':family,'silhouette_candidates':scores,'source_glyph_box':tight},'repair_source_style',line_id=line['line_id'],run_ids=line['run_ids'],desired_bold=best['bold'])
        pages.append({'slide_id':s['id'],'issues':issues,'native_objects':len(native),
                      'status':'requires_review' if issues else 'structural_checks_pass',
                      'browser_verified':False,'meaning':'OCR/项目证据检查，不等于浏览器视觉验收'})
    return {'project_id':p['id'],'revision':p['revision'],'pages':pages,'ai_calls':0,
            'issue_count':sum(len(page['issues']) for page in pages)}

def source_font_candidates(p,s,box,text,style,families=None):
    """Rank bounded Latin font choices by local glyph silhouettes, for NEW runs only.

    This generates a candidate, never claims exact font identity; browser validation
    remains authoritative. Existing text and manual locks are not touched.
    """
    if re.search('[\u3400-\u9fff]',text):return style.get('font'),[],list(box)
    image=Image.open(rc.source_for(p,s['id'])).convert('RGB');crop=image.crop(rc.as_pixels(box,image.width,image.height))
    arr=np.asarray(crop,dtype=float);bg=np.median(np.concatenate([arr[0],arr[-1],arr[:,0],arr[:,-1]]),axis=0)
    ink=(np.linalg.norm(arr-bg,axis=2)>60).astype('uint8')*255;reference=Image.fromarray(ink)
    bounds=reference.getbbox()
    if not bounds:return style.get('font'),[],list(box)
    reference=reference.crop(bounds);rank=[]
    for family in dict.fromkeys(families or [style.get('font','Arial'),'Times New Roman','Noto Serif','Arial','Noto Sans']):
        desc=resolve_font(family,'latin',bool(style.get('bold')))
        if desc['fallback']:continue
        font=ImageFont.truetype(desc['path'],64,index=desc['index']);bbox=font.getbbox(text)
        mask=Image.new('L',(bbox[2]-bbox[0]+4,bbox[3]-bbox[1]+4));ImageDraw.Draw(mask).text((2-bbox[0],2-bbox[1]),text,font=font,fill=255)
        actual=mask.crop(mask.getbbox()).resize(reference.size,Image.Resampling.LANCZOS)
        a=np.asarray(reference,dtype=float)/255;b=np.asarray(actual,dtype=float)/255
        distance=float(np.abs(a-b).mean())
        rank.append({'family':family,'glyph_mask_distance':round(distance,5),'file_sha256':measure_text(text,20,family,'latin',bool(style.get('bold')))['font']['file_sha256']})
    rank.sort(key=lambda r:r['glyph_mask_distance'])
    px,py,_,_=rc.as_pixels(box,image.width,image.height)
    tight=[(px+bounds[0])/image.width,(py+bounds[1])/image.height,(bounds[2]-bounds[0])/image.width,(bounds[3]-bounds[1])/image.height]
    return (rank[0]['family'] if rank else style.get('font')),rank,tight

def _ai_missing_issues(p,s,proposals,clean_ocr):
    """Turn vision suggestions into guarded repair issues, not direct model edits."""
    from .text_layout import split_runs
    regions=rc.get_regions(p,s['id']) or {};ocr=regions.get('regions',[])
    native=[e for e in s.get('elements',[]) if e.get('kind') in ('text','formula') and str(e.get('text','')).strip()]
    plan=[str(s.get('title','')),*str(s.get('body','')).splitlines()]
    def key(v):return ''.join(plain(v).split())
    out=[]
    for n,item in enumerate(proposals or []):
        if not isinstance(item,dict):continue
        text=item.get('text');box=item.get('box');conf=item.get('confidence')
        if not isinstance(text,str) or not text.strip() or '\n' in text or '\r' in text:continue
        text=text.strip()
        if not isinstance(box,list) or len(box)!=4 or not all(isinstance(v,(int,float)) and not isinstance(v,bool) for v in box):continue
        if any(v<0 or v>1 for v in box) or box[2]<=0 or box[3]<=0 or box[0]+box[2]>1.001 or box[1]+box[3]>1.001:continue
        if not isinstance(conf,(int,float)) or isinstance(conf,bool) or not 0<=conf<=1:conf=0
        if any(key(text) in key(e.get('text','')) for e in native):continue
        plan_match=any(key(text)==key(line) or (len(key(text))>=8 and key(text) in key(line)) for line in plan if line.strip())
        source_matches=[r for r in ocr if overlap(box,r.get('box',[0,0,0,0]))>.25 and key(text) in key(r.get('text',''))]
        source_match=any(float(r.get('confidence',0) or 0)>=.97 for r in source_matches)
        trusted=plan_match or source_match
        region_conf=max([float(r.get('confidence',0) or 0) for r in source_matches] or [0])
        ce=clean_evidence(p,s,box,text,clean_ocr)
        collisions=[e['id'] for e in native if overlap(box,e.get('box',[0,0,0,0]))>.12]
        try:pieces=split_runs(text)
        except ValueError:pieces=[]
        role=item.get('role') if item.get('role') in ('title','body','label') else 'body'
        # Require independent content evidence, clean-background evidence, a clear
        # visual region, and no nearby native object before any automatic candidate.
        # A high-confidence exact local OCR match is stronger than the model's
        # self-reported score. If OCR cannot corroborate it, require a strong
        # plan-grounded visual proposal instead.
        content_verified=(source_match and region_conf>=.97) or (plan_match and conf>=.94)
        certain=content_verified and ce.get('safe') and not collisions and bool(pieces)
        size={'title':30,'body':18,'label':14}[role]
        family=p.get('design',{}).get('font') or 'Arial'
        style={'font':family,'eastAsianFont':family,'size':size,'bold':role=='title',
               'color':p.get('design',{}).get('ink','17243A')}
        source_rows=source_matches or [{'id':f"ai_source_{s['id']}_{n}",'text':text,'box':list(box),
            'ocr_box':list(box),'source_box':list(box),'confidence':conf,'style':style,
            'line_id':f"ai_line_{s['id']}_{n}",'run_order':0}]
        out.append({'issue_id':hashlib.sha256(json.dumps([s['id'],'ai_missing',text,box],ensure_ascii=False).encode()).hexdigest()[:20],
            'issue_type':'ai_missing_native_object' if trusted else 'ai_suspected_missing_text',
            'slide_id':s['id'],'text':text,'source_region':box,'matched_elements':collisions,
            'confidence':max(region_conf,float(conf)) if source_match else float(conf),
            'evidence':{'origin':'DeepSeek视觉查漏候选','model_evidence':str(item.get('evidence',''))[:300],
                'plan_text_match':plan_match,'ocr_match_ids':[r.get('id') for r in source_matches],
                'clean':ce,'nearby_native_objects':collisions,'role':role,
                'decision':'候选由本地内容/底图/对象证据约束；仍须浏览器及PPTX验收'},
            'suggested_action':'create_native_text' if certain else 'review',
            'source_rows':source_rows,'ai_proposal':True})
    return out


def create(p,slides=None,ai_issues=None,ai_calls=0,ai_review=None):
    token=uuid.uuid4().hex;folder=core.project_dir(p['id'])/'build'/'repairs'/token;folder.mkdir(parents=True)
    core.safe_json_write(folder/'original_project.json',p)
    __import__('shutil').copytree(core.project_dir(p['id'])/'images',folder/'source_images')
    diagnosis=inspect(p,slides);candidate=deepcopy(p);actions=[]
    ai_issues=ai_issues or {};ai_review=ai_review or {}
    for page in diagnosis['pages']:
        s=next(s for s in p['slides'] if s['id']==page['slide_id'])
        clean_ocr=None
        if s['images'].get('clean') and rc.available_ocr().get('rapidocr'):
            clean_ocr=rc._rapidocr(rc.image_arr(rc.source_for(p,s['id'],'clean')))
        proposed=_ai_missing_issues(p,s,ai_issues.get(s['id'],[]),clean_ocr)
        deterministic_creates=[x for x in page['issues'] if x.get('suggested_action')=='create_native_text']
        proposed=[x for x in proposed if not any(plain(x['text'])==plain(d['text']) and
            overlap(x['source_region'],d['source_region'])>.35 for d in deterministic_creates)]
        page['issues'].extend(proposed)
        if ai_review.get(s['id']):
            for note in ai_review[s['id']][:12]:
                page['issues'].append({'issue_id':hashlib.sha256((s['id']+str(note)).encode()).hexdigest()[:20],
                    'issue_type':'ai_review_needed','slide_id':s['id'],'text':str(note)[:300],
                    'source_region':None,'matched_elements':[],'confidence':0,
                    'evidence':{'origin':'DeepSeek视觉查漏；模型不确定，未自动更改'},'suggested_action':'review'})
        page['status']='requires_review' if page['issues'] else 'structural_checks_pass'
    diagnosis['issue_count']=sum(len(page['issues']) for page in diagnosis['pages'])
    diagnosis['ai_calls']=int(ai_calls)
    for page in diagnosis['pages']:
        s=next(s for s in candidate['slides'] if s['id']==page['slide_id'])
        for problem in page['issues']:
            if problem['suggested_action']!='create_native_text':continue
            source=deepcopy(problem['source_rows'][0]);box=problem['source_region'];st=deepcopy(source.get('style',{}));text=problem['text']
            # Estimate a NEW object's size from original width in actual font.
            # Existing objects are never shrunk or modified by this recovery.
            initial=float(st.get('size',18));family,rank,glyph_box=source_font_candidates(p,s,box,text,st);family=family or p['design'].get('font','Arial');st['font']=family
            measured=measure_text(text,initial,family,'cjk' if re.search('[\u3400-\u9fff]',text) else 'latin',bool(st.get('bold')))
            advance=measured['advance_pt']
            st['size']=round(initial*glyph_box[2]*960/max(1,measured['ink_box_pt'][2]-measured['ink_box_pt'][0]),1)
            final_metrics=measure_text(text,st['size'],family,'cjk' if re.search('[\u3400-\u9fff]',text) else 'latin',bool(st.get('bold')))
            layout_box=[glyph_box[0]-final_metrics['ink_box_pt'][0]/960,glyph_box[1],glyph_box[2]+final_metrics['ink_box_pt'][0]/960,glyph_box[3]]
            if not 6<=st['size']<=96:problem['suggested_action']='review';continue
            element={'id':'recovered_'+problem['issue_id'],'kind':'text','text':text,'source_text':text,
                'box':list(layout_box),'ocr_box':list(layout_box),'source_box':list(box),'original_ocr_box':list(box),'layout_managed':True,'style':st,
                'recovery_evidence':{'issue_id':problem['issue_id'],'initial_size_pt':initial,'source_width_pt':box[2]*960,
                    'measured_advance_pt':advance,'font_candidates':rank,'source_glyph_box':glyph_box,'chosen_size_pt':st['size'],'method':'source width / actual font advance; new object only'}}
            proposed,_=layout_elements([element],p['design'],s['id'])
            if any(w.startswith('layout_conflict:') for e in proposed for w in e['text_layout']['layout_warning']):
                problem['suggested_action']='review';problem['repair_warning']='新对象布局冲突，未加入';continue
            s['elements'].extend(proposed);actions.append({'issue_id':problem['issue_id'],'slide_id':s['id'],
                'action':'create_native_text','new_ids':[e['id'] for e in proposed],'text':text,'parameters':proposed})
        for problem in page['issues']:
            if problem['suggested_action'] not in ('repair_line','repair_source_style'):continue
            members=[e for e in s['elements'] if e['id'] in problem['matched_elements']]
            old=deepcopy(members);source_box=None
            if problem['suggested_action']=='repair_source_style':
                e=members[0];st=e['style'];st['bold']=problem['desired_bold']
                tight=problem['evidence']['source_glyph_box']
                m=measure_text(e['text'],st['size'],st['font'],'latin',st['bold'])
                source_box=[tight[0]-m['ink_box_pt'][0]/960,tight[1],tight[2]+m['ink_box_pt'][0]/960,tight[3]]
            proposed,_=repair_line(members,p['design'],s['id'],
                normalize_size='line_font_size_mismatch' in problem['evidence'].get('constraints',[]),source_box=source_box)
            if {e['id'] for e in proposed}!={e['id'] for e in old} or any(x['issues'] for x in diagnose_lines(proposed,p['design'])) or any(w.startswith('layout_conflict:') for e in proposed for w in e['text_layout']['layout_warning']):
                for original in old:
                    target=next(e for e in s['elements'] if e['id']==original['id']);target.clear();target.update(original)
                problem['repair_warning']='锁定/布局约束未能满足，保持原对象';continue
            changed={e['id']:e for e in proposed};s['elements']=[changed.get(e['id'],e) for e in s['elements']]
            actions.append({'issue_id':problem['issue_id'],'slide_id':s['id'],'action':problem['suggested_action'],
                'new_ids':[e['id'] for e in proposed],'changed_ids':[e['id'] for e in proposed],
                'text':problem['text'],'before_parameters':old,'parameters':proposed})
    # Only declared actions may change existing objects; all others stay identical.
    core.safe_json_write(folder/'candidate_project.json',candidate);build(candidate,folder/'candidate.pptx')
    metadata={'token':token,'project_id':p['id'],'base_revision':p['revision'],'base_signature':signature(p),'candidate_signature':signature(candidate),
              'status':'candidate_created','diagnosis':diagnosis,'actions':actions,'ai_calls':int(ai_calls),
              'pptx_check':check_saved_layout(candidate,folder/'candidate.pptx'),'committed':False}
    core.safe_json_write(folder/'status.json',metadata)
    return metadata

def get(pid,token):return json.loads((session_dir(pid,token)/'status.json').read_text())
def update_status(folder,**fields):
    with _LOCK:
        data=json.loads((folder/'status.json').read_text());data.update(fields)
        data.setdefault('events',[]).append({'at':core.now(),'status':data.get('status'),'phase':data.get('phase'),'error':fields.get('error')})
        core.safe_json_write(folder/'status.json',data)

def validate(pid,token):
    with _LOCK:
        folder=session_dir(pid,token);state=get(pid,token)
        if state['status']=='validating' and (pid,token) in _RUNNING:return state
        if any(project_id==pid for project_id,_ in _RUNNING):raise ValueError('本项目已有候选正在验收，请等待当前任务结束')
        if state.get('committed'):raise ValueError('候选已提交')
        current=core.read_project(pid)
        if signature(current)!=state['base_signature']:raise ValueError('项目已变化，不能用旧候选验收当前项目，请重新生成候选')
        candidate=json.loads((folder/'candidate_project.json').read_text())
        if signature(candidate)!=state['candidate_signature']:raise ValueError('候选内容已变化，请重新生成')
        update_status(folder,status='validating',phase='before browser rendering',error=None,validation_complete=False)
        _RUNNING.add((pid,token))
        threading.Thread(target=_validation_worker,args=(pid,folder),daemon=True).start();return get(pid,token)

def _validation_worker(pid,folder):
    try:_validate(pid,folder)
    finally:
        with _LOCK:_RUNNING.discard((pid,folder.name))

def _validate(pid,folder):
    try:
        results={}
        for mode,file in [('before','original_project.json'),('after','candidate_project.json')]:
            if mode=='before' and (folder/'before/validation/report.json').is_file():
                from .browser_qa import DIAGNOSTICS_REVISION
                cached=json.loads((folder/'before/validation/report.json').read_text())
                if all(page.get('diagnostics_revision')==DIAGNOSTICS_REVISION for page in cached['pages']):results[mode]=cached;continue
            update_status(folder,phase=mode+'：浏览器截图、逐对象检查、PPTX结构验证')
            data=folder/mode/'input';target=data/pid;target.mkdir(parents=True,exist_ok=True)
            shutil=__import__('shutil');shutil.copytree(folder/'source_images',target/'images',dirs_exist_ok=True)
            (target/'build').mkdir(exist_ok=True);shutil.copyfile(folder/file,target/'project.json')
            env=os.environ.copy();env['PPT_STUDIO_DATA']=str(data)
            pages=[p['slide_id'] for p in get(pid,folder.name)['diagnosis']['pages']]
            output=folder/mode/'validation'
            if output.exists():shutil.move(str(output),str(folder/mode/('validation_previous_'+uuid.uuid4().hex[:8])))
            cmd=[sys.executable,str(core.ROOT/'scripts/qa_runner.py'),'--browser','--project',pid,
                '--slides',','.join(pages),'--skip-workflow','--output',str(output)]
            with (folder/(mode+'.log')).open('w') as log:
                run=subprocess.run(cmd,cwd=core.ROOT,env=env,stdout=log,stderr=log,timeout=600)
            if not (output/'report.json').is_file():raise RuntimeError(mode+'浏览器未生成报告：'+(folder/(mode+'.log')).read_text()[-1200:])
            report=json.loads((output/'report.json').read_text());results[mode]=report
            if report.get('fatal_error'):raise ValueError(mode+'浏览器验证失败：'+report['fatal_error'])
        evaluation=_finish_validation(folder,results,publish=False)
        state=json.loads((folder/'status.json').read_text())
        failed={t['issue_id'] for t in evaluation['comparison']['targeted'] if not t['pass']}
        if failed and len(failed)<len(state['actions']) and not state.get('rejected_actions'):
            candidate=json.loads((folder/'candidate_project.json').read_text());rejected=[];accepted=[]
            failed_by_id={item['issue_id']:item for item in evaluation['comparison']['targeted'] if not item['pass']}
            core.safe_json_write(folder/'initial_candidate_project.json',candidate)
            for action in state['actions']:
                if action['issue_id'] not in failed:accepted.append(action);continue
                result=failed_by_id.get(action['issue_id'],{});reason='浏览器局部验收未通过，已回滚。'
                if result.get('visible_native_runs',0)<len(action.get('new_ids',[])):
                    reason='候选文字未在浏览器中完整可见，已回滚。'
                elif result.get('source_region_mae_after') is not None and result.get('source_region_mae_before') is not None:
                    reason=f"局部视觉差异没有改善（MAE {result['source_region_mae_before']:.3f} → {result['source_region_mae_after']:.3f}），已回滚。"
                rejected.append({**action,'rollback_reason':reason,'validation_evidence':result})
                slide=next(s for s in candidate['slides'] if s['id']==action['slide_id'])
                original={e['id']:e for e in action.get('before_parameters',[])}
                slide['elements']=[original.get(e['id'],e) for e in slide['elements'] if e['id'] not in action['new_ids'] or e['id'] in original]
            __import__('shutil').move(str(folder/'after'),str(folder/'initial_trial'))
            core.safe_json_write(folder/'candidate_project.json',candidate);build(candidate,folder/'candidate.pptx')
            update_status(folder,actions=accepted,rejected_actions=rejected,candidate_signature=signature(candidate),status='validating',phase='回滚未改善的行并重新浏览器验收')
            _validate(pid,folder)
        else:_finish_validation(folder,results)
    except Exception as error:update_status(folder,status='validation_failed',phase='failed',error=str(error),validation_complete=True)

def _finish_validation(folder,results,publish=True):
    original=json.loads((folder/'original_project.json').read_text());candidate=json.loads((folder/'candidate_project.json').read_text())
    state=json.loads((folder/'status.json').read_text());changed_ids={id_ for a in state['actions'] for id_ in a.get('changed_ids',[])}
    changed_scope={(a['slide_id'],id_) for a in state['actions'] for id_ in a.get('changed_ids',[])}
    stable=all(e==next((x for x in cs['elements'] if x['id']==e['id']),None) for s,cs in zip(original['slides'],candidate['slides']) for e in s['elements'] if (s['id'],e['id']) not in changed_scope)
    pid=candidate['id'];after=results['after'];before=results['before'];actions=get(pid,folder.name)['actions']
    by_before={p['slide_id']:p for p in before['pages']};by_after={p['slide_id']:p for p in after['pages']}
    targeted=[]
    for action in actions:
        new=[o for o in by_after[action['slide_id']]['objects'] if o['id'] in action['new_ids']]
        slide=next(s for s in candidate['slides'] if s['id']==action['slide_id'])
        issue=next(i for page in get(pid,folder.name)['diagnosis']['pages'] for i in page['issues'] if i['issue_id']==action['issue_id'])
        source=Image.open(folder/'source_images'/slide['images']['design']).convert('RGB')
        x,y,x2,y2=rc.as_pixels(issue['source_region'],source.width,source.height)
        reference=source.crop((x,y,x2,y2))
        before_image=Image.open(folder/'before/validation'/f'{slide["id"]}_aligned.png').convert('RGB').crop((x,y,x2,y2))
        after_image=Image.open(folder/'after/validation'/f'{slide["id"]}_aligned.png').convert('RGB').crop((x,y,x2,y2))
        mae_before=float(np.abs(np.asarray(reference,dtype=float)-np.asarray(before_image,dtype=float)).mean())
        mae_after=float(np.abs(np.asarray(reference,dtype=float)-np.asarray(after_image,dtype=float)).mean())
        triptych=Image.new('RGB',(reference.width*3,reference.height),'white')
        for index,img in enumerate([reference,before_image,after_image]):triptych.paste(img,(index*reference.width,0))
        crop_name=action['issue_id']+'_before_after.png';triptych.save(folder/crop_name)
        visible=len(new)==len(action['new_ids']) and all(o.get('dom') and o['dom'].get('ink_box',{}).get('width',0)>0 and
            o['dom']['text']==o['final_text'] and not any(a['severity'] in ('error','visual_error') or a['type']=='image_missing_text' for a in o['anomalies']) for o in new)
        remaining_weight=any(a['type']=='image_text_weight' and a['severity']=='review' for o in new for a in o['anomalies'])
        resolved=(action['action']!='repair_source_style' or not remaining_weight)
        targeted.append({'outcome':'resolved' if resolved else 'improved_requires_review','issue_id':action['issue_id'],'visible_native_runs':len(new),'pass':visible and (mae_after<mae_before if action['action']!='repair_line' else not any(x['issues'] for x in diagnose_lines([e for e in slide['elements'] if e['id'] in action['new_ids']],candidate['design'])) and mae_after<=mae_before+1),
            'source_region_mae_before':round(mae_before,3),'source_region_mae_after':round(mae_after,3),'crop':crop_name,
            'meaning':'原稿 / 修复前 / 修复后；局部像素改善与DOM可见性同时检查，字体风格差异仍可待复核'})
    no_new_errors=all(p['failed_objects']<=by_before[p['slide_id']]['failed_objects'] and
                      p['visual_error_objects']<=by_before[p['slide_id']]['visual_error_objects'] for p in after['pages'])
    locks_preserved=True
    for old_slide,new_slide in zip(original['slides'],candidate['slides']):
        for old in old_slide['elements']:
            new=next((e for e in new_slide['elements'] if e['id']==old['id']),None)
            if new is None:locks_preserved=False;continue
            locks=old.get('layout_locks',{})
            if new.get('layout_locks',{})!=locks:locks_preserved=False
            for key,value in locks.items():
                if not value:continue
                if key in ('x','y','w','h') and new['box'][('x','y','w','h').index(key)]!=old['box'][('x','y','w','h').index(key)]:locks_preserved=False
                elif key=='style' and new.get('style')!=old.get('style'):locks_preserved=False
                elif key in old.get('style',{}) and new.get('style',{}).get(key)!=old['style'][key]:locks_preserved=False
    accepted=bool(actions) and all(x['pass'] for x in targeted) and stable and locks_preserved and no_new_errors and after.get('layout_contract_pass') and after.get('pptx_pass') and after.get('workflow_pass')
    comparison={'targeted':targeted,'locks_preserved':locks_preserved,'untargeted_elements_unchanged':stable,'changed_scope':sorted(changed_scope),'existing_elements_unchanged':stable and not changed_ids,'declared_changed_ids':sorted(changed_ids),'improved_targeted_issues':sum(t['pass'] for t in targeted),'resolved_targeted_issues':sum(t['pass'] and t['outcome']=='resolved' for t in targeted),'remaining_targeted_issues':sum(not t['pass'] for t in targeted),'no_new_hard_errors':no_new_errors,
                'pptx_pass':after.get('pptx_pass'),'browser_pass':after.get('layout_contract_pass'),
                'remaining_visual_status':after.get('visual_status'),'meaning':'定向修复验证，不宣称整页视觉全部通过'}
    if publish:
        write_diagnostic_summary(folder,results,comparison)
        update_status(folder,status='validated_pending_commit' if accepted else 'checked_current_project' if not actions and after.get('layout_contract_pass') and after.get('pptx_pass') else 'requires_review',phase='done',comparison=comparison,error=None,validation_complete=True)
    else:update_status(folder,phase='比较候选；可能回滚并重新验收',trial_comparison=comparison,validation_complete=False)
    return {'accepted':accepted,'comparison':comparison}

def write_diagnostic_summary(folder,results,comparison):
    import html
    from collections import Counter
    state=json.loads((folder/'status.json').read_text());pages=[]
    for page in results['after']['pages']:
        rows=Counter((a['type'],a['severity'],a.get('classification','unverified')) for o in page['objects'] for a in o['anomalies'])
        pages.append({'slide_id':page['slide_id'],'hard_objects':page['failed_objects'],'review_objects':page['review_objects'],
            'review_lines':len({i['line_id'] for i in page.get('line_issues',[]) if i['severity'] in ('review','visual_error')}),
            'categories':[{'type':t,'severity':severity,'classification':kind,'run_evidence_count':count} for (t,severity,kind),count in rows.items()],
            'line_summary':page.get('issue_summary'),'unmapped_evidence':page.get('unmapped_image_evidence')})
    summary={'project_id':state['project_id'],'base_revision':state['base_revision'],'candidate_signature':state['candidate_signature'],
        'comparison':comparison,'pages':pages,'actions':state['actions'],'rejected_actions':state.get('rejected_actions',[]),
        'meaning':'run证据、涉及对象、聚合行数分开计数；info保留证据，不计为故障；不确定OCR不改写'}
    core.safe_json_write(folder/'diagnostic_summary.json',summary)
    h=html.escape;body=['<!doctype html><meta charset="utf-8"><title>行级诊断与定向修复</title><style>body{font-family:system-ui;margin:32px}td,th{border:1px solid #ddd;padding:8px}table{border-collapse:collapse}pre{white-space:pre-wrap}</style><h1>行级诊断与定向修复</h1><p><a href="before/validation/report.html">修复前浏览器报告</a> · <a href="after/validation/report.html">修复后浏览器报告</a> · <a href="diagnostic_summary.json">完整JSON及参数变化</a></p>']
    body.append('<p>已验证改善 '+str(comparison['improved_targeted_issues'])+' 项，已解决指定问题 '+str(comparison['resolved_targeted_issues'])+' 项；回滚 '+str(len(summary['rejected_actions']))+' 个未改善候选。整页仍待复核。</p>')
    for page in pages:
        body.append('<h2>'+h(page['slide_id'])+'：待复核对象 '+str(page['review_objects'])+'，涉及行 '+str(page['review_lines'])+'</h2><table><tr><th>类别</th><th>判定</th><th>run证据数量</th></tr>')
        for row in page['categories']:body.append('<tr><td>'+h(row['type'])+'</td><td>'+h(row['severity']+' · '+row['classification'])+'</td><td>'+str(row['run_evidence_count'])+'</td></tr>')
        body.append('</table>')
    for target in comparison['targeted']:body.append('<p>'+h(target['issue_id']+' · '+target['outcome'])+' · <a href="'+h(target['crop'])+'">原稿 / 修复前 / 修复后局部</a></p>')
    if summary['rejected_actions']:
        body.append('<h2>已回滚候选及原因</h2><ul>')
        for action in summary['rejected_actions']:
            body.append('<li>'+h(action.get('slide_id','')+' · '+action.get('text',''))+'：'+h(action.get('rollback_reason','浏览器验收未通过'))+'</li>')
        body.append('</ul>')
    (folder/'diagnostic_summary.html').write_text(''.join(body),encoding='utf-8')

def commit(pid,token):
    with _LOCK:
        folder=session_dir(pid,token);state=get(pid,token);current=core.read_project(pid)
        if state.get('committed'):return state
        if state['status']!='validated_pending_commit':raise ValueError('候选尚未通过定向浏览器/PPTX验收，不能提交')
        if current['revision']!=state['base_revision'] or signature(current)!=state['base_signature']:
            raise ValueError('项目已变化，旧候选不能覆盖，请重新检查')
        candidate=json.loads((folder/'candidate_project.json').read_text())
        if signature(candidate)!=state['candidate_signature']:raise ValueError('候选内容已变化，不能提交')
        core.safe_json_write(folder/'precommit_backup.json',current)
        core.save_project(candidate)
        update_status(folder,status='committed',committed=True,committed_revision=candidate['revision'],committed_signature=signature(candidate))
        return get(pid,token)
