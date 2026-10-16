"""Authoritative text layout in slide points; OCR geometry is evidence, not advance.

This module is used when generating or editing a candidate. Export only consumes
its saved output. No renderer is allowed to fit fonts or regroup the scene.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import re
import unicodedata
from .fonts import measure_text, default_cjk_font

WIDTH_PT, HEIGHT_PT = 960.0, 540.0
VERSION = 2
ENGINE_REVISION = 'v2.1-harfbuzz'
BREAKS = re.compile(r'\r\n|[\r\n\v\f\u0085\u2028\u2029]')
OPERAND = r'(?:[A-Za-z]{1,2}|\d+(?:\.\d+)?)(?:\s*[_^]\s*(?:[A-Za-z]{1,2}|\d+))?'
FORMULA = re.compile(
    r'\\(?:frac|sum|int|sqrt|alpha|beta|gamma|theta|pi|sigma)\b(?:\s*\{[^{}]*\}){0,3}|'
    r'\\\([^\n]+?\\\)|\\\[[^\n]+?\\\]|\$[^$\n]+\$|'
    r'(?<![\w-])[A-Za-z]{1,2}\s*[_^]\s*(?:[A-Za-z]{1,2}|\d+)(?![\w-])|'
    r'(?<![\w-])' + OPERAND + r'(?:\s*[=+\-*/×÷≤≥<>]\s*' + OPERAND + r')+(?![\w-])')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def input_signature(element):
    return digest({k: element.get(k) for k in ('text', 'box', 'style', 'layout_locks', 'kind')})


def text_class(char):
    code = ord(char)
    if (0x3400 <= code <= 0x9fff or 0xf900 <= code <= 0xfaff or
        0x3040 <= code <= 0x30ff or 0xac00 <= code <= 0xd7af):
        return 'cjk'
    if char.isalpha() or char.isdigit():
        return 'latin'
    # Full-width punctuation is measured in the CJK face; preserve the glyph.
    if 0x3000 <= code <= 0x303f or 0xff00 <= code <= 0xffef:
        return 'cjk'
    if char in '∑∏√∫=^_×÷≤≥≠±':
        return 'formula'
    return None


def split_runs(text):
    """Lossless Python character ranges, including every original whitespace."""
    value = str(text or '')
    if BREAKS.search(value):
        raise ValueError('split_runs expects one visual line')
    if not value.strip():
        return []
    classes = [text_class(c) for c in value]
    for match in FORMULA.finditer(value):
        span = match.group()
        operators = re.findall(r'[=+\-*/×÷≤≥<>]', span)
        operands = [part.strip() for part in re.split(r'[=+\-*/×÷≤≥<>]', span) if part.strip()]
        variable = len(operators) == 1 and operators[0] in '+-' and (
            re.search(r'\s[+-]\s', span) or all(re.fullmatch(r'[A-Za-z]{1,2}', p) for p in operands))
        if not re.fullmatch(r'\d+\s*/\s*\d+', span) and (
            span.startswith(('\\', '$')) or re.search(r'[=^_*/×÷≤≥<>]', span) or len(operators) >= 2 or variable):
            classes[match.start():match.end()] = ['formula']*(match.end()-match.start())
    opening = set('([{“‘')
    for i, kind in enumerate(classes):
        if kind:
            continue
        before = next((classes[j] for j in range(i-1, -1, -1) if classes[j]), None)
        after = next((classes[j] for j in range(i+1, len(classes)) if classes[j]), None)
        classes[i] = (after if value[i] in opening else before) or after or 'latin'
    result = []
    start = 0
    for i in range(1, len(value)+1):
        if i == len(value) or classes[i] != classes[start]:
            piece = value[start:i]
            result.append({'text': piece, 'text_class': classes[start], 'char_start': start,
                           'char_end': i, 'whitespace': [{'offset': n+start, 'char': c}
                                                        for n, c in enumerate(piece) if c.isspace()]})
            start = i
    assert ''.join(run['text'] for run in result) == value
    return result


def split_regions(regions):
    """Structure raw OCR rows without inventing final geometry or losing text."""
    output = []
    for index, region in enumerate(regions):
        raw = str(region.get('text') or '')
        source_id = str(region.get('id') or f'text_{index+1:03d}')
        box = list(region.get('ocr_box') or region.get('box') or [0, 0, 1, 1])
        lines = BREAKS.split(raw)
        for number, line in enumerate(lines):
            if not line.strip():
                continue
            source_box = [box[0], box[1]+box[3]*number/len(lines), box[2], box[3]/len(lines)]
            line_id = region.get('line_id') if len(lines) == 1 else None
            line_id = line_id or 'line:' + digest([source_id, number, line, source_box])[:20]
            pieces = split_runs(line)
            for n, piece in enumerate(pieces):
                # This geometry is deliberately marked as source evidence.
                # The layout engine will compute all advances from fonts.
                output.append({**deepcopy(region), **piece, 'id': source_id if len(pieces) == 1 and len(lines) == 1
                               else f'{source_id}.l{number+1}.r{n+1}',
                               'block_id': region.get('block_id') or 'block:'+source_id,
                               'line_id': line_id, 'run_id': line_id+f':run:{piece["char_start"]}',
                               'run_order': n, 'text_unit': 'line', 'source_text': line,
                               'source_box': list(source_box), 'ocr_box': list(source_box),
                               'source_group': line_id, 'box': list(source_box)})
    return output


def _legacy_lines(elements):
    """Only join fragments that share an exact source and a nearby visual row.

    Repeated independent labels remain independent even if their text matches.
    Existing full-source whitespace can be recovered; absent evidence is flagged.
    """
    rows = []
    for element in elements:
        source = str(element.get('source_text') or element.get('text') or '')
        b = element.get('ocr_box') or element.get('source_box') or element['box']
        center = b[1]+b[3]/2
        fragment = source != str(element.get('text') or '')
        row = next((r for r in rows if fragment and r['fragment'] and r['source'] == source
                    and abs(r['center']-center) <= min(r['box'][3], b[3])*.45
                    and element['box'][0] <= r['right']+.04), None)
        if row is None:
            row = {'source': source, 'fragment': fragment, 'center': center,
                   'box': list(b), 'right': element['box'][0]+element['box'][2], 'elements': []}
            rows.append(row)
        row['elements'].append(element)
        row['right'] = max(row['right'], element['box'][0]+element['box'][2])
        boxes = [e.get('ocr_box') or e.get('source_box') or e['box'] for e in row['elements']]
        x = min(b[0] for b in boxes); y = min(b[1] for b in boxes)
        row['box'] = [x, y, max(b[0]+b[2] for b in boxes)-x, max(b[1]+b[3] for b in boxes)-y]
    return rows


def structure_elements(elements, slide_id='S001'):
    structured = []
    pending = []
    for e in deepcopy(elements):
        if e.get('kind') not in ('text', 'formula') or not str(e.get('text') or '').strip():
            structured.append(e)
        elif e.get('line_id') and e.get('run_id') and not BREAKS.search(str(e['text'])):
            pieces=split_runs(e['text']) if e['kind']=='text' else []
            if len(pieces)<=1:
                if pieces:e.update(text_class=pieces[0]['text_class'])
                structured.append(e)
            else:
                for n,piece in enumerate(pieces):
                    structured.append({**deepcopy(e),**piece,'id':e['id'] if n==0 else f'{e["id"]}.r{n+1}',
                        'run_id':e['run_id'] if n==0 else f'{e["run_id"]}:split:{n}',
                        'run_order':e.get('run_order',0)+n/100,'text_layout':{}})
        else:
            pending.append(e)
    for row in _legacy_lines(pending):
        items = sorted(row['elements'], key=lambda e: e['box'][0])
        raw = row['source']
        # Do not restore source text after a deliberate user edit or for a
        # partial selection that omitted siblings; use the actual selected text.
        joined = ''.join(str(e.get('text') or '') for e in items)
        key = lambda v: ''.join(unicodedata.normalize('NFKC', v).split())
        recoverable = key(joined) == key(raw)
        if not recoverable:
            for e in items:
                pieces = split_regions([{**e, 'source_text': e['text']}])
                for p in pieces:
                    p['kind'] = 'formula' if e['kind'] == 'formula' else 'text'
                    p['layout_managed'] = bool(e.get('source_text') or len(pieces)>1)
                    p['migration_warning'] = 'source_text_mismatch_or_partial_selection'
                    structured.append(p)
            continue
        line_id = 'line:'+digest([slide_id, [e['id'] for e in items], raw, row['box']])[:20]
        if BREAKS.search(raw):
            pieces = split_regions([{**items[0], 'text': raw, 'box': row['box']}])
        else:
            pieces = split_runs(raw) if items[0]['kind'] != 'formula' else [
                {'text': raw, 'text_class': 'formula', 'char_start': 0, 'char_end': len(raw), 'whitespace': []}]
        ids_used = set()
        for n, piece in enumerate(pieces):
            parent = next((e for e in items if str(e['text']).strip() == piece['text'].strip() and e['id'] not in ids_used), items[min(n, len(items)-1)])
            eid = parent['id'] if parent['id'] not in ids_used else f'{parent["id"]}.r{n+1}'
            ids_used.add(eid)
            structured.append({**deepcopy(parent), **piece, 'id': eid,
                'block_id': parent.get('block_id') or 'block:'+items[0]['id'],
                'logical_id':parent.get('logical_id') or items[0]['id'],
                'block_box':list(parent.get('block_box') or row['box']),
                'line_id': piece.get('line_id') or line_id,
                'run_id': piece.get('run_id') or line_id+f':run:{piece["char_start"]}',
                'run_order': n, 'source_text': piece.get('source_text', raw),
                'source_box': list(piece.get('source_box') or row['box']),
                'ocr_box': list(piece.get('ocr_box') or row['box']),
                'source_group': piece.get('line_id') or line_id,
                'layout_managed': bool(parent.get('source_text') or len(items)>1 or len(pieces)>1)})
    # Preserve original z order, with split runs next to their parent.
    order = {e['id']: n for n, e in enumerate(elements)}
    structured.sort(key=lambda e: (order.get(e['id'], next((n for id_, n in order.items() if e['id'].startswith(id_+'.')), len(order))), e.get('run_order', 0)))
    return structured


def _measure_run(e, theme):
    style = e.setdefault('style', {})
    cls = e.get('text_class', 'latin')
    size = float(style.get('size', 18))
    latin = style.get('font') or theme.get('font') or 'Noto Sans'
    family = style.get('eastAsianFont') or latin if cls == 'cjk' else latin
    if cls == 'formula' and e.get('kind') == 'formula':
        from .equations import measure_formula
        return measure_formula(str(e['text']), size)
    metrics = measure_text(str(e['text']), size, family, cls, style.get('bold', False),
                           style.get('italic', False), style.get('char_spacing_pt', 0))
    if cls=='cjk' and metrics['missing_characters'] and not e.get('layout_locks',{}).get('font'):
        metrics=measure_text(str(e['text']),size,default_cjk_font(),cls,style.get('bold',False),style.get('italic',False),style.get('char_spacing_pt',0))
        metrics['font'].update(requested_family=family,fallback=True,fallback_reason='requested_face_lacks_CJK_glyphs')
    actual = metrics['font']['actual_family']
    if not e.get('layout_locks', {}).get('font'):
        if cls == 'cjk':
            style['eastAsianFont'] = actual
            style['font'] = actual
        else:
            style['font'] = actual
    return metrics


def layout_elements(elements, theme=None, slide_id='S001', previous=None, line_constraints=None):
    theme = theme or {}
    result = structure_elements(elements, slide_id)
    prior = {e['id']: e for e in previous or []}
    groups = {}
    for e in result:
        if e.get('kind') in ('text', 'formula') and str(e.get('text') or '').strip():
            groups.setdefault(e['line_id'], []).append(e)
    reports = []
    for line_id, runs in groups.items():
        runs.sort(key=lambda e: (e.get('run_order', 0), e['box'][0]))
        # A saved, unchanged line is immutable even across font/renderer changes.
        if all(e.get('text_layout', {}).get('version') == VERSION and
               e['text_layout'].get('engine_revision') == ENGINE_REVISION and
               e['text_layout'].get('input_signature') == input_signature(e) and
               e['text_layout'].get('id') == e['id'] for e in runs):
            reports.append({'line_id': line_id, 'runs': [e['text_layout'] for e in runs], 'unchanged': True})
            continue
        text=''.join(e['text'] for e in runs)
        offset=0
        for e in runs:
            e.update(char_start=offset,char_end=offset+len(e['text']),source_text=text,
                     whitespace=[{'offset':offset+n,'char':c} for n,c in enumerate(e['text']) if c.isspace()])
            offset+=len(e['text'])
            if e.get('text_class')=='formula':e['kind']='formula'
        measurements = [_measure_run(e, theme) for e in runs]
        box = list(runs[0].get('ocr_box') or runs[0].get('source_box') or runs[0]['box'])
        managed = any(e.get('layout_managed', True) for e in runs)
        # OCR glyph-height estimates can be a little generous (font fallback,
        # anti-aliasing, or a loose OCR box). Preserve the measured size when it
        # fits; otherwise scale the whole visual line down only as much as
        # needed to stay within the original line width. This is preferable to
        # exporting oversized runs that spill into neighboring content.
        source_width = box[2]*WIDTH_PT
        line_warnings = []
        total = sum(m['advance_pt'] for m in measurements)
        margin_width = sum(float(e.get('style', {}).get('margin_left_pt', 0))+
                           float(e.get('style', {}).get('margin_right_pt', 0)) for e in runs)
        locked_size = any((e.get('layout_locks') or {}).get('size') or
                          (e.get('layout_locks') or {}).get('style') for e in runs)
        available_width = max(0.0, source_width-margin_width)
        if managed and not locked_size and total > available_width+.25:
            scale = max(.5, min(1.0, (available_width/max(total,.01))*.985))
            for e in runs:
                style=e.setdefault('style',{})
                if isinstance(style.get('size'),(int,float)):
                    style['size']=round(max(6.0,float(style['size'])*scale),1)
            measurements = [_measure_run(e, theme) for e in runs]
            line_warnings.append('source_width_scaled_to_fit')
        # Align visible ink to source glyph bottom, then share this baseline.
        baseline = (box[1]+box[3])*HEIGHT_PT - max(m['ink_box_pt'][3] for m in measurements)
        cursor = box[0]*WIDTH_PT
        changed = [(e, prior.get(e['id'])) for e in runs if prior.get(e['id']) and e['box'] != prior[e['id']]['box']]
        if changed:
            e, old = changed[0]; idx = runs.index(e)
            old_metrics = old.get('text_layout', {})
            baseline = old_metrics.get('baseline_pt', baseline)+(e['box'][1]-old['box'][1])*HEIGHT_PT
            cursor += (e['box'][0]-old['box'][0])*WIDTH_PT
        constraint=(line_constraints or {}).get(line_id,{})
        baseline=constraint.get('baseline_pt',baseline);cursor=constraint.get('cursor_pt',cursor)
        baseline = max(max(m['ascent_pt'] for m in measurements), baseline)
        total = sum(m['advance_pt'] for m in measurements)
        if cursor+total > WIDTH_PT+.01:
            line_warnings.append('layout_conflict:line_exceeds_slide')
        if total > available_width+.5:
            line_warnings.append('source_width_exceeded:minimum_size_reached')
        line_records = []
        # WPS can wrap a nearly exact-width frame despite wrap="none". Give
        # the frame the available visual-row slot, without adding that empty
        # capacity to the cursor advance. This is geometry from neighboring
        # source rows, not a guessed font multiplier or font shrink.
        row_right=WIDTH_PT
        for other_id,other_runs in groups.items():
            if other_id==line_id:continue
            ob=other_runs[0].get('ocr_box') or other_runs[0].get('source_box') or other_runs[0]['box']
            overlap=max(0,min(box[1]+box[3],ob[1]+ob[3])-max(box[1],ob[1]))
            if overlap>=min(box[3],ob[3])*.25 and ob[0]*WIDTH_PT>cursor+.01:
                row_right=min(row_right,ob[0]*WIDTH_PT)
        for e, metrics in zip(runs, measurements):
            st = e.setdefault('style', {})
            locks = e.get('layout_locks') or {}
            margin_x = float(st.get('margin_left_pt', 0))+float(st.get('margin_right_pt', 0))
            margin_y = float(st.get('margin_top_pt', 0))+float(st.get('margin_bottom_pt', 0))
            # Absolute 0.5pt allowance covers subpixel frame rounding; it is
            # never added to the cursor advance and never changes font size.
            measured_width = max(metrics['advance_pt'], metrics['ink_box_pt'][2])+margin_x+.5
            width=max(measured_width,row_right-cursor) if managed else measured_width
            height = metrics['line_height_pt']+margin_y
            y = baseline-metrics['ascent_pt']-float(st.get('margin_top_pt', 0))
            proposed = [cursor/WIDTH_PT, y/HEIGHT_PT, width/WIDTH_PT, height/HEIGHT_PT]
            if managed:
                e['box'] = [round(e['box'][i] if locks.get(k) else proposed[i], 9) for i, k in enumerate(('x', 'y', 'w', 'h'))]
                for key,value in [('vertical_align','top'),('line_spacing',1.0),('align','left')]:
                    if not locks.get(key):st[key]=value
            b = e['box']
            actual_baseline = b[1]*HEIGHT_PT+float(st.get('margin_top_pt', 0))+metrics['ascent_pt']
            free_height=b[3]*HEIGHT_PT-margin_y-metrics['line_height_pt']
            actual_baseline+=free_height*{'top':0,'middle':.5,'bottom':1}.get(st.get('vertical_align','top'),0)
            warnings = list(line_warnings)
            overflow = metrics['advance_pt'] > b[2]*WIDTH_PT-margin_x+.01
            clipping=(actual_baseline+metrics['ink_box_pt'][1]<b[1]*HEIGHT_PT+float(st.get('margin_top_pt',0))-.01 or
                      actual_baseline+metrics['ink_box_pt'][3]>(b[1]+b[3])*HEIGHT_PT-float(st.get('margin_bottom_pt',0))+.01)
            if overflow: warnings.append('layout_conflict:horizontal_overflow')
            if clipping: warnings.append('layout_conflict:vertical_clipping')
            if b[0]<0 or b[1]<0 or b[0]+b[2]>1.000001 or b[1]+b[3]>1.000001:
                warnings.append('layout_conflict:outside_slide')
            if locks.get('y') and abs(actual_baseline-baseline)>.5:
                warnings.append('layout_conflict:locked_baseline')
            if len(runs)>1 and st.get('align','left')!='left':
                warnings.append('layout_conflict:manual_alignment_in_continuous_row')
            if metrics['font'].get('fallback'):warnings.append('font_fallback:'+metrics['font']['requested_family'])
            if not metrics['font'].get('accessible_to_system',True):warnings.append('font_not_installed_for_WPS')
            if metrics.get('uncertain'):warnings.append('font_or_formula_measurement_uncertain')
            record = {'version': VERSION, 'engine_revision':ENGINE_REVISION,'id': e['id'], 'block_id': e['block_id'], 'line_id': line_id,
                'run_id': e['run_id'], 'text': e['text'], 'text_class': e.get('text_class'),
                'char_start': e.get('char_start', 0), 'char_end': e.get('char_end', len(e['text'])),
                'whitespace': e.get('whitespace', []), 'source_text': e.get('source_text'),
                'ocr_box': box, 'font_size_pt': float(st.get('size', 18)), **metrics,
                'baseline_pt': actual_baseline, 'line_baseline_pt': baseline,
                'box': list(b), 'overflow': overflow, 'clipping': clipping, 'layout_warning': warnings}
            record['frame_capacity_pt']=max(0,row_right-cursor)
            record['frame_policy']='available_visual_row; independent of cursor advance'
            record['input_signature'] = input_signature(e)
            e['text_layout'] = record
            line_records.append(record)
            cursor += metrics['advance_pt']
        reports.append({'line_id': line_id, 'source_text': runs[0].get('source_text'),
                        'baseline_pt': baseline, 'advance_pt': total, 'runs': line_records,
                        'layout_warning': line_warnings})
    return result, {'version': VERSION, 'slide_id': slide_id, 'lines': reports,
                    'warnings': [{'id': r['id'], 'warnings': r['layout_warning']}
                                 for line in reports for r in line['runs'] if r['layout_warning']]}


def layout_project(project):
    result = deepcopy(project)
    reports = []
    for slide in result['slides']:
        slide['elements'], report = layout_elements(slide['elements'], result.get('design'), slide['id'])
        slide['text_structure'] = report
        reports.append(report)
    result['text_layout_version'] = VERSION
    return result, {'version': VERSION, 'pages': reports, 'office_verified': False}


def diagnose_lines(elements, theme=None, observation=None):
    """Geometry in points. OCR extents and safety frames are evidence, not ink."""
    from collections import defaultdict
    from copy import deepcopy
    groups=defaultdict(list)
    dom={o['id']:o for o in (observation or {}).get('objects',[])}
    stage=(observation or {}).get('stage',{'width':960,'height':540})
    scale=stage['width']/WIDTH_PT
    for e in elements:
        if e.get('kind') in ('text','formula') and e.get('text','').strip():groups[e.get('line_id',e['id'])].append(e)
    rows=[]
    for line_id,runs in groups.items():
        runs.sort(key=lambda e:e.get('run_order',0));evidence=[];issues=[]
        for e in runs:
            m=_measure_run(deepcopy(e),theme or {});st=e.get('style',{});b=e['box'];ml=float(st.get('margin_left_pt',0));mt=float(st.get('margin_top_pt',0))
            baseline=b[1]*HEIGHT_PT+mt+m['ascent_pt']
            baseline+=max(0,b[3]*HEIGHT_PT-m['line_height_pt']-mt-float(st.get('margin_bottom_pt',0)))*{'top':0,'middle':.5,'bottom':1}.get(st.get('vertical_align','top'),0)
            ink=[b[0]*WIDTH_PT+ml+m['ink_box_pt'][0],baseline+m['ink_box_pt'][1],m['ink_box_pt'][2]-m['ink_box_pt'][0],m['ink_box_pt'][3]-m['ink_box_pt'][1]]
            o=dom.get(e['id']);di=(o or {}).get('ink_box')
            if di:ink=[di['x']/scale,di['y']/scale,di['width']/scale,di['height']/scale];baseline=o['baseline_px']/scale
            evidence.append({'id':e['id'],'run_id':e.get('run_id',e['id']),'source_box':e.get('original_ocr_box') or e.get('source_box') or e.get('ocr_box') or b,
                'frame':b,'dom_frame':(o or {}).get('box'),'ink_pt':ink,'baseline_pt':baseline,'advance_pt':m['advance_pt'],
                'size_pt':float(st.get('size',18)),'line_height_pt':m['line_height_pt'],'locks':e.get('layout_locks',{}),
                'legacy_warnings':e.get('text_layout',{}).get('layout_warning',[])})
            if ink[0]<-.5 or ink[1]<-.5 or ink[0]+ink[2]>WIDTH_PT+.5 or ink[1]+ink[3]>HEIGHT_PT+.5:issues.append('ink_outside_slide')
            if ink[0]<b[0]*WIDTH_PT-.5 or ink[1]<b[1]*HEIGHT_PT-.5 or ink[0]+ink[2]>(b[0]+b[2])*WIDTH_PT+.5 or ink[1]+ink[3]>(b[1]+b[3])*HEIGHT_PT+.5:issues.append('ink_outside_frame')
        if max(x['baseline_pt'] for x in evidence)-min(x['baseline_pt'] for x in evidence)>.5:issues.append('baseline_mismatch')
        if len(runs)>1 and all(e['kind']=='text' for e in runs):
            sizes=[x['size_pt'] for x in evidence]
            if max(sizes)-min(sizes)>max(.5,min(sizes)*.04):issues.append('line_font_size_mismatch')
        for left,right in zip(evidence,evidence[1:]):
            if abs(right['frame'][0]*WIDTH_PT-left['frame'][0]*WIDTH_PT-left['advance_pt'])>.5:issues.append('text_spacing')
        source=[min(x['source_box'][0] for x in evidence),min(x['source_box'][1] for x in evidence)]
        source+=[max(x['source_box'][0]+x['source_box'][2] for x in evidence)-source[0],max(x['source_box'][1]+x['source_box'][3] for x in evidence)-source[1]]
        ink_width=max(x['ink_pt'][0]+x['ink_pt'][2] for x in evidence)-min(x['ink_pt'][0] for x in evidence)
        rows.append({'line_id':line_id,'run_ids':[x['run_id'] for x in evidence],'element_ids':[x['id'] for x in evidence],
            'text':''.join(e['text'] for e in runs),'issues':sorted(set(issues)),'evidence':evidence,'source_box':source,
            'source_extent':{'source_width_pt':source[2]*WIDTH_PT,'ink_width_pt':ink_width,
                'advance_pt':sum(x['advance_pt'] for x in evidence),'frame_width_pt':max(x['frame'][2]*WIDTH_PT for x in evidence),
                'classification':'source_reference_difference; not clipping','legacy_warning_retained':any('source_width_exceeded:no_font_shrink' in x['legacy_warnings'] for x in evidence)}})
    # Overlap of visible ink, not transparent safety frames. Keep both line IDs.
    for index,left in enumerate(rows):
        for right in rows[index+1:]:
            for a in left['evidence']:
                for b in right['evidence']:
                    x,y,w,h=a['ink_pt'];xx,yy,ww,hh=b['ink_pt'];area=max(0,min(x+w,xx+ww)-max(x,xx))*max(0,min(y+h,yy+hh)-max(y,yy))
                    if area/max(1e-6,min(w*h,ww*hh))>.12:
                        for row,other in [(left,right),(right,left)]:
                            if 'visible_overlap' not in row['issues']:row['issues'].append('visible_overlap')
                            row.setdefault('overlap_lines',[]).append(other['line_id'])
    return rows


def repair_line(elements, theme=None, slide_id='S001', normalize_size=False, source_box=None):
    """Stage03 explicit line transaction; use the existing engine only."""
    from copy import deepcopy
    import statistics
    runs=deepcopy(elements)
    unlocked=[e for e in runs if not e.get('layout_locks',{}).get('size') and not e.get('layout_locks',{}).get('style')]
    locked_sizes={float(e.get('style',{}).get('size',18)) for e in runs if e.get('layout_locks',{}).get('size') or e.get('layout_locks',{}).get('style')}
    target=next(iter(locked_sizes)) if len(locked_sizes)==1 else statistics.median(float(e.get('style',{}).get('size',18)) for e in runs)
    for e in runs:
        e.pop('text_layout',None);e['layout_managed']=True
        if normalize_size and e in unlocked:e.setdefault('style',{})['size']=target
        if source_box is not None:e['ocr_box']=list(source_box)
    # Full style locks also lock geometry-affecting style attributes.
    for e in runs:
        if e.get('layout_locks',{}).get('style'):
            e['layout_locks']={**e['layout_locks'],**{k:True for k in e.get('style',{})}}
    constraints={};cursor=0
    for e in runs:
        locks=e.get('layout_locks',{});m=_measure_run(deepcopy(e),theme or {});line=constraints.setdefault(e.get('line_id',e['id']),{})
        if locks.get('x') and 'cursor_pt' not in line:line['cursor_pt']=e['box'][0]*WIDTH_PT-cursor
        if locks.get('y') and 'baseline_pt' not in line:
            st=e.get('style',{});line['baseline_pt']=e['box'][1]*HEIGHT_PT+float(st.get('margin_top_pt',0))+m['ascent_pt']
        cursor+=m['advance_pt']
    proposed,report=layout_elements(runs,theme,slide_id,line_constraints=constraints)
    for old,new in zip(elements,proposed):
        new['layout_locks']=deepcopy(old.get('layout_locks',{}))
        if old.get('layout_locks',{}).get('style'):new['style']=deepcopy(old.get('style',{}))
        new['text_layout']['input_signature']=input_signature(new)
    return proposed,report
