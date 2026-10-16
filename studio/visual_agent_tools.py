"""Bounded editing tools for the Visual PPT agent.

The model never receives filesystem or Python execution access.  Every tool
operates on a deep-copied, isolated scene graph; callers decide whether the
result is rendered, accepted, or discarded.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
import uuid

from . import core


TOOL_NAMES = {
    'get_page_state', 'get_element', 'get_screenshot', 'get_region_screenshot',
    'edit_element', 'create_element', 'delete_element', 'duplicate_element',
    'reorder_element', 'batch_edit', 'align_elements', 'distribute_elements',
    'undo_edit',
}

TEXT_STYLE = {
    'font', 'eastAsianFont', 'size', 'color', 'bold', 'italic',
    'char_spacing_pt', 'line_spacing', 'paragraph_space_before_pt',
    'paragraph_space_after_pt', 'align', 'vertical_align',
    'margin_left_pt', 'margin_right_pt', 'margin_top_pt', 'margin_bottom_pt',
}
SHAPE_STYLE = {'fill', 'stroke', 'strokeWidth', 'arrow'}
IMAGE_STYLE = {'fit'}
ELEMENT_FIELDS = {'text', 'box', 'style', 'asset'}

TOOL_SCHEMA = [
    {'name': 'get_page_state', 'description': '读取当前页对象清单和页面尺寸', 'parameters': {'slide_id': 'string'}},
    {'name': 'get_element', 'description': '读取一个对象的完整属性', 'parameters': {'slide_id': 'string', 'element_id': 'string'}},
    {'name': 'get_screenshot', 'description': '读取当前真实页面截图；由调用层提供截图', 'parameters': {'slide_id': 'string'}},
    {'name': 'get_region_screenshot', 'description': '读取归一化区域截图；由调用层提供截图', 'parameters': {'slide_id': 'string', 'region': '[x,y,w,h]'}},
    {'name': 'edit_element', 'description': '直接编辑当前页已有对象的文字、样式或几何；本页候选事务内无需问题ID或OCR证据', 'parameters': {'slide_id': 'string', 'element_id': 'string', 'patch': 'object', 'evidence_issue_id': 'string?'}},
    {'name': 'create_element', 'description': '在当前页创建原生文字、公式、形状、线或已有素材图片；本页候选事务内无需来源白名单', 'parameters': {'slide_id': 'string', 'element': 'object'}},
    {'name': 'delete_element', 'description': '删除当前页多余或错误的原生对象；本页候选事务内无需重复对象证据', 'parameters': {'slide_id': 'string', 'element_id': 'string', 'evidence': 'string?'}},
    {'name': 'duplicate_element', 'description': '复制当前页对象并指定新位置', 'parameters': {'slide_id': 'string', 'element_id': 'string', 'box': '[x,y,w,h]'}},
    {'name': 'reorder_element', 'description': '调整当前页对象图层顺序', 'parameters': {'slide_id': 'string', 'element_id': 'string', 'index': 'integer'}},
    {'name': 'batch_edit', 'description': '原子地编辑当前页多个对象；无需问题ID或置信度字段', 'parameters': {'slide_id': 'string', 'edits': '[{element_id,patch,evidence_issue_id?}]'}},
    {'name': 'align_elements', 'description': '按 left/center/right/top/middle/bottom 对齐', 'parameters': {'slide_id': 'string', 'element_ids': 'string[]', 'axis': 'string'}},
    {'name': 'distribute_elements', 'description': '按 x/y 均匀分布对象并保留首末对象', 'parameters': {'slide_id': 'string', 'element_ids': 'string[]', 'axis': 'x|y'}},
    {'name': 'undo_edit', 'description': '撤销最近一次工具编辑', 'parameters': {'slide_id': 'string'}},
]


def _valid_box(box):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError('box 必须是 [x,y,w,h]')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
        raise ValueError('box 必须包含有限数值')
    x, y, w, h = map(float, box)
    # PowerPoint permits off-canvas objects (for bleed, entrance/exit effects,
    # and intentional cropping). Keep the requested position; only normalize
    # reversed/zero dimensions so renderers receive a usable geometry.
    w, h = abs(w), abs(h)
    if w < 0.00001: w = 0.00001
    if h < 0.00001: h = 0.00001
    return [round(v, 7) for v in (x, y, w, h)]


def _element_index(slide, element_id):
    for i, element in enumerate(slide.get('elements', [])):
        if element.get('id') == element_id:
            return i
    raise ValueError(f'对象不存在：{element_id}')


def _locked(element, field):
    locks = element.get('layout_locks') or {}
    if field in ('x', 'y', 'w', 'h'):
        return bool(locks.get(field))
    if field in TEXT_STYLE or field in SHAPE_STYLE or field in IMAGE_STYLE:
        return bool(locks.get('style') or locks.get(field))
    return False


def _validate_style(kind, style):
    if not isinstance(style, dict):
        return {}
    allowed = TEXT_STYLE if kind in ('text', 'formula') else SHAPE_STYLE if kind in ('rect', 'ellipse', 'line') else IMAGE_STYLE if kind == 'image' else set()
    aliases = {'font_size':'size','font_family':'font','text_color':'color',
        'alignment':'align','verticalAlignment':'vertical_align','stroke_width':'strokeWidth'}
    out = {}
    for original_key, value in style.items():
        key = aliases.get(original_key, original_key)
        # Ignore properties this renderer does not understand, while still
        # applying every supported property in the same edit request.
        if key not in allowed: continue
        if key in ('font', 'eastAsianFont'):
            if isinstance(value, str) and value.strip(): out[key] = value.strip()[:240]
        elif key in ('color', 'fill', 'stroke'):
            if isinstance(value, str) and re.fullmatch(r'#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})', value):
                color=value.lstrip('#')
                if len(color)==3: color=''.join(char*2 for char in color)
                out[key] = color.upper()
        elif key in ('bold', 'italic', 'arrow'):
            if isinstance(value, bool): out[key] = value
        elif key in ('align', 'vertical_align'):
            choices = {'left', 'center', 'right'} if key == 'align' else {'top', 'middle', 'bottom'}
            if isinstance(value,str) and value.lower() in choices: out[key] = value.lower()
        elif key == 'fit':
            if isinstance(value,str) and value.lower() in {'stretch', 'contain', 'cover'}: out[key] = value.lower()
        else:
            if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value):
                # A non-positive font size cannot be represented in PPTX; use
                # its magnitude with a tiny visible minimum instead of losing
                # the entire object edit.
                number=abs(float(value)) if key=='size' else float(value)
                if key=='size': number=max(number,0.5)
                out[key] = number
    return out


def _visual_text_edit_proof(element, text, issue_id, issues):
    normalize=lambda value:re.sub(r'\s+','',str(value or '')).casefold()
    needle=normalize(text)
    if not needle or not issue_id:return False
    for issue in issues or []:
        if not isinstance(issue,dict) or issue.get('issue_id')!=issue_id:continue
        priority=str(issue.get('priority','')).upper()
        confidence=issue.get('confidence',0)
        ids=issue.get('element_ids') or []
        expected=normalize(issue.get('expected_result'))
        return (priority in {'P0','P1','0','1'} and
            isinstance(confidence,(int,float)) and not isinstance(confidence,bool) and confidence>=.8 and
            element.get('id') in ids and needle in expected)
    return False


def _visual_whitespace_edit_proof(element, old_text, new_text, issue_id, issues):
    compact=lambda value:re.sub(r'\s+','',str(value or ''))
    if not compact(old_text) or compact(old_text)!=compact(new_text) or not issue_id:return False
    for issue in issues or []:
        if not isinstance(issue,dict) or issue.get('issue_id')!=issue_id:continue
        ids=issue.get('element_ids') or []
        category=' '.join(str(issue.get(k,'')) for k in ('category','description','expected_result','evidence')).casefold()
        return (element.get('id') in ids and
            any(marker in category for marker in ('空格','间距','换行','断行','spacing','space','line break','wrap')))
    return False


def _visual_expected_text_correction_proof(element, patch, issue_id, issues):
    """Permit a narrowly scoped text correction copied verbatim from a linked issue.

    This covers spelling + spacing corrections where whitespace-only proof is
    insufficient. The model must cite the issue's exact quoted expected text,
    target the object named by that issue, and the issue region must overlap the
    object's source glyph box. No geometry or style changes are authorized.
    """
    if not isinstance(patch, dict) or set(patch) != {'text'} or not issue_id:
        return False
    new_text = patch.get('text')
    if not isinstance(new_text, str) or not new_text.strip():
        return False
    for issue in issues or []:
        if not isinstance(issue, dict) or issue.get('issue_id') != issue_id:
            continue
        confidence = issue.get('confidence', 0)
        if (element.get('id') not in (issue.get('element_ids') or []) or
                not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or confidence < .65):
            return False
        source = ' '.join(str(issue.get(key, '')) for key in ('category', 'description', 'evidence'))
        if not any(marker in source.casefold() for marker in
                   ('文字', '拼写', '空格', 'text', 'spelling', 'spacing')):
            return False
        expected = str(issue.get('expected_result') or '')
        quoted = re.findall(r'“([^”]+)”|「([^」]+)」|"([^"]+)"|\'([^\']+)\'', expected)
        targets = {next((part for part in group if part), '') for group in quoted}
        if new_text not in targets:
            return False
        region = issue.get('region')
        box = element.get('source_box') or element.get('source_box_norm') or element.get('ocr_box') or element.get('box')
        if not isinstance(region, (list, tuple)) or len(region) != 4 or not isinstance(box, (list, tuple)) or len(box) != 4:
            return False
        try:
            rx, ry, rw, rh = map(float, region)
            x, y, w, h = map(float, box)
        except (TypeError, ValueError):
            return False
        area = max(1e-9, w * h)
        overlap = max(0, min(x+w, rx+rw)-max(x, rx)) * max(0, min(y+h, ry+rh)-max(y, ry))
        return overlap / area >= .5
    return False


def _visual_issue_edit_proof(element, issue_id, issues):
    """Require restore-mode edits to target an object named by a real issue."""
    if not issue_id:
        return False
    for issue in issues or []:
        if not isinstance(issue,dict) or issue.get('issue_id')!=issue_id:
            continue
        priority=str(issue.get('priority','')).upper()
        return (priority in {'P0','P1','P2','P3','0','1','2','3'} and
            element.get('id') in (issue.get('element_ids') or []))
    return False


def _apply_patch(element, patch, mode, *, source_regions=None, evidence_issue_id=None, visual_issues=None):
    if not isinstance(patch, dict) or not patch:
        raise ValueError('patch 必须是非空对象')
    patch = dict(patch)
    # Models sometimes echo the object's immutable kind beside a valid text
    # correction. Treat an identical kind as redundant metadata; changing the
    # type still requires an explicit delete/create operation.
    if 'kind' in patch:
        if patch['kind'] != element.get('kind'):
            raise ValueError('对象 kind 不可通过 edit_element 修改；请保留现有类型，只修改 text、box、style 或 asset。')
        patch.pop('kind')
        if not patch:
            raise ValueError('patch 除重复的 kind 外没有可修改字段。')
    patch = {key:value for key,value in patch.items() if key in ELEMENT_FIELDS}
    if not patch:
        raise ValueError('patch 不包含可应用的文字、几何、样式或素材属性。')
    if 'text' in patch:
        if element.get('kind') not in ('text', 'formula'):
            raise ValueError('只有文字或公式对象可以修改 text')
        value = patch['text']
        if not isinstance(value, str) or len(value) > 12000:
            raise ValueError('text 最多 12000 字符')
        value=value.replace('\r\n','\n').replace('\r','\n').replace('\u2028','\n').replace('\u2029','\n')
        value=''.join('\n' if c in '\v\f\u0085' else c for c in value)
        # User-authorized restore edits run only against an isolated current-page
        # candidate. OCR provenance, issue confidence, and layout locks do not
        # veto DeepSeek's valid visual corrections in that transaction.
        element['text'] = value
    if 'box' in patch:
        box = _valid_box(patch['box'])
        element['box'] = box
    if 'style' in patch:
        style = _validate_style(element.get('kind'), patch['style'])
        element.setdefault('style', {}).update(style)
    if 'asset' in patch:
        value = patch['asset']
        if element.get('kind') != 'image' or not isinstance(value, str) or not core.FILENAME_RE.fullmatch(value):
            raise ValueError('图片素材引用不受支持')
        element['asset'] = value


def _distribute_full_line_edit(slide, element_id, text):
    """Apply a full-line correction across the OCR runs that form that line.

    The model sometimes targets the first OCR run but supplies the corrected
    *whole line*. Writing that phrase into the first run leaves the other runs
    in place, so a later layout pass renders the phrase twice. When the new
    phrase contains every sibling run and its script segmentation matches the
    existing run count, distribute the new text over those existing runs.
    Fragment-only edits keep their original behavior.
    """
    from .text_layout import split_runs

    elements = slide.get('elements', [])
    target = next((row for row in elements if row.get('id') == element_id), None)
    if not target or not target.get('line_id') or not target.get('run_id'):
        return False
    peers = [row for row in elements if row.get('line_id') == target.get('line_id')
             and row.get('run_id') and row.get('kind') in ('text', 'formula')]
    peers.sort(key=lambda row: (row.get('run_order', 0), row.get('box', [0])[0]))
    if len(peers) < 2 or target not in peers:
        return False
    compact = lambda value: re.sub(r'\s+', '', str(value or ''))
    normalized = compact(text)
    siblings = [row for row in peers if row is not target and compact(row.get('text'))]
    if not normalized or not siblings or not all(compact(row.get('text')) in normalized for row in siblings):
        return False
    try:
        pieces = split_runs(text)
    except ValueError:
        return False
    if len(pieces) != len(peers):
        return False
    for row, piece in zip(peers, pieces):
        row['text'] = piece['text']
        row['text_class'] = piece['text_class']
        row['char_start'] = piece['char_start']
        row['char_end'] = piece['char_end']
        row['whitespace'] = piece['whitespace']
        row['text_layout'] = {}
    return True


def _matches_current_line(slide, element_id, text):
    target=next((row for row in slide.get('elements',[]) if row.get('id')==element_id),None)
    if not target or not target.get('line_id') or not target.get('run_id'):
        return False
    peers=[row for row in slide.get('elements',[]) if row.get('line_id')==target.get('line_id')
        and row.get('run_id') and row.get('kind') in ('text','formula')]
    if len(peers)<2:
        return False
    peers.sort(key=lambda row:(row.get('run_order',0),row.get('box',[0])[0]))
    return ''.join(str(row.get('text') or '') for row in peers)==text


def _relayout_after_agent_edit(slide, project, previous_elements, explicit_boxes=None):
    """Refresh the affected scene graph's unified typography/line geometry.

    Geometry explicitly included with the same model patch is temporarily
    locked while the engine recalculates sibling runs, then the original lock
    metadata is restored. This avoids stale text_layout data swallowing edits.
    """
    from .text_layout import input_signature, layout_elements
    explicit_boxes=explicit_boxes or {}
    previous={e.get('id'):e for e in previous_elements}
    for element in slide.get('elements',[]):
        fields=explicit_boxes.get(element.get('id'))
        if not fields:continue
        original=element.get('layout_locks') or {}
        element['layout_locks']={**original,**{field:True for field in fields}}
    laid_out,report=layout_elements(slide.get('elements',[]),project.get('design'),slide.get('id','S001'),previous_elements)
    for element in laid_out:
        old=previous.get(element.get('id'))
        if old is not None:
            element['layout_locks']=deepcopy(old.get('layout_locks',{}))
        element.get('text_layout',{})['input_signature']=input_signature(element)
    slide['elements']=laid_out
    slide['text_structure']=report


def _patch_needs_text_layout(element, patch):
    if 'text' in patch and patch['text']!=element.get('text'):
        return True
    style=patch.get('style') or {}
    return bool(set(style)&{'font','eastAsianFont','size','bold','italic','char_spacing_pt'})


def _patch_is_noop(element, patch):
    if not isinstance(patch,dict):
        return False
    if 'kind' in patch and patch['kind'] != element.get('kind'):
        return False
    if 'text' in patch and patch['text']!=element.get('text'):
        return False
    if 'box' in patch and list(patch['box'])!=list(element.get('box') or []):
        return False
    if 'asset' in patch and patch['asset']!=element.get('asset'):
        return False
    current=element.get('style') or {}
    if any(current.get(key)!=value for key,value in (patch.get('style') or {}).items()):
        return False
    return bool(set(patch)-{'kind'})


def _trusted_source(text, box, source_regions):
    normalized = lambda value: re.sub(r'\s+', '', str(value or '')).casefold()
    for row in source_regions or []:
        if normalized(row.get('source_text') or row.get('text')) != normalized(text):
            continue
        candidate = row.get('box') or row.get('source_box')
        if not isinstance(candidate, (list, tuple)) or len(candidate) != 4:
            continue
        if max(abs(float(a) - float(b)) for a, b in zip(candidate, box)) <= .015:
            return True
    return False


def _matching_source_box(text, source_regions):
    """Return a unique OCR/Design box for verbatim text, when one exists.

    Missing text is anchored to the source image's measured region instead of
    trusting a model's rough placement estimate. If the same exact text occurs
    more than once, callers must resolve the location from visual evidence.
    """
    normalized = lambda value: re.sub(r'\s+', '', str(value or '')).casefold()
    needle = normalized(text)
    if not needle:
        return None
    matches = []
    for row in source_regions or []:
        if normalized(row.get('source_text') or row.get('text')) != needle:
            continue
        box = row.get('source_box') or row.get('box')
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            matches.append(_valid_box(box))
        except ValueError:
            continue
    distinct = []
    for box in matches:
        if not any(max(abs(a-b) for a,b in zip(box, prior)) <= .005 for prior in distinct):
            distinct.append(box)
    return distinct[0] if len(distinct) == 1 else None


def _has_same_text_at_visual_location(slide, text, box, *, threshold=0.78):
    """Detect a likely duplicate native text object before creating another.

    In restore mode, a vision model can mistake a low-contrast or small existing
    label for missing content. If identical text already occupies essentially
    the same location, the safe action is to edit that object (for example its
    color), not stack a second glyph over it.
    """
    normalize=lambda value:re.sub(r'\s+','',str(value or '')).casefold()
    needle=normalize(text)
    if not needle:
        return None
    x,y,w,h=map(float,box)
    area=max(1e-9,w*h)
    for existing in slide.get('elements',[]):
        if existing.get('kind') not in {'text','formula'} or normalize(existing.get('text'))!=needle:
            continue
        current=existing.get('box')
        if not isinstance(current,(list,tuple)) or len(current)!=4:
            continue
        ex,ey,ew,eh=map(float,current)
        intersection=max(0,min(x+w,ex+ew)-max(x,ex))*max(0,min(y+h,ey+eh)-max(y,ey))
        smaller=max(1e-9,min(area,ew*eh))
        if intersection/smaller>=threshold:
            return existing.get('id')
    return None


def _visual_source_proof(text, box, issue_id, issues):
    """Accept only verbatim text inside a high-confidence, model-viewed Design issue."""
    normalize=lambda value:re.sub(r'\s+','',str(value or '')).casefold()
    needle=normalize(text)
    if not needle or not issue_id:return False
    for issue in issues or []:
        if not isinstance(issue,dict) or issue.get('issue_id')!=issue_id:continue
        priority=str(issue.get('priority','')).upper()
        category=str(issue.get('category','')).casefold()
        confidence=issue.get('confidence',0)
        if priority not in {'P0','P1','0','1'} or not isinstance(confidence,(int,float)) or isinstance(confidence,bool) or confidence<.85:
            return False
        if not any(marker in category for marker in ('缺失','missing','content','内容')):return False
        evidence=' '.join(str(issue.get(key,'')) for key in ('description','expected_result','evidence'))
        if needle not in normalize(evidence):return False
        region=issue.get('region')
        if not isinstance(region,(list,tuple)) or len(region)!=4:return False
        rx,ry,rw,rh=map(float,region);x,y,w,h=map(float,box)
        ix=max(0,min(x+w,rx+rw)-max(x,rx));iy=max(0,min(y+h,ry+rh)-max(y,ry))
        return ix*iy/max(1e-9,w*h)>=.8
    return False


def execute_tool_calls(project, slide_id, calls, *, mode='restore', source_regions=None, visual_issues=None,
                       screenshots=None, history=None):
    """Execute a bounded JSON tool batch on a copy; each call is atomic."""
    if mode not in {'restore', 'free'}:
        raise ValueError('mode 必须是 restore 或 free')
    candidate = deepcopy(project)
    slide = core.find_slide(candidate, slide_id)
    history = history if history is not None else []
    events = []
    if not isinstance(calls, list) or len(calls) > 1000:
        raise ValueError('每轮最多 1000 个工具调用')

    def execute(name, args):
        if not isinstance(args, dict): raise ValueError('工具参数必须是对象')
        if name == 'get_page_state':
            return {'slide_id': slide_id, 'objects': deepcopy(slide.get('elements', [])), 'dimensions': [1, 1]}
        if name == 'get_element':
            i = _element_index(slide, args.get('element_id')); return deepcopy(slide['elements'][i])
        if name == 'get_screenshot':
            return {'available': bool((screenshots or {}).get('current')), 'screenshot': (screenshots or {}).get('current')}
        if name == 'get_region_screenshot':
            box = _valid_box(args.get('region')); return {'region': box, 'screenshot': (screenshots or {}).get('region')}
        if name == 'undo_edit':
            if not history: raise ValueError('没有可撤销的工具编辑')
            slide['elements'] = history.pop()
            return {'restored': True, 'object_count': len(slide['elements'])}
        if name == 'batch_edit':
            edits = args.get('edits')
            if not isinstance(edits, list) or not edits or len(edits) > 100: raise ValueError('edits 必须是 1–100 项')
            snapshot = deepcopy(slide['elements'])
            explicit_boxes={};needs_layout=False
            try:
                for edit in edits:
                    i = _element_index(slide, edit.get('element_id'))
                    patch=edit.get('patch')
                    if (_patch_is_noop(slide['elements'][i],patch) or
                            isinstance(patch,dict) and isinstance(patch.get('text'),str) and
                            _matches_current_line(slide,edit.get('element_id'),patch['text'])):
                        continue
                    needs_layout=needs_layout or _patch_needs_text_layout(slide['elements'][i],patch or {})
                    if isinstance(patch,dict) and 'box' in patch:explicit_boxes[edit.get('element_id')]=('x','y','w','h')
                    _apply_patch(slide['elements'][i], patch, mode,
                        source_regions=source_regions,evidence_issue_id=edit.get('evidence_issue_id'),visual_issues=visual_issues)
                    if isinstance(patch,dict) and isinstance(patch.get('text'),str):
                        _distribute_full_line_edit(slide,edit.get('element_id'),patch['text'])
                if needs_layout:_relayout_after_agent_edit(slide,candidate,snapshot,explicit_boxes)
            except Exception:
                slide['elements'] = snapshot
                raise
            return {'edited': [e['element_id'] for e in edits]}
        if name == 'edit_element':
            i = _element_index(slide, args.get('element_id'))
            snapshot=deepcopy(slide['elements']);patch=args.get('patch')
            if (_patch_is_noop(slide['elements'][i],patch) or
                    isinstance(patch,dict) and isinstance(patch.get('text'),str) and
                    _matches_current_line(slide,args['element_id'],patch['text'])):
                result={'element_id':args['element_id'],'element':deepcopy(slide['elements'][i]),'no_change':True}
                if isinstance(patch,dict) and isinstance(patch.get('text'),str):
                    result['current_line_text']=''.join(row.get('text','') for row in sorted(
                        (row for row in slide['elements'] if row.get('line_id')==slide['elements'][i].get('line_id')),
                        key=lambda row:(row.get('run_order',0),row.get('box',[0])[0])))
                return result
            needs_layout=_patch_needs_text_layout(slide['elements'][i],patch or {})
            _apply_patch(slide['elements'][i], args.get('patch'), mode,
                source_regions=source_regions,evidence_issue_id=args.get('evidence_issue_id'),visual_issues=visual_issues)
            distributed_line=False
            if isinstance(patch,dict) and isinstance(patch.get('text'),str):
                distributed_line=_distribute_full_line_edit(slide,args['element_id'],patch['text'])
            if needs_layout:
                explicit={args['element_id']:('x','y','w','h')} if isinstance(patch,dict) and 'box' in patch else {}
                _relayout_after_agent_edit(slide,candidate,snapshot,explicit)
            idx=_element_index(slide,args['element_id'])
            return {'element_id': args['element_id'], 'element': deepcopy(slide['elements'][idx]),
                'full_line_edit_distributed':distributed_line}
        if name == 'create_element':
            raw = args.get('element')
            if not isinstance(raw, dict): raise ValueError('element 必须是对象')
            kind = raw.get('kind')
            if kind not in {'text', 'formula', 'rect', 'ellipse', 'line', 'image'}: raise ValueError('unsupported element kind')
            element = deepcopy(raw); element['id'] = 'agent_' + uuid.uuid4().hex[:16]
            requested_box = _valid_box(element.get('box'))
            element['box'] = requested_box
            element['style'] = _validate_style(kind, element.get('style', {}))
            if kind in {'text', 'formula'}:
                text = element.get('text')
                if not isinstance(text, str) or len(text) > 12000:
                    raise ValueError('新增文本最多 12000 字符')
                text=text.replace('\r\n','\n').replace('\r','\n').replace('\u2028','\n').replace('\u2029','\n')
                text=''.join('\n' if c in '\v\f\u0085' else c for c in text)
                element['text']=text
                element['source_text'] = text
            if kind == 'image':
                asset = element.get('asset')
                if not isinstance(asset, str) or not core.FILENAME_RE.fullmatch(asset) or not (core.project_dir(project['id'])/'images'/asset).is_file():
                    raise ValueError('新增图片必须引用当前项目已有素材')
            slide['elements'].append(element)
            result = {'element_id': element['id'], 'element': deepcopy(element)}
            return result
        if name == 'delete_element':
            i = _element_index(slide, args.get('element_id')); element = slide['elements'][i]
            return slide['elements'].pop(i)
        if name == 'duplicate_element':
            i = _element_index(slide, args.get('element_id')); element = deepcopy(slide['elements'][i])
            element['id'] = 'agent_' + uuid.uuid4().hex[:16]; element['box'] = _valid_box(args.get('box'))
            slide['elements'].insert(i+1,element)
            return {'element_id': element['id'], 'element': deepcopy(element)}
        if name == 'reorder_element':
            i = _element_index(slide, args.get('element_id')); target = args.get('index')
            if isinstance(target,bool) or not isinstance(target,int) or not 0 <= target < len(slide['elements']): raise ValueError('index 越界')
            element = slide['elements'].pop(i); slide['elements'].insert(target,element)
            return {'element_id': element['id'], 'index': target}
        if name in {'align_elements', 'distribute_elements'}:
            ids = args.get('element_ids'); axis = args.get('axis')
            if not isinstance(ids,list) or len(ids)<2 or len(set(ids)) != len(ids): raise ValueError('至少指定两个不同对象')
            if axis not in ({'left','center','right','top','middle','bottom'} if name=='align_elements' else {'x','y'}): raise ValueError('axis 不受支持')
            selected = [slide['elements'][_element_index(slide,eid)] for eid in ids]
            if name == 'align_elements':
                index={'left':0,'center':0,'right':0,'top':1,'middle':1,'bottom':1}[axis]
                ext={'left':0,'center':2,'right':2,'top':0,'middle':3,'bottom':3}[axis]
                target=(min(e['box'][index] for e in selected) if axis in {'left','top'} else
                        max(e['box'][index]+e['box'][ext] for e in selected) if axis in {'right','bottom'} else
                        sum(e['box'][index]+e['box'][ext]/2 for e in selected)/len(selected))
                for e in selected:
                    box=list(e['box']);box[index]=target if axis in {'left','top'} else target-box[ext] if axis in {'right','bottom'} else target-box[ext]/2
                    new=_valid_box(box)
                    e['box']=new
            else:
                idx=0 if axis=='x' else 1; extent=2 if axis=='x' else 3
                selected.sort(key=lambda e:e['box'][idx]);first=selected[0]['box'][idx]
                last=selected[-1]['box'][idx]+selected[-1]['box'][extent]
                gap=(last-first-sum(e['box'][extent] for e in selected))/(len(selected)-1)
                cursor=first
                for e in selected:
                    box=list(e['box']);box[idx]=cursor;new=_valid_box(box)
                    e['box']=new;cursor+=box[extent]+gap
            return {'element_ids': ids, 'axis': axis}
        raise ValueError(f'unknown tool: {name}')

    for call in calls:
        name = call.get('tool') if isinstance(call, dict) else None
        args = call.get('arguments', {}) if isinstance(call, dict) else {}
        if name not in TOOL_NAMES:
            events.append({'tool': str(name), 'status': 'unsupported', 'error': '工具名不受支持'}); continue
        before = deepcopy(slide.get('elements', []))
        try:
            result = execute(name, args)
            changed=slide.get('elements',[])!=before
            if changed and name!='undo_edit':
                history.append(before)
            read_only={'get_page_state','get_element','get_screenshot','get_region_screenshot'}
            outcome='observed' if name in read_only else ('applied' if changed else 'no_change')
            events.append({'tool': name, 'status': outcome, 'result': result})
        except Exception as exc:
            slide['elements'] = before
            event={'tool': name, 'status': 'rejected', 'error': str(exc)[:300]}
            duplicate=re.search(r'同一位置已存在相同文字对象 ([A-Za-z0-9_:-]+)',str(exc))
            if duplicate:
                existing=next((e for e in slide.get('elements',[]) if e.get('id')==duplicate.group(1)),None)
                if existing is not None:
                    event['result']={'duplicate_element':deepcopy(existing)}
            events.append(event)
    return candidate, events, history
