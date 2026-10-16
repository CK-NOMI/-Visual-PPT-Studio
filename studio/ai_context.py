"""Model-facing views; persisted render/layout objects are never modified."""
from copy import deepcopy
import json


_INTERNAL = {'file_sha256', 'file_identity', 'input_signature', 'engine_revision',
             'path', 'aliases', 'version', 'bundled', 'accessible_to_system'}


def evidence_view(value):
    if isinstance(value, dict):
        return {k: evidence_view(v) for k, v in value.items() if k not in _INTERNAL}
    if isinstance(value, list):
        return [evidence_view(v) for v in value]
    return deepcopy(value)


def object_view(element):
    keys = ('id', 'kind', 'text', 'source_text', 'box', 'style', 'line_id', 'run_id',
            'run_order', 'rich_runs', 'rotation', 'z_index', 'asset', 'layout_locks',
            'latex', 'mathml', 'source_group')
    out = {k: evidence_view(element[k]) for k in keys if k in element}
    layout = element.get('text_layout')
    if isinstance(layout, dict):
        # These duplicate the live object or refer only to local font files.
        duplicate = {'id', 'text', 'source_text', 'box', 'line_id', 'run_id', 'block_id'}
        out['text_layout'] = evidence_view({k: v for k, v in layout.items() if k not in duplicate})
        font = out['text_layout'].get('font')
        if isinstance(font, dict):
            # Full style also appears on the object. Keep actual font/fallback
            # evidence, rather than repeating every style and file attribute.
            if font.get('style') == out.get('style'):
                font.pop('style', None)
    return out


def source_view(rows, objects=None):
    """OCR evidence, without repeating each native object's layout/font dump."""
    keys = ('id', 'kind', 'role', 'text', 'source_text', 'box', 'source_box', 'ocr_box',
            'confidence', 'line_id', 'run_id', 'run_order', 'source_group', 'block_id',
            'ocr_conflicts', 'source_conflicts', 'text_unit', 'whitespace', 'style',
            'latex', 'mathml', 'formula', 'source')
    by_run = {o.get('run_id'): o for o in objects or [] if o.get('run_id')}
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = {k: evidence_view(row[k]) for k in keys if k in row}
        native = by_run.get(row.get('run_id'))
        if native:
            item['element_ref'] = native.get('id')
            for key in ('kind', 'text', 'source_text', 'style', 'line_id', 'run_id',
                        'run_order', 'source_group', 'box', 'whitespace'):
                if key in item and item[key] == native.get(key):
                    item.pop(key)
            # Source position is independent of a moved native object's box.
            if item.get('ocr_box') == item.get('source_box'):
                item.pop('ocr_box', None)
        sizes = row.get('font_size_evidence')
        if isinstance(sizes, dict):
            item['font_size_evidence'] = {k: deepcopy(sizes[k]) for k in
                ('method', 'estimate_pt', 'image_height_px', 'source_ink_height_px') if k in sizes}
        out.append(item)
    return out


def table_view(rows):
    """Lossless field-name deduplication with explicit missing-value indices."""
    columns = sorted({key for row in rows for key in row})
    # None can be an actual value. The presence bitmap disambiguates absence.
    return {'columns': columns, 'rows': [[row.get(k) for k in columns] for row in rows],
            'present': [[i for i, k in enumerate(columns) if k in row] for row in rows]}


def model_object_context(elements):
    objects = [object_view(e) for e in elements]
    layouts = []
    fonts = {}
    by_font = {}
    for obj in objects:
        layout = obj.pop('text_layout', None)
        if not isinstance(layout, dict):
            continue
        font = layout.pop('font', None)
        if isinstance(font, dict):
            identity = json.dumps(font, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if identity not in by_font:
                ref = 'f' + str(len(by_font))
                by_font[identity] = ref
                fonts[ref] = font
            layout['font_ref'] = by_font[identity]
        obj['layout_ref'] = len(layouts)
        layouts.append(layout)
    return {'objects': objects, 'layout_table': table_view(layouts), 'font_definitions': fonts}


def focus_reflection_context(elements, source_rows, issue):
    """Scope verbose OCR/layout data to one issue while keeping nearby context.

    The caller still sends full-page Design and before/after renders. If the
    issue has no trustworthy normalized region, or scoping finds no objects,
    return the complete context (fail open to preserve visual repair quality).
    """
    if not isinstance(issue, dict):
        return elements, source_rows
    region = issue.get('region')
    ids = issue.get('element_ids') or issue.get('object_ids') or []
    ids = {str(value) for value in ids if isinstance(value, (str, int))}
    try:
        x, y, w, h = map(float, region)
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1
                and x + w <= 1.001 and y + h <= 1.001):
            raise ValueError('invalid normalized region')
    except (TypeError, ValueError):
        return elements, source_rows

    # Include a small band around the target so alignment and duplicate text
    # in neighboring boxes remain visible to the model.
    px = max(0.018, min(0.06, w * 0.35))
    py = max(0.018, min(0.06, h * 0.65))
    roi = (max(0.0, x - px), max(0.0, y - py),
           min(1.0, x + w + px), min(1.0, y + h + py))

    def overlaps(box):
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            return False
        try:
            bx, by, bw, bh = map(float, box)
        except (TypeError, ValueError):
            return False
        if bw <= 0 or bh <= 0 or bx < -0.01 or by < -0.01 or bx > 1.01 or by > 1.01:
            return False
        return bx < roi[2] and bx + bw > roi[0] and by < roi[3] and by + bh > roi[1]

    selected = {e.get('id') for e in elements if e.get('id') in ids}
    selected.update(e.get('id') for e in elements if overlaps(e.get('box')))
    if not selected:
        return elements, source_rows
    lines = {e.get('line_id') for e in elements
             if e.get('id') in selected and e.get('line_id')}
    selected.update(e.get('id') for e in elements if e.get('line_id') in lines)
    focused_elements = [e for e in elements if e.get('id') in selected]

    run_ids = {e.get('run_id') for e in focused_elements if e.get('run_id')}
    source_ids = {e.get('id') for e in focused_elements}
    focused_sources = [row for row in source_rows or []
        if isinstance(row, dict) and (
            row.get('run_id') in run_ids or row.get('id') in source_ids or
            overlaps(row.get('ocr_box')) or overlaps(row.get('source_box')) or
            overlaps(row.get('box')))]
    # If OCR contains no usable geometry/linkage, preserve it rather than
    # presenting an empty evidence set as authoritative.
    if source_rows and not focused_sources:
        focused_sources = source_rows
    return focused_elements, focused_sources


def history_view(rows):
    """Keep every issue's latest outcome and recent exact edits, not render dumps."""
    latest = {}
    recent = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        reflection = row.get('reflection') or {}
        issues = reflection.get('issues') or (reflection.get('reflection') or {}).get('issues') or []
        for issue in issues:
            if isinstance(issue, dict) and issue.get('issue_id'):
                latest[issue['issue_id']] = {k: deepcopy(issue[k]) for k in
                    ('issue_id', 'status', 'description', 'expected_result') if k in issue}
        events = []
        for event in row.get('tool_events') or []:
            result = event.get('result') or {}
            compact = {k: deepcopy(event[k]) for k in ('tool', 'status', 'error') if k in event}
            if isinstance(result.get('element'), dict):
                compact['element'] = object_view(result['element'])
            events.append(compact)
        recent.append({'round': row.get('round'), 'outcome': row.get('outcome'),
                       'tool_calls': deepcopy(row.get('tool_calls') or []),
                       'tool_events': events, 'reason': reflection.get('reason', '')})
    return {'latest_issue_outcomes': list(latest.values()), 'recent_attempts': recent[-3:]}


def feedback_view(feedback):
    """Discard embedded render/project snapshots but retain execution evidence."""
    if isinstance(feedback, list):
        return [feedback_view(v) for v in feedback]
    if not isinstance(feedback, dict):
        return deepcopy(feedback)
    out = {}
    for k, v in feedback.items():
        if k in {'before_render', 'after_render', 'candidate_project', 'original_project'}:
            continue
        if k == 'element' and isinstance(v, dict):
            out[k] = object_view(v)
        else:
            out[k] = feedback_view(v)
    return evidence_view(out)


def runtime_messages_view(messages):
    """Keep native tool/reasoning history and current before/after image evidence.

    Only superseded user images are omitted. Assistant and tool messages retain
    their order, IDs and reasoning_content verbatim, including old tool calls.
    """
    images = []
    for mi, message in enumerate(messages):
        if message.get('role') != 'user' or not isinstance(message.get('content'), list):
            continue
        parts = message['content']
        is_crop = any('区域' in str(p.get('text', '')) for p in parts if isinstance(p, dict))
        for pi, part in enumerate(parts):
            if isinstance(part, dict) and part.get('type') in {'image_url', 'file'}:
                images.append((mi, pi, is_crop))
    if len(images) <= 3:
        return deepcopy(messages)
    design = images[0][:2]
    renders = [x for x in images[1:] if not x[2]]
    retained = {design, *(x[:2] for x in renders[-2:])}
    crop_since = renders[-2][0] if len(renders) >= 2 else 0
    retained.update(x[:2] for x in images[1:] if x[2] and x[0] >= crop_since)
    output = deepcopy(messages)
    for mi, pi, _ in images:
        if (mi, pi) not in retained:
            output[mi]['content'][pi] = {'type': 'text',
                'text': '[历史截图已由后续真实渲染更新；请依据Design及最新修改前后截图判断。]'}
    return output
