"""Local, inspectable image→editable-slide experiments (no image-generation API).

Original implementation for Visual PPT Studio. Every intermediate is saved so a
human can review it. Optical text recognition is an optional dependency; detected
text is NOT assumed to be the trusted presentation manuscript.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from .core import find_slide, now, project_dir, save_project, safe_json_write

FIDELITY_THRESHOLD = 90

_RAPIDOCR_ENGINE = None
_MULTISCALE_OCR_CACHE: dict[tuple[str,bool,Any],list[dict]] = {}


def lab_dir(pid: str) -> Path:
    path = project_dir(pid) / 'build' / 'reconstruction'
    path.mkdir(parents=True, exist_ok=True)
    return path


def source_for(p: dict, sid: str, role='design') -> Path:
    s = find_slide(p, sid)
    if role not in ('design','clean'):
        raise ValueError('图像来源只允许 design 或 clean')
    name = s.get('images', {}).get(role)
    if not name:
        raise ValueError(f'请先上传 {sid}_{role}.png')
    source = project_dir(p['id']) / 'images' / name
    if not source.is_file():
        raise ValueError(f'{role} 图片不存在')
    if role=='clean':
        meta=s.get('image_metadata',{}).get('clean',{})
        design_sha=s.get('image_metadata',{}).get('design',{}).get('sha256')
        if meta.get('stale_source') or not design_sha or meta.get('source_design_sha256')!=design_sha:
            raise ValueError('clean 与当前 design 未有效配对，不能作为修复来源')
    return source


def image_arr(src: Path):
    with Image.open(src) as im:
        if im.width * im.height > 18_000_000:
            raise ValueError('设计图像素过大，单张限1800万像素')
        return np.asarray(Image.alpha_composite(Image.new('RGBA',im.size,'white'),im.convert('RGBA')).convert('RGB')).copy()


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def valid_box(b: list[float]) -> list[float]:
    if not isinstance(b, list) or len(b) != 4:
        raise ValueError('区域必须为 [x,y,w,h]')
    try:
        b = [float(x) for x in b]
    except (TypeError, ValueError):
        raise ValueError('坐标必须为数字') from None
    if not all(np.isfinite(b)) or min(b) < 0 or b[2] <= 0 or b[3] <= 0 or b[0]+b[2] > 1.000001 or b[1]+b[3] > 1.000001:
        raise ValueError('区域越界，要求0–1归一化坐标，宽高大于0')
    return b


def as_pixels(box: list[float], w: int, h: int) -> tuple[int,int,int,int]:
    x,y,bw,bh = valid_box(box)
    return max(0,int(round(x*w))), max(0,int(round(y*h))), min(w,int(round((x+bw)*w))), min(h,int(round((y+bh)*h)))


def normalize_xyxy(x1,y1,x2,y2,w,h):
    x1 = max(0,min(w,round(float(x1)))); x2 = max(0,min(w,round(float(x2))))
    y1 = max(0,min(h,round(float(y1)))); y2 = max(0,min(h,round(float(y2))))
    return [round(x1/w,6),round(y1/h,6),round(max(0,x2-x1)/w,6),round(max(0,y2-y1)/h,6)]


def available_ocr() -> dict:
    import importlib.util
    o = {}
    o['rapidocr'] = importlib.util.find_spec('rapidocr_onnxruntime') is not None
    o['tesseract'] = bool(shutil.which('tesseract')) and importlib.util.find_spec('pytesseract') is not None
    o['simple_lama'] = importlib.util.find_spec('simple_lama_inpainting') is not None
    o['opencv'] = True
    return o


def _rapidocr(img: np.ndarray) -> list[dict]:
    global _RAPIDOCR_ENGINE
    from rapidocr_onnxruntime import RapidOCR
    if _RAPIDOCR_ENGINE is None:_RAPIDOCR_ENGINE=RapidOCR()
    output, _ = _RAPIDOCR_ENGINE(img)
    result=[]
    for item in output or []:
        if len(item)<3: continue
        polygon,text,confidence=item[0],str(item[1]),float(item[2])
        pts=np.asarray(polygon,dtype=np.float64)
        if pts.shape[0]<4: continue
        x1,y1=pts.min(axis=0);x2,y2=pts.max(axis=0)
        result.append({'text':text.strip(),'confidence':round(confidence,4),'box':normalize_xyxy(x1,y1,x2,y2,img.shape[1],img.shape[0])})
    return result


def _rapidocr_multiscale(img:np.ndarray,full_upscale:bool=True) -> list[dict]:
    """Read a slide at full scale and as overlapping enlarged tiles.

    A single whole-slide OCR pass systematically loses tiny labels and text near
    busy artwork. Overlapping quadrants preserve local context while increasing
    the effective text size; this is deliberately position-agnostic (not a
    footer/page-number special case). Coordinates from each crop are mapped
    back to the original slide before duplicate suppression.
    """
    # Include the backend callable identity so tests/custom OCR adapters cannot
    # accidentally reuse results produced by a previously installed backend.
    cache_key=(hashlib.sha256(img.tobytes()).hexdigest(),full_upscale,_rapidocr)
    cached=_MULTISCALE_OCR_CACHE.get(cache_key)
    if cached is not None:
        return [dict(item,box=list(item.get('box',[]))) for item in cached]
    results=_rapidocr(img)
    h,w=img.shape[:2]
    candidates=[]
    if full_upscale:
        enlarged=cv2.resize(img,(w*2,h*2),interpolation=cv2.INTER_CUBIC)
        candidates.extend(_rapidocr(enlarged))
    # 8% overlap prevents labels at quadrant boundaries from being clipped.
    # Each crop is enlarged 2x, giving four times the source pixel height.
    x_ranges=((0,.54),(.46,1.0));y_ranges=((0,.54),(.46,1.0))
    for y0f,y1f in y_ranges:
        for x0f,x1f in x_ranges:
            x0=int(w*x0f);x1=int(w*x1f);y0=int(h*y0f);y1=int(h*y1f)
            tile=img[y0:y1,x0:x1]
            if tile.size==0:continue
            tile_large=cv2.resize(tile,(tile.shape[1]*2,tile.shape[0]*2),interpolation=cv2.INTER_CUBIC)
            for item in _rapidocr(tile_large):
                x,y,bw,bh=item.get('box',[0,0,0,0])
                tw,th=tile.shape[1],tile.shape[0]
                item['box']=[(x0+x*tw)/w,(y0+y*th)/h,bw*tw/w,bh*th/h]
                candidates.append(item)
    for candidate in candidates:
        box=candidate.get('box',[0,0,0,0]);text=_plain_text(candidate.get('text',''))
        duplicate=None
        for index,current in enumerate(results):
            other=_plain_text(current.get('text',''));existing=current.get('box',[0,0,0,0])
            center=float(np.hypot((box[0]+box[2]/2)-(existing[0]+existing[2]/2),
                                  (box[1]+box[3]/2)-(existing[1]+existing[3]/2)))
            similarity=SequenceMatcher(None,text,other,autojunk=False).ratio() if text and other else 0
            # OCR may split/merge a short token differently between crops;
            # spatially close boxes with substantial overlap are duplicates too.
            overlap_x=max(0,min(box[0]+box[2],existing[0]+existing[2])-max(box[0],existing[0]))
            overlap_y=max(0,min(box[1]+box[3],existing[1]+existing[3])-max(box[1],existing[1]))
            overlap_x/=max(1e-6,min(box[2],existing[2]))
            overlap_y/=max(1e-6,min(box[3],existing[3]))
            # Full-slide and tiled OCR frequently return the same heading with
            # slightly different word endings and box heights. The former
            # 0.012 center limit missed these near-identical boxes, so both
            # complete and partial title detections were promoted as separate
            # layers. Require strong horizontal/vertical overlap as well as
            # textual evidence to avoid merging neighboring labels.
            same_text=(similarity>=.64 or
                       (min(len(text),len(other))>=4 and
                        (text in other or other in text)))
            same_geometry=center<=.06 and overlap_x>=.78 and overlap_y>=.68
            if same_geometry and (same_text or not text or not other):
                duplicate=index;break
        if duplicate is None:
            results.append(candidate)
        else:
            prior=results[duplicate]
            # Prefer the most complete spelling when two OCR passes disagree
            # on a clipped title suffix; confidence breaks equal-length ties.
            candidate_rank=(len(_plain_text(candidate.get('text',''))),
                            float(candidate.get('confidence') or 0))
            prior_rank=(len(_plain_text(prior.get('text',''))),
                        float(prior.get('confidence') or 0))
            if candidate_rank>prior_rank:results[duplicate]=candidate
    # Tile OCR may also produce word fragments inside a full OCR line. Remove
    # those here so the higher recall from enlargement does not create extra
    # editable objects or competing boxes.
    results=_dedupe_ocr_fragments(results)
    # Targets are identical across optimization rounds. Cache only a few recent
    # rasters so the thorough reference scan is not repeated on every trial.
    _MULTISCALE_OCR_CACHE[cache_key]=[dict(item,box=list(item.get('box',[]))) for item in results]
    while len(_MULTISCALE_OCR_CACHE)>8:
        _MULTISCALE_OCR_CACHE.pop(next(iter(_MULTISCALE_OCR_CACHE)))
    return results


def _tesseract(img: np.ndarray) -> list[dict]:
    import pytesseract
    from pytesseract import Output
    langs=pytesseract.get_languages(config='')
    language='chi_sim+eng' if 'chi_sim' in langs else ('eng' if 'eng' in langs else (langs[0] if langs else 'eng'))
    d=pytesseract.image_to_data(Image.fromarray(img),lang=language,config='--psm 11',output_type=Output.DICT, timeout=100)
    groups={}
    for i,word in enumerate(d['text']):
        word=(word or '').strip()
        if not word:continue
        try:confidence=float(d['conf'][i])
        except (ValueError, TypeError):confidence=-1
        if confidence<20:continue
        key=(d['block_num'][i],d['par_num'][i],d['line_num'][i])
        groups.setdefault(key,[]).append((word,confidence,d['left'][i],d['top'][i],d['width'][i],d['height'][i]))
    result=[]
    for row in groups.values():
        x1=min(z[2] for z in row);y1=min(z[3] for z in row)
        x2=max(z[2]+z[4] for z in row);y2=max(z[3]+z[5] for z in row)
        result.append({'text':' '.join(z[0] for z in row),
                       'confidence':round(sum(z[1] for z in row)/len(row)/100,4),
                       'box':normalize_xyxy(x1,y1,x2,y2,img.shape[1],img.shape[0])})
    return result


def _estimate_text_style(img: np.ndarray, box: list[float], text:str='', family:str|None=None) -> dict:
    """Estimate ink colour and point size from a tight OCR line box.

    This is a visual measurement, not a font-recognition claim. DeepSeek may
    refine family/weight later; the sampled color is a strong local starting
    point for the editable line object.
    """
    h,w=img.shape[:2]
    x1,y1,x2,y2=as_pixels(box,w,h)
    if x2<=x1 or y2<=y1:return {}
    pad=max(2,int((y2-y1)*.55))
    ax1=max(0,x1-pad);ay1=max(0,y1-pad);ax2=min(w,x2+pad);ay2=min(h,y2+pad)
    crop=img[ay1:ay2,ax1:ax2]
    core=img[y1:y2,x1:x2]
    if crop.size==0 or core.size==0:return {}
    border=np.concatenate([crop[0],crop[-1],crop[:,0],crop[:,-1]],axis=0)
    background=np.median(border.astype(np.float32),axis=0)
    pixels=core.reshape(-1,3).astype(np.float32)
    distance=np.linalg.norm(pixels-background,axis=1)
    ink=pixels[distance>=38]
    size=round(max(6,min(96,(y2-y1)*.504)),1)
    evidence=None
    if text:
        from .fonts import measure_text,default_cjk_font
        metrics=measure_text(text,24,family or default_cjk_font(),'cjk' if re.search(r'[\u3400-\u9fff]',text) else 'latin')
        ys=np.where((distance>=38).reshape(core.shape[:2]))[0]
        ink_height=float(ys.max()-ys.min()+1) if len(ys) else float(y2-y1)
        measured_height=metrics['ink_box_pt'][3]-metrics['ink_box_pt'][1]
        if measured_height>0:
            # Normalize image pixels to the actual 540pt slide, then match the
            # selected face's visible glyph height. Independent of resolution.
            size=round(max(6,min(96,24*(ink_height*540/h)/measured_height)),1)
            evidence={'method':'source ink height / actual font glyph height','source_ink_height_px':ink_height,
                      'image_height_px':h,'measured_font':metrics['font'],'estimate_pt':size}
    if len(ink)<max(2,int(len(pixels)*.015)):
        return {'size':size,**({'font_size_evidence':evidence} if evidence else {})}
    # Median suppresses anti-aliased edge colors while retaining colored ink.
    rgb=np.clip(np.median(ink,axis=0),0,255).astype(np.uint8)
    color=''.join(f'{int(c):02X}' for c in rgb)
    return {'size':size,'color':color,**({'font_size_evidence':evidence} if evidence else {})}


def _estimate_fidelity_ink_color(img: np.ndarray, box: list[float]) -> str | None:
    """Estimate foreground ink from the strongest glyph pixels in a tight OCR box.

    A median over every anti-aliased ink pixel biases small rendered text heavily
    toward its background (for example, #333333 can be measured as #797979).
    Sample the highest-contrast fifth instead; this tracks the actual stroke
    color while remaining an image measurement rather than trusting PPT styles.
    """
    h,w=img.shape[:2]
    x1,y1,x2,y2=as_pixels(box,w,h)
    if x2<=x1 or y2<=y1:return None
    pad=max(2,int((y2-y1)*.55))
    ax1=max(0,x1-pad);ay1=max(0,y1-pad);ax2=min(w,x2+pad);ay2=min(h,y2+pad)
    crop=img[ay1:ay2,ax1:ax2]
    core=img[y1:y2,x1:x2]
    if crop.size==0 or core.size==0:return None
    border=np.concatenate([crop[0],crop[-1],crop[:,0],crop[:,-1]],axis=0)
    background=np.median(border.astype(np.float32),axis=0)
    pixels=core.reshape(-1,3).astype(np.float32)
    distance=np.linalg.norm(pixels-background,axis=1)
    ink_mask=distance>=38
    ink=pixels[ink_mask]
    ink_distance=distance[ink_mask]
    if len(ink)<2:return None
    cutoff=float(np.percentile(ink_distance,80))
    solid=ink[ink_distance>=cutoff]
    if len(solid)<2:solid=ink
    rgb=np.clip(np.median(solid,axis=0),0,255).astype(np.uint8)
    return ''.join(f'{int(c):02X}' for c in rgb)


def _paired_text_color(design: np.ndarray, clean: np.ndarray, box: list[float]) -> str | None:
    """Recover foreground ink by comparing a design crop with its paired clean crop."""
    if design.shape[:2]!=clean.shape[:2]:
        clean=cv2.resize(clean,(design.shape[1],design.shape[0]),interpolation=cv2.INTER_AREA)
    h,w=design.shape[:2];x1,y1,x2,y2=as_pixels(box,w,h)
    if x2<=x1 or y2<=y1:return None
    original=design[y1:y2,x1:x2].astype(np.float32)
    background=clean[y1:y2,x1:x2].astype(np.float32)
    delta=np.linalg.norm(original-background,axis=2)
    if delta.size<4 or float(delta.max())<24:return None
    cutoff=max(24,float(np.percentile(delta,75)))
    mask=delta>=cutoff
    if int(mask.sum())<max(3,int(delta.size*.002)):return None
    foreground=np.median(original[mask],axis=0)
    return ''.join(f'{int(c):02X}' for c in np.clip(foreground,0,255).astype(np.uint8))


def _contrast_ratio(foreground: str, background: np.ndarray) -> float:
    def luminance(rgb):
        values=[]
        for value in rgb:
            channel=float(value)/255
            values.append(channel/12.92 if channel<=.04045 else ((channel+.055)/1.055)**2.4)
        return .2126*values[0]+.7152*values[1]+.0722*values[2]
    try:ink=[int(foreground[i:i+2],16) for i in (0,2,4)]
    except (ValueError,TypeError):return 21.0
    a,b=sorted((luminance(ink),luminance(background)))
    return (b+.05)/(a+.05)


def _visible_ink_color(estimated: str | None, background: np.ndarray) -> str:
    """Keep source ink when readable; otherwise choose the strongest neutral contrast."""
    if estimated and _contrast_ratio(estimated,background)>=3.0:return estimated
    candidates=('FFFFFF','17243A')
    return max(candidates,key=lambda value:_contrast_ratio(value,background))


def _contained_ocr_fragment(box: list[float], parent: list[float], threshold: float=.78) -> bool:
    """True when one OCR box is mostly a fragment inside a larger text line."""
    ax1,ay1,aw,ah=map(float,box);bx1,by1,bw,bh=map(float,parent)
    ix=max(0.0,min(ax1+aw,bx1+bw)-max(ax1,bx1))
    iy=max(0.0,min(ay1+ah,by1+bh)-max(ay1,by1))
    area=max(aw*ah,1e-9);parent_area=max(bw*bh,1e-9)
    return ix*iy/area>=threshold and area<parent_area*.78


def _dedupe_ocr_fragments(regions: list[dict]) -> list[dict]:
    """Discard smaller OCR fragments nested in a full-line observation.

    Overlapping-tile OCR can report a full heading plus isolated words or a
    punctuation mark from that same heading. Counting both as separate lines
    incorrectly lowers text precision and geometry despite no visual defect.
    """
    ordered=sorted(regions,key=lambda r:(-float(r['box'][2])*float(r['box'][3]),
                                         -float(r.get('confidence') or 0)))
    kept=[]
    for region in ordered:
        if any(_contained_ocr_fragment(region['box'],parent['box']) and
               _plain_text(region.get('text','')) in _plain_text(parent.get('text',''))
               for parent in kept):
            continue
        kept.append(region)
    return kept


def mark_ocr_conflicts(regions: list[dict]) -> list[dict]:
    """Keep contradictory observations for review, without merging repeated labels.

    Apply before script splitting: sibling runs share a source box by design.
    This is a safety gate, never an automatic spelling decision.
    """
    from copy import deepcopy
    result=deepcopy(regions)
    for index,left in enumerate(result):
        a=left['box'];ta=_plain_text(left.get('text',''))
        for right in result[index+1:]:
            b=right['box'];tb=_plain_text(right.get('text',''))
            if not ta or not tb or ta==tb:continue
            ix=max(0,min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0]))
            iy=max(0,min(a[1]+a[3],b[1]+b[3])-max(a[1],b[1]))
            if ix/max(1e-9,min(a[2],b[2]))<.78 or iy/max(1e-9,min(a[3],b[3]))<.68:continue
            for region,other in ((left,right),(right,left)):
                region['review_required']=True
                region.setdefault('ocr_conflicts',[]).append({'text':other['text'],'box':list(other['box']),
                    'confidence':other.get('confidence'),'reason':'same source region has contradictory OCR text'})
    return result


_FORMULA_OPERAND=r'(?:[A-Za-z]{1,2}|\d+(?:\.\d+)?)(?:\s*[_^]\s*(?:[A-Za-z]{1,2}|\d+))?'
_FORMULA_SPAN=re.compile(
    r'\\(?:frac|sum|int|sqrt|alpha|beta|gamma|theta|pi|sigma)\b(?:\s*\{[^{}]*\}){0,3}|'
    r'\\\([^\n]+?\\\)|\\\[[^\n]+?\\\]|\$[^$\n]+\$|'
    r'(?<![\w-])[A-Za-z]{1,2}\s*[_^]\s*(?:[A-Za-z]{1,2}|\d+)(?![\w-])|'
    r'(?<![\w-])'+_FORMULA_OPERAND+r'(?:\s*[=+\-*/×÷≤≥<>]\s*'+_FORMULA_OPERAND+r')+(?![\w-])'
)


def _text_class(char:str)->str|None:
    code=ord(char);category=unicodedata.category(char)
    if (0x3400<=code<=0x4DBF or 0x4E00<=code<=0x9FFF or 0xF900<=code<=0xFAFF or
        0x3040<=code<=0x30FF or 0xAC00<=code<=0xD7AF):return 'cjk'
    if char.isalpha() or char.isdigit():return 'latin'
    if char.isspace() or category.startswith('P'):return None
    if category.startswith('S') or char in '^_':return 'formula'
    return None


def _split_mixed_script_line(text:str,box:list[float])->list[dict]:
    from .text_layout import split_runs
    return [{**r,'box':list(box)} for r in split_runs(text)]


def _split_mixed_script_regions(regions:list[dict])->list[dict]:
    from .text_layout import split_regions
    return split_regions(regions)


def detect_text(p:dict,sid:str,backend='auto') -> dict:
    source=source_for(p,sid);im=image_arr(source);available=available_ocr()
    clean_image=None
    try:clean_image=image_arr(source_for(p,sid,'clean'))
    except (ValueError,FileNotFoundError):pass
    if backend=='auto':backend='rapidocr' if available['rapidocr'] else 'tesseract' if available['tesseract'] else 'none'
    if backend not in ('rapidocr','tesseract') or not available.get(backend):
        raise ValueError('未检测到可用OCR：建议安装可选依赖 python -m pip install -r requirements-reconstruction.txt；或安装 tesseract 与 pytesseract。')
    # Tiny footer labels (especially slide numbers) often fall below RapidOCR's
    # whole-slide detection threshold.  The multiscale pass enlarges the full
    # slide and overlapping tiles, then maps detections back to slide coords.
    detected=_rapidocr_multiscale(im,full_upscale=True) if backend=='rapidocr' else _tesseract(im)
    detected=[r for r in detected if r['text'] and r['box'][2]>.001 and r['box'][3]>.001]
    detected=_split_mixed_script_regions(mark_ocr_conflicts(detected))
    detected=sorted(detected,key=lambda r:(round(r['box'][1]/0.025),r['box'][0],r.get('run_order',0)))
    for i,r in enumerate(detected,1):
        r['id']=f'text_{i:03d}'
        r['role']='custom'
        r['style']=_estimate_text_style(im,r['ocr_box'],r.get('source_text',r['text']),p.get('design',{}).get('font'))
        if 'font_size_evidence' in r['style']:r['font_size_evidence']=r['style'].pop('font_size_evidence')
        if clean_image is not None:
            x1,y1,x2,y2=as_pixels(r['box'],im.shape[1],im.shape[0])
            if x2>x1 and y2>y1:
                local_background=np.median(clean_image[y1:y2,x1:x2].reshape(-1,3),axis=0)
                foreground=_paired_text_color(im,clean_image,r['box']) or r['style'].get('color')
                r['style']['color']=_visible_ink_color(foreground,local_background)
        r['editable']=False # never auto-promote OCR guesses on complex slides
    out={'slide_id':sid,'backend':backend,'source_sha256':sha_file(source),
         'image_size':[im.shape[1],im.shape[0]],'regions':detected,
         'notice':'OCR内容和字体仅为候选；必须人工检查。原始规划文案仍是可信来源。'}
    safe_json_write(lab_dir(p['id'])/f'{sid}_ocr.json',out)
    return out


def detect_primitive_candidates(source:Path,limit=24) -> list[dict]:
    """Find rough rectangular panels and long rules for human review.

    This deliberately reports low-confidence candidates. It does not claim
    semantic segmentation and does not auto-promote anything into the deck.
    """
    rgb=image_arr(source);h,w=rgb.shape[:2]
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    edges=cv2.Canny(cv2.GaussianBlur(gray,(3,3),0),45,135)
    contours,_=cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    rects=[]
    for contour in contours:
        area=cv2.contourArea(contour)/(w*h)
        if not .00025<=area<=.10:continue
        perimeter=cv2.arcLength(contour,True)
        approx=cv2.approxPolyDP(contour,.035*perimeter,True)
        if not 4<=len(approx)<=8 or not cv2.isContourConvex(approx):continue
        x,y,bw,bh=cv2.boundingRect(approx)
        if bw<max(30,w*.035) or bh<max(22,h*.026):continue
        ratio=bw/max(1,bh)
        if ratio>14 or ratio<.08:continue
        box=[round(x/w,6),round(y/h,6),round(bw/w,6),round(bh/h,6)]
        # Suppress nearly identical nested edge contours.
        duplicate=False
        for prior in rects:
            a=prior['box'];ix=max(0,min(box[0]+box[2],a[0]+a[2])-max(box[0],a[0]));iy=max(0,min(box[1]+box[3],a[1]+a[3])-max(box[1],a[1]))
            inter=ix*iy;union=box[2]*box[3]+a[2]*a[3]-inter
            if union and inter/union>.88:duplicate=True;break
        if not duplicate:
            rects.append({'id':'cv_rect','kind':'rect','box':box,'text':'','confidence':.42,
                          'role':'custom','style':{},'source':'opencv_geometry'})
    rects=sorted(rects,key=lambda r:r['box'][2]*r['box'][3],reverse=True)[:max(0,min(limit,18))]
    min_length=max(36,int(min(w,h)*.045))
    found=cv2.HoughLinesP(edges,1,np.pi/180,threshold=max(25,min_length//2),
                          minLineLength=min_length,maxLineGap=max(6,int(min(w,h)*.008)) )
    lines=[]
    for line in np.asarray(found).reshape(-1,4) if found is not None else []:
        x1,y1,x2,y2=map(int,line);dx=x2-x1;dy=y2-y1;length=float(np.hypot(dx,dy))
        if length<min_length or abs(dy)>max(3,abs(dx)*.045) and abs(dx)>max(3,abs(dy)*.045):continue
        box=normalize_xyxy(min(x1,x2),min(y1,y2),max(x1,x2)+1,max(y1,y2)+1,w,h)
        if box[2]<=0 or box[3]<=0:continue
        # Keep only long, nearly horizontal/vertical rules; short text strokes
        # are filtered by the minimum segment length.
        if min(box[2],box[3])>.045:continue
        lines.append({'id':'cv_line','kind':'line','box':box,'text':'','confidence':.30,
                      'role':'custom','style':{'strokeWidth':1},'source':'opencv_geometry'})
        if len(lines)>=max(0,min(limit,6)):break
    output=[]
    for i,item in enumerate(rects+lines,1):
        item['id']=f'geometry_{i:03d}';output.append(item)
    return output[:max(0,min(limit,40))]


def persist_regions(p:dict,sid:str,regions:list[dict]) -> dict:
    source=source_for(p,sid)
    if len(regions)>1000:raise ValueError('最多1000个文字区域；未截断，请分批复核')
    normalized=[]
    for i,r in enumerate(regions):
        if not isinstance(r,dict):raise ValueError('区域格式错误')
        txt=r.get('text','')
        if not isinstance(txt,str) or len(txt)>1600:raise ValueError('文字必须为最多1600字符的字符串')
        item={'id':str(r.get('id') or f'text_{i+1:03d}'),'text':txt,'box':valid_box(r.get('box')),
              'confidence':float(r.get('confidence') or 0),'role':r.get('role','custom'),
              'editable':bool(r.get('editable', False))}
        # Keep script-split provenance across the review/save/reload boundary.
        # Without it, confirmation can no longer reflow chunks as one line.
        for key in ('source_text','source_group','block_id','line_id','run_id','migration_warning','review_reason'):
            value=r.get(key)
            if isinstance(value,str) and value and len(value)<=1600:
                item[key]=value
        for key in ('char_start','char_end','run_order','whitespace','ocr_box','text_layout','layout_managed','layout_locks','kind','font_size_evidence','review_required','ocr_conflicts'):
            if key in r:item[key]=r[key]
        source_box=r.get('source_box')
        if isinstance(source_box,(list,tuple)) and len(source_box)==4:
            try:item['source_box']=valid_box(source_box)
            except (ValueError,TypeError):pass
        if r.get('text_class') in ('cjk','latin','formula'):
            item['text_class']=r['text_class']
        style=r.get('style') if isinstance(r.get('style'),dict) else {}
        safe_style={}
        if isinstance(style.get('size'),(int,float)) and not isinstance(style.get('size'),bool) and 6<=style['size']<=96:
            safe_style['size']=round(float(style['size']),1)
        if isinstance(style.get('color'),str) and re.fullmatch(r'#?[0-9A-Fa-f]{6}',style['color']):
            safe_style['color']=style['color'].lstrip('#').upper()
        if isinstance(style.get('bold'),bool):safe_style['bold']=style['bold']
        for key in ('font','eastAsianFont'):
            value=style.get(key)
            if isinstance(value,str) and len(value)<=80 and re.fullmatch(r'[\w .()\-]+',value):
                safe_style[key]=value
        for key in ('char_spacing_pt','line_spacing'):
            value=style.get(key)
            if isinstance(value,(int,float)) and not isinstance(value,bool) and abs(value)<=100:
                safe_style[key]=float(value)
        if safe_style:item['style']=safe_style
        if item['role'] not in ('custom','title','body','ignore'):raise ValueError('role应是custom/title/body/ignore')
        normalized.append(item)
    out={'slide_id':sid,'backend':'human_reviewed','source_sha256':sha_file(source),
         'image_size':list(Image.open(source).size),'regions':normalized,
         'reviewed_at':now()}
    safe_json_write(lab_dir(p['id'])/f'{sid}_ocr.json',out)
    return out


def get_regions(p:dict,sid:str) -> dict|None:
    f=lab_dir(p['id'])/f'{sid}_ocr.json'
    if not f.is_file():return None
    data=json.loads(f.read_text(encoding='utf-8'))
    current=sha_file(source_for(p,sid))
    data['source_valid']=data.get('source_sha256')==current
    return data


def make_mask(im:np.ndarray,regions:list[dict],mode='ink',padding=4,threshold=32) -> np.ndarray:
    if mode not in ('ink','rectangle'):raise ValueError('mode只能是ink或rectangle')
    if not 0<=padding<=24:raise ValueError('padding只允许0至24像素')
    h,w=im.shape[:2]
    mask=np.zeros((h,w),dtype=np.uint8)
    lab=cv2.cvtColor(im,cv2.COLOR_RGB2LAB)
    for r in regions:
        box=r['box'] if isinstance(r,dict) else r
        x1,y1,x2,y2=as_pixels(box,w,h)
        if x2<=x1 or y2<=y1:continue
        x1=max(0,x1-2);y1=max(0,y1-2);x2=min(w,x2+2);y2=min(h,y2+2)
        if mode=='rectangle':mask[y1:y2,x1:x2]=255;continue
        # Estimate local background colour from perimeter, then mark ink with
        # sufficient LAB distance. Thick text might require rectangle fallback.
        roi=lab[y1:y2,x1:x2]
        if roi.size==0:continue
        edges=np.concatenate([roi[0],roi[-1],roi[:,0],roi[:,-1]],axis=0).astype(np.float32)
        background=np.median(edges,axis=0)
        contrast=np.linalg.norm(roi.astype(np.float32)-background,axis=2)
        local=(contrast>threshold).astype(np.uint8)*255
        # Avoid broad fills in region-heavy illustrations: retain small objects.
        n,labels,stats,_=cv2.connectedComponentsWithStats(local,8)
        fine=np.zeros_like(local)
        for i in range(1,n):
            area=stats[i,cv2.CC_STAT_AREA]
            if area>=2 and area<.48*local.size:fine[labels==i]=255
        if not np.any(fine):
            # Rather than pretending a successful mask, leave this region blank
            # and report empty coverage in the output QA.
            continue
        mask[y1:y2,x1:x2]=cv2.bitwise_or(mask[y1:y2,x1:x2],fine)
    if padding:
        k=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*padding+1,2*padding+1))
        mask=cv2.dilate(mask,k,iterations=1)
    return mask


def generate_clean_candidate(p:dict,sid:str,regions:list[dict],mask_mode='ink',method='telea',padding=4,threshold=32,source_role='design') -> dict:
    s=find_slide(p,sid)
    source=source_for(p,sid,source_role)
    if not regions or len(regions)>350:raise ValueError('需要1–350个有效掩码区域')
    if method not in ('telea','ns','lama'):raise ValueError('背景修补方式不支持')
    arr=image_arr(source)
    mask=make_mask(arr,regions,mode=mask_mode,padding=padding,threshold=threshold)
    if not np.any(mask):raise ValueError('掩码为空，请改用矩形Mask或降低阈值')
    if mask.mean()/255>.45:raise ValueError('掩码覆盖超过45%的整页，风险过高，请缩小区域')
    if method=='lama':
        try:
            from simple_lama_inpainting import SimpleLama
        except ImportError as e:
            raise ValueError('LaMa尚未安装。请安装可选扩展，或使用Telea/NS；不会静默降级。') from e
        res=np.asarray(SimpleLama()(Image.fromarray(arr),Image.fromarray(mask)))
        if res.ndim!=3:raise ValueError('LaMa返回图片无效')
        res=res[:,:,:3]
    else:
        bgr=cv2.cvtColor(arr,cv2.COLOR_RGB2BGR)
        alg=cv2.INPAINT_TELEA if method=='telea' else cv2.INPAINT_NS
        res=cv2.cvtColor(cv2.inpaint(bgr,mask,3,alg),cv2.COLOR_BGR2RGB)
    out=lab_dir(p['id'])
    Image.fromarray(mask).save(out/f'{sid}_mask.png')
    # Outside the mask stays byte-exact (PIL decoding) and is never redesigned.
    Image.fromarray(res).save(out/f'{sid}_candidate.png')
    compare=np.asarray(Image.fromarray(arr).convert('RGBA')).copy()
    compare[mask>0]=np.array([255,40,100,255],dtype=np.uint8)
    overlay=Image.blend(Image.fromarray(compare),Image.fromarray(arr).convert('RGBA'),0.60)
    overlay.save(out/f'{sid}_mask_overlay.png')
    # Visual inspection strips (before vs after) for a bounded set of regions.
    # These are diagnostics, not evidence that inpainting is artifact-free.
    tiles=[]
    for item in regions[:12]:
        x1,y1,x2,y2=as_pixels(item['box'],arr.shape[1],arr.shape[0])
        pad=max(12,int((y2-y1)*.7))
        a1=max(0,x1-pad);b1=max(0,y1-pad);a2=min(arr.shape[1],x2+pad);b2=min(arr.shape[0],y2+pad)
        left=Image.fromarray(arr[b1:b2,a1:a2]);right=Image.fromarray(res[b1:b2,a1:a2])
        scale=min(1.7,450/max(1,left.width))
        new=(max(1,int(left.width*scale)),max(1,int(left.height*scale)))
        left=left.resize(new);right=right.resize(new)
        tile=Image.new('RGB',(new[0]*2+16,new[1]+22),'white')
        tile.paste(left,(0,22));tile.paste(right,(new[0]+16,22))
        ImageDraw.Draw(tile).text((3,3),'SOURCE  |  CANDIDATE',fill='#111111')
        tiles.append(tile)
    if tiles:
        sheet=Image.new('RGB',(max(x.width for x in tiles),sum(x.height for x in tiles)+6*(len(tiles)-1)),'#E7EAF0')
        y=0
        for tile in tiles:sheet.paste(tile,(0,y));y+=tile.height+6
        sheet.save(out/f'{sid}_inspection.png')
    stats={'slide_id':sid,'source_role':source_role,'source_sha256':sha_file(source),
           'source_design_sha256':sha_file(source_for(p,sid,'design')),
           'source_clean_sha256':sha_file(source_for(p,sid,'clean')) if s.get('images',{}).get('clean') else None,
           'mode':mask_mode,'method':method,
           'selected_text_regions':len(regions), 'selective_background':True,
           'padding':padding,'threshold':threshold,'mask_percent':round(float(np.mean(mask>0)*100),3),
           'regions':len(regions),'created_at':now(),
           'warning':'修复只是推测被遮挡的像素；需检查背景纹理、文字残留和重要主体。'}
    safe_json_write(out/f'{sid}_candidate_report.json',stats)
    return {'candidate':f'{sid}_candidate.png','inspection':f'{sid}_inspection.png','mask':f'{sid}_mask.png',
            'overlay':f'{sid}_mask_overlay.png','report':stats}


def accept_clean(p:dict,sid:str) -> dict:
    folder=lab_dir(p['id']);candidate=folder/f'{sid}_candidate.png'
    report=folder/f'{sid}_candidate_report.json'
    if not candidate.is_file() or not report.is_file():raise ValueError('请先生成候选底图')
    meta=json.loads(report.read_text(encoding='utf-8'))
    role=meta.get('source_role','design')
    try:src=source_for(p,sid,role)
    except (ValueError,FileNotFoundError) as exc:raise ValueError('候选来源图片已失效，请重新生成候选') from exc
    design=source_for(p,sid,'design')
    if (meta.get('source_sha256')!=sha_file(src)
        or meta.get('source_design_sha256',sha_file(design))!=sha_file(design)
        or (meta.get('source_clean_sha256') and meta['source_clean_sha256']!=sha_file(source_for(p,sid,'clean')))):
        raise ValueError('候选来源图片已改变，候选底图过期，请重新生成')
    s=find_slide(p,sid);images=project_dir(p['id'])/'images';out=images/f'{sid}_clean.png'
    # Back up the previous clean, regardless of its extension.
    old_name=s['images'].get('clean')
    if old_name:
        old_file=images/old_name
        if old_file.exists():
            backup=folder/f'{sid}_previous_clean.png'
            with Image.open(old_file) as original:
                original.save(backup,format='PNG')
            if old_file!=out:old_file.unlink()
    shutil.copy2(candidate,out)
    s['images']['clean']=out.name
    with Image.open(out) as im:w,h=im.size
    s.setdefault('image_metadata',{})['clean']={'sha256':sha_file(out),'width':w,'height':h,
        'generated_by':'approved_local_reconstruction','method':meta['method'],
        'accepted_at':now(),'warning':meta['warning'],'source_design_sha256':meta['source_sha256'],'stale_source':False,'embedded_text_remaining':True}
    save_project(p)
    return {'slide_id':sid,'filename':out.name,'previous_clean_backed_up':(folder/f'{sid}_previous_clean.png').exists()}


def candidate_is_current(p:dict,sid:str,meta:dict)->bool:
    try:
        role=meta.get('source_role','design')
        source=source_for(p,sid,role);design=source_for(p,sid,'design')
        return (meta.get('source_sha256')==sha_file(source)
                and meta.get('source_design_sha256',sha_file(design))==sha_file(design)
                and (not meta.get('source_clean_sha256') or meta['source_clean_sha256']==sha_file(source_for(p,sid,'clean'))))
    except (ValueError,FileNotFoundError):
        return False


def extract_asset(p:dict,sid:str,box:list[float],make_transparent=False,tolerance=26) -> dict:
    arr=image_arr(source_for(p,sid));h,w=arr.shape[:2]
    x1,y1,x2,y2=as_pixels(box,w,h)
    if (x2-x1)*(y2-y1)<100:raise ValueError('裁剪区域过小')
    crop=arr[y1:y2,x1:x2,:]
    rgba=np.dstack((crop,np.full(crop.shape[:2],255,dtype=np.uint8)))
    if make_transparent:
        # Uniform border matte, not automatic semantic segmentation.
        edge=np.concatenate([crop[0],crop[-1],crop[:,0],crop[:,-1]],axis=0).astype(np.float32)
        med=np.median(edge,axis=0)
        if np.max(np.linalg.norm(edge-med,axis=1))>65:
            raise ValueError('边缘背景颜色不统一，不适合透明化，请直接裁剪或使用SAM分割。')
        delta=np.linalg.norm(crop.astype(np.float32)-med,axis=2)
        alpha=np.clip((delta-tolerance)/(max(1,tolerance)*1.5),0,1)
        rgba[:,:,3]=(alpha*255).astype(np.uint8)
    s=find_slide(p,sid)
    images=project_dir(p['id'])/'images'
    index=next((i for i in range(1,100) if f'asset_{i:02d}' not in s['images']),None)
    if index is None:raise ValueError('这一页的独立素材槽位已用尽')
    role=f'asset_{index:02d}'
    path=images/f'{sid}_{role}.png';Image.fromarray(rgba).save(path)
    s['images'][role]=path.name
    s.setdefault('image_metadata',{})[role]={'sha256':sha_file(path),'width':x2-x1,'height':y2-y1,
       'source':'cropped_from_design','box':box,'transparent_matting':make_transparent,'extracted_at':now()}
    save_project(p)
    return {'role':role,'filename':path.name,'box':box,
            'notice':'仅裁剪已可见像素。复杂对象仍是独立位图，不代表内部细节可编辑。'}


def compare_render(p:dict,sid:str,preview_path:Path,output_dir:Path|None=None) -> dict:
    target=Image.open(source_for(p,sid)).convert('RGB')
    actual=Image.open(preview_path).convert('RGB').resize(target.size,Image.Resampling.LANCZOS)
    a=np.asarray(target).astype(np.int16);b=np.asarray(actual).astype(np.int16)
    diff=np.mean(np.abs(a-b),axis=2).astype(np.float32)
    editable_mask=np.zeros(diff.shape,dtype=bool)
    text_regions=[]
    for element in find_slide(p,sid).get('elements',[]):
        if element.get('kind')!='text' or not element.get('text','').strip():continue
        x1,y1,x2,y2=as_pixels(element.get('box'),target.width,target.height)
        if x2<=x1 or y2<=y1:continue
        editable_mask[y1:y2,x1:x2]=True
        text_regions.append({'id':str(element.get('id',''))[:64],
            'box':element['box'],'mae_0_255':round(float(np.mean(diff[y1:y2,x1:x2])),3)})
    heat=cv2.applyColorMap(np.clip(diff*3,0,255).astype(np.uint8),cv2.COLORMAP_TURBO)
    folder=output_dir or lab_dir(p['id']);folder.mkdir(parents=True,exist_ok=True);name=f'{sid}_diff.png'
    Image.fromarray(cv2.cvtColor(heat,cv2.COLOR_BGR2RGB)).save(folder/name)
    objective=score_render_fidelity(p,sid,target,actual)
    report={'slide_id':sid,'mae_0_255':round(float(np.mean(diff)),3),'source_size':list(target.size),
            'native_text_mae_0_255':round(float(np.mean(diff[editable_mask])),3) if editable_mask.any() else None,
            'background_mae_0_255':round(float(np.mean(diff[~editable_mask])),3) if (~editable_mask).any() else None,
            'native_text_regions':text_regions,
            'objective_score':objective,
            'note':'objective_score由OCR文字匹配、框坐标、字号/颜色及文字遮罩外背景共同计算；是工程验收指标，不等价于人工审美。','diff':name}
    safe_json_write(folder/f'{sid}_diff_report.json',report)
    return report


def _plain_text(text:str) -> str:
    return re.sub(r'[\W_]+','',str(text or '').lower(),flags=re.UNICODE)


def _is_math_region(text:str) -> bool:
    return len(re.findall(r'[=∑∏√∫^]|\\frac|\\sum|\\int',str(text or '')))>=2


def _math_text(text:str) -> str:
    value=(str(text or '').lower().replace('−','-').replace('×','*').replace('·','.')
           .replace('σ','∑').replace('\\sum','∑').replace('\\int','∫').replace('\\frac','/'))
    return re.sub(r'\s+','',value)


def _math_operators(text:str) -> tuple[str,...]:
    return tuple(sorted(re.findall(r'[=∑∏√∫^*/+\-]',_math_text(text))))


def _ink_profile(img:np.ndarray,box:list[float]) -> dict:
    """Measure a size-normalized glyph weight proxy, independent of ink color.

    The median local stroke diameter is more useful than raw pixel coverage when
    the source and PPT use different font families. Coverage remains a secondary
    signal because glyph design (especially CJK vs. Latin) changes its value.
    """
    h,w=img.shape[:2];x1,y1,x2,y2=as_pixels(box,w,h)
    if x2<=x1 or y2<=y1:return {'density':0.0,'stroke_ratio':0.0,'confidence':0.0}
    pad=max(2,int((y2-y1)*.45));crop=img[max(0,y1-pad):min(h,y2+pad),max(0,x1-pad):min(w,x2+pad)]
    core=img[y1:y2,x1:x2]
    if crop.size==0 or core.size==0:return {'density':0.0,'stroke_ratio':0.0,'confidence':0.0}
    border=np.concatenate([crop[0],crop[-1],crop[:,0],crop[:,-1]],axis=0).astype(np.float32)
    background=np.median(border,axis=0)
    pixels=core.astype(np.float32)
    distance=np.linalg.norm(pixels-background,axis=2)
    # Thresholding chroma distance makes this work for colored text too. Crop to
    # actual glyph bounds so varying OCR padding does not dilute the estimate.
    mask=(distance>=34).astype(np.uint8)
    ys,xs=np.where(mask>0)
    if len(xs)<4:return {'density':float(np.mean(mask)),'stroke_ratio':0.0,'confidence':0.0}
    mask=mask[ys.min():ys.max()+1,xs.min():xs.max()+1]
    glyph_height=max(1,mask.shape[0])
    dist=cv2.distanceTransform(mask,cv2.DIST_L2,5)
    local_max=cv2.dilate(dist,np.ones((3,3),np.uint8))
    peaks=dist[(dist>=local_max-.05)&(dist>=.8)]
    if len(peaks)==0:return {'density':float(np.mean(mask)),'stroke_ratio':0.0,'confidence':0.0}
    # Ignore the thickest 20% of shapes (e.g. dots/icons accidentally included)
    # and use a robust median diameter normalized by observed glyph height.
    cutoff=float(np.quantile(peaks,.8))
    stroke_ratio=float(np.median(peaks[peaks<=cutoff])*2/glyph_height)
    confidence=min(1.0,len(peaks)/max(8,mask.shape[0]*mask.shape[1]*.015))
    return {'density':float(np.mean(mask)),'stroke_ratio':stroke_ratio,'confidence':confidence}


def _ink_density(img:np.ndarray,box:list[float]) -> float:
    """Backward-compatible coverage component of the glyph ink profile."""
    return _ink_profile(img,box)['density']


def _match_ocr_regions(expected:list[dict],observed:list[dict],max_center_delta:float=.22) -> list[tuple[dict,dict|None,float]]:
    """Pair OCR regions globally, so repeated short labels stay at their positions.

    A stray digit in a diagram must not be paired with a page number merely
    because the OCR strings happen to match. Leave remote candidates unmatched
    so the calibration agent gets a truthful missing-region signal. Candidate
    pairs are ranked across the whole slide before assignment; a per-reference
    greedy pass can consume a nearby observation needed by a later duplicate
    (for example three identical "大模型" labels) and silently swap their IDs.
    """
    refs=[];pair_scores={}
    for ref in expected:
        math_region=_is_math_region(ref.get('text',''))
        source=(_math_text if math_region else _plain_text)(ref.get('text',''))
        if not source:continue
        refs.append(ref);i=len(refs)-1
        for j,obs in enumerate(observed):
            seen=(_math_text if math_region else _plain_text)(obs.get('text',''))
            similarity=SequenceMatcher(None,source,seen,autojunk=False).ratio()
            a=ref.get('box',[0,0,0,0]);b=obs.get('box',[0,0,0,0])
            center=float(np.hypot((a[0]+a[2]/2)-(b[0]+b[2]/2),(a[1]+a[3]/2)-(b[1]+b[3]/2)))
            if center>max_center_delta:continue
            joint=.82*similarity+.18*float(np.exp(-center/.08))
            if similarity>=.35:pair_scores[(i,j)]=(joint,similarity)
    if not refs:return []
    # Hungarian minimum-cost assignment with one dummy unmatched column per
    # reference. Dummies cost zero; invalid or remote pairs cost more, while
    # valid pairs have negative utility, so all selected matches are useful.
    n=len(refs);real_columns=len(observed);m=real_columns+n
    costs=[[-pair_scores[(i,j)][0] if (i,j) in pair_scores else 1e3 for j in range(real_columns)]+[0.0]*n for i in range(n)]
    u=[0.0]*(n+1);v=[0.0]*(m+1);p=[0]*(m+1);way=[0]*(m+1)
    for i in range(1,n+1):
        p[0]=i;j0=0;minv=[float('inf')]*(m+1);used=[False]*(m+1)
        while True:
            used[j0]=True;i0=p[j0];delta=float('inf');j1=0
            for j in range(1,m+1):
                if used[j]:continue
                cur=costs[i0-1][j-1]-u[i0]-v[j]
                if cur<minv[j]:minv[j]=cur;way[j]=j0
                if minv[j]<delta:delta=minv[j];j1=j
            for j in range(m+1):
                if used[j]:u[p[j]]+=delta;v[j]-=delta
                else:minv[j]-=delta
            j0=j1
            if p[j0]==0:break
        while True:
            j1=way[j0];p[j0]=p[j1];j0=j1
            if j0==0:break
    assigned={}
    for j in range(1,m+1):
        i=p[j]-1;obs_index=j-1
        if i>=0 and obs_index<real_columns and (i,obs_index) in pair_scores:
            assigned[i]=(observed[obs_index],pair_scores[(i,obs_index)][1])
    return [(ref,*assigned.get(i,(None,0.0))) for i,ref in enumerate(refs)]


def score_render_fidelity(p:dict,sid:str,target:Image.Image,actual:Image.Image) -> dict:
    """Score a real Office render against its design using observable signals.

    DeepSeek's numeric opinion is intentionally excluded: the score is derived
    from OCR matches, their measured geometry/style and non-text background.
    """
    engines=available_ocr()
    if not engines.get('rapidocr'):
        return {'score':None,'threshold':FIDELITY_THRESHOLD,'status':'unavailable','notice':'自动评分需要 RapidOCR。'}
    target_arr=np.asarray(target.convert('RGB'));actual_arr=np.asarray(actual.convert('RGB'))
    region_file=get_regions(p,sid)
    expected=(region_file or {}).get('regions',[]) if (region_file or {}).get('source_valid') else []
    expected=[r for r in expected if isinstance(r,dict) and r.get('text','').strip() and r.get('role')!='ignore' and float(r.get('confidence') or 0)>=.45]
    design_ocr=_rapidocr_multiscale(target_arr)
    if not expected:expected=design_ocr
    else:
        # Keep small labels/page numbers discovered directly from the original
        # even when a reviewed OCR map exists but omitted them.
        for detected in design_ocr:
            if float(detected.get('confidence') or 0)<.45:continue
            box=detected.get('box',[0,0,0,0]);cx=box[0]+box[2]/2;cy=box[1]+box[3]/2
            duplicate=any(
                SequenceMatcher(None,_plain_text(detected.get('text','')),_plain_text(r.get('text','')),autojunk=False).ratio()>=.65
                and np.hypot(cx-(r['box'][0]+r['box'][2]/2),cy-(r['box'][1]+r['box'][3]/2))<=.018
                for r in expected
            )
            if not duplicate:
                duplicate=any(_contained_ocr_fragment(box,r['box']) for r in expected)
            if not duplicate:
                detected['style']=_estimate_text_style(target_arr,box)
                expected.append(detected)
    expected=_rapidocr_multiscale(target_arr) if not expected else expected
    # The design is comprehensively scanned and cached once. For each trial,
    # scan the actual render normally plus overlapping high-resolution tiles;
    # skipping a redundant whole-slide 2x pass keeps iterative scoring usable.
    observed=_dedupe_ocr_fragments(_rapidocr_multiscale(actual_arr,full_upscale=False))
    if not expected or not observed:
        return {'score':0,'threshold':FIDELITY_THRESHOLD,'status':'insufficient_ocr','expected_regions':len(expected),'observed_regions':len(observed),'notice':'未能在设计稿或PPT渲染图中稳定识别文字，不能宣称达标。'}
    matches=_match_ocr_regions(expected,observed)
    if not matches:
        return {'score':0,'threshold':FIDELITY_THRESHOLD,'status':'insufficient_ocr','expected_regions':len(expected),'observed_regions':len(observed),'notice':'没有可比较的文本区域。'}
    # OCR region IDs belong to the reference map, not to the editable PPT
    # objects. Resolve each region to the closest native text object using both
    # normalized text and slide position so the calibration agent receives IDs
    # it can actually edit (repeated labels such as "Token 1" stay distinct).
    source_slide=next((s for s in p.get('slides',[]) if s['id']==sid),{'elements':[]})
    native_text=[e for e in source_slide.get('elements',[])
                 if e.get('kind') in ('text','formula') and str(e.get('text','')).strip()]
    def native_target(ref:dict) -> dict|None:
        ref_text=_plain_text(str(ref.get('text','')))
        if not ref_text:return None
        rb=ref.get('box',[0,0,0,0]);rcx=float(rb[0])+float(rb[2])/2;rcy=float(rb[1])+float(rb[3])/2
        candidates=[]
        for element in native_text:
            text=_plain_text(str(element.get('text','')))
            if not text:continue
            similarity=SequenceMatcher(None,ref_text,text,autojunk=False).ratio()
            eb=element.get('box',[0,0,0,0]);ecx=float(eb[0])+float(eb[2])/2;ecy=float(eb[1])+float(eb[3])/2
            distance=float(np.hypot(rcx-ecx,rcy-ecy))
            candidates.append((.78*similarity+.22*float(np.exp(-distance/.08)),similarity,distance,element))
        if not candidates:return None
        _,similarity,distance,element=max(candidates,key=lambda row:row[:3])
        if similarity<.52 or distance>.18:return None
        # Distinguish OCR-measured glyph bounds from the editable PowerPoint
        # text-frame geometry. Calibration must never copy OCR boxes into PPT.
        element_box=element.get('box',[0,0,0,0])
        style=element.get('style') or {}
        style_keys=('size','font','eastAsianFont','color','bold','italic','align')
        return {'id':str(element.get('id','')),'expected_text':str(element.get('text',''))[:100],
                'reference_region_id':str(ref.get('id',''))[:64],
                'editable_box':[round(float(v),4) for v in element_box],
                'editable_style':{key:style[key] for key in style_keys if key in style}}
    targets={id(ref):native_target(ref) for ref,obs,_ in matches}
    def target_fields(ref:dict) -> dict:
        target=targets.get(id(ref))
        if target:return target
        return {'reference_region_id':str(ref.get('id',''))[:64],
                'expected_text':str(ref.get('text',''))[:100]}
    text_score=float(np.mean([sim for _,obs,sim in matches]))
    math_refs=[(ref,obs,sim) for ref,obs,sim in matches if _is_math_region(ref.get('text',''))]
    geometry=[];color=[];size=[];weight=[];valid_matches=0;differences=[]
    for ref,obs,sim in matches:
        if obs is None:
            differences.append({'type':'missing_text',**target_fields(ref),
                                'expected':str(ref.get('text',''))[:100],'actual':'未检测到'})
            continue
        valid_matches+=1
        a=ref.get('box',[0,0,0,0]);b=obs.get('box',[0,0,0,0])
        acx=a[0]+a[2]/2;acy=a[1]+a[3]/2;bcx=b[0]+b[2]/2;bcy=b[1]+b[3]/2
        center=float(np.hypot(acx-bcx,acy-bcy));center_score=float(np.exp(-center/.035))
        # Center-only geometry missed the user's most visible defect: a text
        # box can start at the right place yet wrap/crowd because its size differs.
        width_score=float(np.exp(-abs(np.log(max(b[2],1e-6)/max(a[2],1e-6)))/.45))
        height_score=float(np.exp(-abs(np.log(max(b[3],1e-6)/max(a[3],1e-6)))/.45))
        geometry.append(.40*center_score+.30*width_score+.30*height_score)
        if sim<.92:
            differences.append({'type':'text_content',**target_fields(ref),
                                'expected':str(ref.get('text',''))[:100],'actual':str(obs.get('text',''))[:100],'similarity':round(sim,3)})
        # Small labels in diagrams can visibly sit outside their background
        # shapes after a 1% slide-height shift. The old 2.5% center threshold
        # hid those defects from DeepSeek, while the loose size threshold let
        # clipped one-line labels pass as equivalent.
        if center>.010 or width_score<.86 or height_score<.86:
            differences.append({'type':'position_or_size',**target_fields(ref),
                                'expected_box':[round(float(v),4) for v in a],
                                'actual_box':[round(float(v),4) for v in b],
                                'center_delta':round(center,4),'width_score':round(width_score,3),'height_score':round(height_score,3)})
        expected_style=ref.get('style') or _estimate_text_style(target_arr,a)
        ca=expected_style.get('color');cb=_estimate_fidelity_ink_color(actual_arr,b)
        if ca and cb:
            try:
                va=np.array([int(ca[k:k+2],16) for k in (0,2,4)],dtype=float);vb=np.array([int(cb[k:k+2],16) for k in (0,2,4)],dtype=float)
                color.append(max(0.0,1.0-float(np.linalg.norm(va-vb))/441.673))
                if float(np.linalg.norm(va-vb))>45:
                    differences.append({'type':'text_color',**target_fields(ref),'expected':ca,'actual':cb,'text':str(ref.get('text',''))[:80]})
            except (ValueError,TypeError):pass
        ha=float(a[3]);hb=float(b[3])
        if ha>0 and hb>0:
            size.append(float(np.exp(-abs(np.log(hb/ha))/.45)))
            if abs(np.log(hb/ha))>.18:
                differences.append({'type':'glyph_size',**target_fields(ref),'expected_height':round(ha,4),'actual_height':round(hb,4),'text':str(ref.get('text',''))[:80]})
        expected_ink=_ink_profile(target_arr,a);actual_ink=_ink_profile(actual_arr,b)
        expected_stroke=expected_ink['stroke_ratio'];actual_stroke=actual_ink['stroke_ratio']
        if expected_stroke>.002 and actual_stroke>.002 and min(expected_ink['confidence'],actual_ink['confidence'])>=.35:
            stroke_ratio=actual_stroke/expected_stroke
            density_ratio=(actual_ink['density']+.002)/(expected_ink['density']+.002)
            # Stroke thickness dominates; coverage is deliberately secondary so
            # a different typeface is not mistaken for a different font weight.
            weight_ratio=stroke_ratio**.8*density_ratio**.2
            weight.append(float(np.exp(-abs(np.log(weight_ratio))/.30)))
            if stroke_ratio<.84 or stroke_ratio>1.19:
                differences.append({'type':'text_weight',**target_fields(ref),'metric':'normalized_stroke_thickness',
                                    'expected_stroke_ratio':round(expected_stroke,4),
                                    'actual_stroke_ratio':round(actual_stroke,4),
                                    'stroke_ratio':round(stroke_ratio,3),
                                    'expected_density':round(expected_ink['density'],4),
                                    'actual_density':round(actual_ink['density'],4),
                                    'ratio':round(weight_ratio,3),
                                    'direction':'actual_lighter' if stroke_ratio<1 else 'actual_heavier',
                                    'text':str(ref.get('text',''))[:80]})
    # OCR geometry already exists for both the reference and actual render.
    # Compare pairwise text-box collisions so a visibly crowded label can count
    # as a real local improvement even when the rounded page score is unchanged.
    observed_pairs=[(ref,obs) for ref,obs,_ in matches if obs is not None]
    def overlap_ratio(a:list[float],b:list[float]) -> float:
        ax,ay,aw,ah=map(float,a);bx,by,bw,bh=map(float,b)
        intersection=max(0.0,min(ax+aw,bx+bw)-max(ax,bx))*max(0.0,min(ay+ah,by+bh)-max(ay,by))
        return intersection/max(1e-9,min(aw*ah,bw*bh))
    for index,(ref_a,obs_a) in enumerate(observed_pairs):
        for ref_b,obs_b in observed_pairs[index+1:]:
            expected_overlap=overlap_ratio(ref_a.get('box',[0,0,0,0]),ref_b.get('box',[0,0,0,0]))
            actual_overlap=overlap_ratio(obs_a.get('box',[0,0,0,0]),obs_b.get('box',[0,0,0,0]))
            if actual_overlap>=.12 and actual_overlap-expected_overlap>=.10:
                text_a=str(ref_a.get('text',''))[:48];text_b=str(ref_b.get('text',''))[:48]
                target_a=targets.get(id(ref_a));target_b=targets.get(id(ref_b))
                differences.append({'type':'text_overlap','text':f'{text_a} / {text_b}',
                                    'expected_overlap':round(expected_overlap,3),
                                    'actual_overlap':round(actual_overlap,3),
                                    'objects':[target_a.get('id') if target_a else None,
                                               target_b.get('id') if target_b else None],
                                    'object_texts':[target_a.get('expected_text') if target_a else text_a,
                                                    target_b.get('expected_text') if target_b else text_b],
                                    'reference_regions':[str(ref_a.get('id',''))[:64],str(ref_b.get('id',''))[:64]]})
    coverage=valid_matches/max(1,len(matches))
    precision=valid_matches/max(1,len(observed))
    semantic=(text_score*coverage*precision)**(1/3) if text_score and coverage and precision else 0.0
    geometry_score=float(np.mean(geometry)) if geometry else 0.0
    style_score=((float(np.mean(color)) if color else .5)*.40+
                 (float(np.mean(size)) if size else .5)*.30+
                 (float(np.mean(weight)) if weight else .5)*.30)
    # Exclude padded OCR text boxes so native-glyph differences do not penalize
    # the preserved clean background; compare the rest as a structural check.
    bg_mask=np.ones(target_arr.shape[:2],dtype=bool)
    for r in expected:
        x1,y1,x2,y2=as_pixels(r['box'],target.width,target.height)
        pad=max(3,int((y2-y1)*.35));bg_mask[max(0,y1-pad):min(target.height,y2+pad),max(0,x1-pad):min(target.width,x2+pad)]=False
    bg_diff=np.mean(np.abs(target_arr.astype(np.int16)-actual_arr.astype(np.int16)),axis=2)
    background=float(np.exp(-float(np.mean(bg_diff[bg_mask]))/36)) if bg_mask.any() else .5
    score=round(100*(.40*semantic+.30*geometry_score+.20*style_score+.10*background))
    # A page must not pass the fidelity gate while native text boxes are
    # predictably too small. This is a conservative deterministic guard; the
    # final visual score still comes from the rendered-image comparison above.
    layout_warnings=[]
    try:
        from .quality import _estimated_lines
        source_slide=next((s for s in p.get('slides',[]) if s['id']==sid),{'elements':[]})
        for element in source_slide.get('elements',[]):
            if element.get('kind')!='text' or not str(element.get('text') or '').strip():continue
            x,y,w,h=element['box'];style=element.get('style') or {};font_size=float(style.get('size',18))
            available_width=w*13.333333*72-float(style.get('margin_left_pt',0))-float(style.get('margin_right_pt',0))
            available_height=h*7.5*72-float(style.get('margin_top_pt',0))-float(style.get('margin_bottom_pt',0))
            required_height=_estimated_lines(element['text'],available_width,font_size,style.get('char_spacing_pt',0))*font_size*1.22*float(style.get('line_spacing',1.05))
            required_height+=max(0,str(element['text']).count('\n'))*float(style.get('paragraph_space_after_pt',0))
            overflow_ratio=required_height/max(available_height*1.15,1)
            if overflow_ratio>=1.45:
                layout_warnings.append({'id':element.get('id'),'type':'likely_overflow','ratio':round(overflow_ratio,2)})
    except (KeyError,TypeError,ValueError):
        layout_warnings=[]
    # The width estimator is intentionally conservative and can over-predict
    # wraps for long Latin/CJK mixed headings. A complete OCR match whose
    # measured glyph box remains inside its actual PPT text frame is direct
    # render evidence that the estimated overflow did not occur.
    if layout_warnings:
        source_slide=next((s for s in p.get('slides',[]) if s['id']==sid),{'elements':[]})
        elements={str(e.get('id')):e for e in source_slide.get('elements',[])}
        verified_warnings=[]
        for warning in layout_warnings:
            element=elements.get(str(warning.get('id')))
            if element is None:
                verified_warnings.append(warning);continue
            text=_plain_text(element.get('text',''));x,y,w,h=map(float,element.get('box',[0,0,0,0]))
            visible=[]
            for detected in observed:
                similarity=SequenceMatcher(None,text,_plain_text(detected.get('text','')),autojunk=False).ratio()
                b=detected.get('box',[0,0,0,0]);cx=b[0]+b[2]/2;cy=b[1]+b[3]/2
                ecx=x+w/2;ecy=y+h/2
                if similarity>=.90 and np.hypot(cx-ecx,cy-ecy)<=.05:
                    visible.append((similarity,-float(np.hypot(cx-ecx,cy-ecy)),detected))
            if not visible:
                verified_warnings.append(warning);continue
            detected=max(visible,key=lambda item:(item[0],item[1]))[2]
            bx,by,bw,bh=map(float,detected['box']);tol=.003
            if bx<x-tol or by<y-tol or bx+bw>x+w+tol or by+bh>y+h+tol:
                verified_warnings.append(warning)
        layout_warnings=verified_warnings
    safety_cap=FIDELITY_THRESHOLD-1
    if layout_warnings:score=min(score,safety_cap)
    formula_ok=all(obs is not None and sim>=.90 and
                   _math_operators(ref.get('text',''))==_math_operators(obs.get('text',''))
                   for ref,obs,sim in math_refs)
    cjk_matches=[(ref,obs) for ref,obs,_ in matches
                 if re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]',str(ref.get('text','')))]
    cjk_detected=sum(bool(obs and re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]',str(obs.get('text',''))))
                     for _,obs in cjk_matches)
    cjk_coverage=cjk_detected/max(1,len(cjk_matches)) if cjk_matches else None
    cjk_missing=bool(cjk_matches and cjk_coverage<.25)
    if cjk_missing:score=min(score,49)
    if math_refs and not formula_ok:score=min(score,safety_cap)
    status='cjk_text_not_detected' if cjk_missing else ('pass' if score>=FIDELITY_THRESHOLD and formula_ok else 'below_threshold')
    notice=f'评分v4由OCR文字与覆盖率、文字框中心/宽高、文字颜色/实际字高、按字高归一化的局部笔画厚度（字重代理，墨迹覆盖率仅作次要信号）及非文字背景实测构成；不采用DeepSeek自评。不同字体家族允许存在，但可见笔画粗细应接近。字距/行距通过实际渲染后的文字框宽高和溢出门禁间接反映，并非独立精确测量。疑似公式须通过文本与运算符校验；否则封顶{safety_cap}分。{FIDELITY_THRESHOLD}分是自动停止门槛，不等于已被证明的{FIDELITY_THRESHOLD}%人类视觉相似度。'
    if layout_warnings:notice+=f' 检测到可能溢出的原生文字框 {len(layout_warnings)} 个，分数封顶{safety_cap}；优化优先修复这些框。'
    if math_refs and not formula_ok:
        differences.extend({'type':'formula_mismatch','expected':str(ref.get('text',''))[:100],
                            'actual':str(obs.get('text',''))[:100] if obs else '未识别'}
                           for ref,obs,sim in math_refs
                           if not obs or sim<.90 or _math_operators(ref.get('text',''))!=_math_operators(obs.get('text','')))
    if cjk_missing:
        notice=f'渲染图中的中文 OCR 覆盖仅 {cjk_detected}/{len(cjk_matches)} 个中文区域；已暂停 DeepSeek 参数校准并将分数封顶49。请修复东亚字体/渲染后重新评分，不能根据缺字画面调坐标或字号。'
    return {'score':score,'threshold':FIDELITY_THRESHOLD,'status':status,
            'scoring_version':4,
            'components':{'text':round(semantic*100,1),'position':round(geometry_score*100,1),
                          'typography':round(style_score*100,1),'background':round(background*100,1),
                          'weight':round(float(np.mean(weight))*100,1) if weight else None,
                          'formula':100.0 if not math_refs or formula_ok else 0.0},
            'expected_regions':len(matches),'matched_regions':valid_matches,'observed_regions':len(observed),
            'cjk_expected_regions':len(cjk_matches),'cjk_detected_regions':cjk_detected,
            'cjk_coverage':round(cjk_coverage,4) if cjk_coverage is not None else None,
            'formula_regions':len(math_refs),'formula_verified':formula_ok,
            'layout_warnings':layout_warnings,
            # Overlapping observed text is an actionable readability defect;
            # keep it ahead of the many lower-priority style deltas so it
            # survives the bounded feedback payload sent to the vision agent.
            'differences':([d for d in differences if d.get('type')=='text_overlap']+
                          [d for d in differences if d.get('type')!='text_overlap']),
            'notice':notice}


def reconstruction_coverage(p:dict,sid:str) -> dict:
    """Honest native editability accounting for dense slides: never call raster text editable."""
    s=find_slide(p,sid); regions=get_regions(p,sid)
    if regions and not regions.get('source_valid'):regions=None
    if not regions and available_ocr().get('rapidocr'):
        # Populate line-level OCR once so export can detect grouped-body layouts
        # before wasting calibration calls on an unfixable single text box.
        regions=detect_text(p,sid,backend='rapidocr')
    all_regions=[x for x in (regions or {}).get('regions',[]) if x.get('role')!='ignore' and x.get('text','').strip()]
    enabled=[x for x in all_regions if x.get('editable')]
    text_objs=[e for e in s.get('elements',[]) if e.get('kind')=='text' and e.get('text','').strip()]
    formula_objs=[e for e in s.get('elements',[]) if e.get('kind')=='formula' and e.get('text','').strip()]
    report={'slide_id':sid,'ocr_text_regions':len(all_regions),
            'confirmed_editable_regions':len(enabled),'native_text_objects':len(text_objs),
            'native_formula_objects':len(formula_objs),
            'raster_text_regions_estimate':max(0,len(all_regions)-len(enabled)),
            'editable_fraction_of_ocr_regions':round(len(enabled)/len(all_regions),4) if all_regions else None,
            'note':'OCR检测可能遗漏或重复；该比例只是已人工确认的OCR区域覆盖率，不是整页全部像素的可编辑比例。'}
    return report


def initialize_raster_first(p:dict,sid:str) -> dict:
    """Explicit, reversible raster-first starting point for dense slides.

    Copies the untouched design as visual base. Existing text is still raster!
    Do NOT claim full editability. Later selective repair promotes chosen regions.
    """
    source=source_for(p,sid)
    obj=find_slide(p,sid)
    folder=lab_dir(p['id']);backup=folder/f'{sid}_elements_before_raster.json'
    if not backup.exists():safe_json_write(backup,obj.get('elements',[]))
    dst=project_dir(p['id'])/'images'/f'{sid}_clean.png'
    if dst.exists():
        with Image.open(dst) as im:im.save(folder/f'{sid}_previous_clean.png')
    with Image.open(source) as im:im.convert('RGB').save(dst,'PNG')
    obj['images']['clean']=dst.name
    obj['elements']=[]
    obj.setdefault('image_metadata',{})['clean']={
        'sha256':sha_file(dst),'source_design_sha256':sha_file(source),
        'stale_source':False,'embedded_text_remaining':True,
        'status':'RASTER_FIRST_BASELINE_NOT_FULLY_EDITABLE','created_at':now()}
    save_project(p)
    return {'slide_id':sid,'image':dst.name,'native_text_count':0,
            'notice':'原图已原样保留为可见背景，文字目前全部仍是栅格。之后逐块去字并提升为原生可编辑文字。已备份旧元素。'}
