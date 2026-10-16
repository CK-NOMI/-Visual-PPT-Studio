"""Source-compatible Scene Graph view over existing project.json.

Elements are kept as their original dicts: no lossy migration, no silent reflow.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from typing import Any
from .errors import ValidationError

KINDS = {'text', 'formula', 'rect', 'ellipse', 'line', 'image'}
COLOR_KEYS = {'color', 'fill', 'stroke'}
TEXT_STYLE = {'font', 'eastAsianFont', 'size', 'bold', 'italic', 'color', 'char_spacing_pt',
              'line_spacing', 'vertical_align', 'align', 'margin_left_pt',
              'margin_right_pt', 'margin_top_pt', 'margin_bottom_pt',
              'paragraph_space_before_pt', 'paragraph_space_after_pt',
              'underline', 'strike'}
SHAPE_STYLE = {'fill', 'stroke', 'strokeWidth'}
IMAGE_STYLE = {'fit','crop_left','crop_right','crop_top','crop_bottom'}
LINE_STYLE = {'stroke', 'strokeWidth', 'arrow'}
STYLES = {'text': TEXT_STYLE, 'formula': {'size', 'align'}, 'rect': SHAPE_STYLE,
          'ellipse': SHAPE_STYLE, 'line': LINE_STYLE, 'image': IMAGE_STYLE}

def cloned(value: Any) -> Any:
    return copy.deepcopy(value)

def signature(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()

def slide(project: dict, sid: str) -> dict:
    return next((s for s in project['slides'] if s.get('id') == sid), None) or _missing('slide', sid)

def element(page: dict, eid: str) -> dict:
    return next((e for e in page['elements'] if e.get('id') == eid), None) or _missing('element', eid)

def _missing(kind, name):
    raise ValidationError(f'{kind} does not exist: {name}')

def validate_box(box: list, *, allow_connector_endpoint=False) -> list[float]:
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValidationError('box must be [x,y,w,h]')
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in box):
        raise ValidationError('box values must be finite numbers')
    x,y,w,h = map(float,box)
    if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= w <= 1 and 0 <= h <= 1):
        raise ValidationError('box coordinates must be within 0..1')
    if not allow_connector_endpoint and (w <= 0 or h <= 0):
        raise ValidationError('box width/height must be positive')
    if x+w > 1.0000001 or y+h > 1.0000001:
        raise ValidationError('box extends past slide boundary')
    return [x,y,w,h]

def _number(v, low, high, name):
    if isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) or not low <= v <= high:
        raise ValidationError(f'{name} must be a finite number in [{low}, {high}]')
    return v

def _hex(v, name):
    if not isinstance(v,str) or len(v.lstrip('#')) != 6 or any(c not in '0123456789abcdefABCDEF' for c in v.lstrip('#')):
        raise ValidationError(f'{name} requires 6-digit RGB')
    return v.lstrip('#').upper()

def validate_style(kind: str, patch: dict) -> dict:
    if not isinstance(patch, dict): raise ValidationError('style must be an object')
    if kind not in STYLES: raise ValidationError(f'unsupported object kind {kind}')
    unknown = set(patch)-STYLES[kind]
    if unknown: raise ValidationError(f'unsupported {kind} style: {sorted(unknown)} (never silently ignored)')
    result={}
    for k,v in patch.items():
        if k in COLOR_KEYS: result[k]=_hex(v,k)
        elif k in ('bold','italic','arrow','underline','strike'):
            if not isinstance(v,bool): raise ValidationError(f'{k} must be boolean')
            result[k]=v
        elif k in ('font','eastAsianFont'):
            if not isinstance(v,str) or not 0<len(v)<=100 or any(ord(c)<32 for c in v):
                raise ValidationError('font must be a valid family name')
            result[k]=v
        elif k=='size': result[k]=_number(v,4,160,k)
        elif k=='char_spacing_pt':result[k]=_number(v,-5,20,k)
        elif k=='line_spacing':result[k]=_number(v,.7,2.5,k)
        elif k=='strokeWidth':result[k]=_number(v,0,72,k)
        elif k.startswith('crop_'):result[k]=_number(v,0,.99,k)
        elif k.endswith('_pt'):result[k]=_number(v,0,72,k)
        elif k=='align':
            if v not in ('left','center','right'):raise ValidationError('align must be left|center|right')
            result[k]=v
        elif k=='vertical_align':
            if v not in ('top','middle','bottom'):raise ValidationError('vertical_align must be top|middle|bottom')
            result[k]=v
        elif k=='fit':
            if v not in ('stretch','contain','cover'):raise ValidationError('fit must be stretch|contain|cover')
            result[k]=v
    return result

def validate_element(raw: dict, *, allowed_assets: set[str]|None=None, strict_style: bool=True) -> dict:
    if not isinstance(raw,dict) or raw.get('kind') not in KINDS:raise ValidationError('element kind unsupported')
    obj=cloned(raw)
    eid=obj.get('id')
    if not isinstance(eid,str) or not 0<len(eid)<=128 or any(ord(c)<33 for c in eid):
        raise ValidationError('invalid element id')
    obj['box']=validate_box(obj.get('box'),allow_connector_endpoint=obj['kind']=='line')
    if 'rotation' in obj:obj['rotation']=_number(obj['rotation'],0,360,'rotation')
    kind=obj['kind']
    if kind in ('text','formula'):
        if not isinstance(obj.get('text',''),str) or len(obj.get('text',''))>10000:
            raise ValidationError('invalid text')
        if kind=='text':
            if any(c in obj.get('text','') for c in '\r\v\f\u0085\u2028\u2029'):
                raise ValidationError('invalid control characters in text')
            validate_rich_runs(obj.get('rich_runs'), obj.get('text', ''), optional=True)
    if kind=='image':
        asset=obj.get('asset')
        if not isinstance(asset,str) or not asset or '/' in asset or '\\' in asset:
            raise ValidationError('image must reference an existing slide asset filename')
        if allowed_assets is None or asset not in allowed_assets:
            raise ValidationError('image asset not registered for this slide')
    if strict_style:
        obj['style']=validate_style(kind,obj.get('style',{}))
    else:
        # Existing project objects may contain unrelated historic properties.
        # Keep them round-trip intact; only new writes are strictly whitelisted.
        if not isinstance(obj.get('style',{}),dict):raise ValidationError('invalid existing style')
    if kind=='image':
        st=obj.get('style') or {}
        if st.get('crop_left',0)+st.get('crop_right',0)>=1 or st.get('crop_top',0)+st.get('crop_bottom',0)>=1:
            raise ValidationError('crop values leave no visible image area')
    return obj

def validate_scene(project:dict):
    if not isinstance(project,dict) or not isinstance(project.get('slides'),list):
        raise ValidationError('invalid project schema')
    for s in project['slides']:
        if not isinstance(s.get('elements'),list):raise ValidationError('slide elements missing')
        ids=[e.get('id') for e in s['elements']]
        if len(ids)!=len(set(ids)):raise ValidationError('duplicate element IDs')
        assets=set((s.get('images') or {}).values())
        for e in s['elements']:validate_element(e,allowed_assets=assets,strict_style=False)
    return True

def groups_for_page(page:dict)->list[dict]:
    """Read-only derived groups; never infer semantics from mere frame intersection."""
    rows={}
    for obj in page['elements']:
        if obj.get('line_id'):
            rows.setdefault(('line',obj['line_id']),[]).append(obj)
        if obj.get('semantic_group_id'):
            rows.setdefault(('semantic',obj['semantic_group_id']),[]).append(obj)
    result=[]
    for (kind,gid),objects in rows.items():
        if len(objects)<2:continue
        items=sorted(objects,key=lambda e:(e.get('run_order',1_000_000),page['elements'].index(e)))
        result.append({'id':gid,'type':kind,'element_ids':[e['id'] for e in items]})
    return result


# Native exporter bridge applies these rich-run styles after existing studio.pptx_native.build.
# The concatenated text remains the single content authority for OCR/QA/provenance.
RICH_RUN_STYLE = {'font','eastAsianFont','size','bold','italic','color',
                  'char_spacing_pt','underline','strike'}

def validate_rich_runs(runs, full_text, *, optional=False):
    if runs is None and optional:return None
    if not isinstance(runs,list) or not 1<=len(runs)<=200:
        raise ValidationError('rich_runs must contain 1..200 segments')
    combined=''
    normalized=[]
    for run in runs:
        if not isinstance(run,dict) or set(run)-{'text','style'}:
            raise ValidationError('rich_run supports exactly text and style')
        text=run.get('text')
        if not isinstance(text,str) or not text:
            raise ValidationError('rich_run text must be nonempty string')
        style=run.get('style',{})
        bad=set(style)-RICH_RUN_STYLE if isinstance(style,dict) else {'style'}
        if bad:raise ValidationError(f'unsupported rich_run properties: {sorted(bad)}')
        normalized.append({'text':text,'style':validate_style('text',style)})
        combined+=text
    if combined!=full_text:raise ValidationError('concatenated rich_runs must exactly match element.text')
    return normalized

def points_to_box(pt):
    """Precise 960x540 pt design-space box matching Studio 13 1/3 × 7.5 inch."""
    if not isinstance(pt,(list,tuple)) or len(pt)!=4:
        raise ValidationError('box_pt must be [x,y,width,height] in 960x540-point space')
    box=[]
    for idx,(value,total) in enumerate(zip(pt,(960,540,960,540))):
        _number(value,0,total,f'box_pt[{idx}]')
        box.append(float(value)/total)
    return validate_box(box)

def box_to_points(box):
    return [round(float(x)*span,5) for x,span in zip(box,(960,540,960,540))]
