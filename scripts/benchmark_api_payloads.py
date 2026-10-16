"""Compare model context bytes using saved candidates; never calls an API.

Run with --project-dir PATH --candidate S001:TOKEN --candidate S002:TOKEN.
Only byte counts and page IDs are printed; no slide text or images are logged.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.ai_context import (model_object_context, source_view, object_view,
    table_view, history_view, focus_reflection_context)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def benchmark(project_dir, slide_id, token):
    folder = project_dir / 'build' / 'visual_agent' / token
    project = json.loads((folder / 'candidate_project.json').read_text())
    elements = next(s for s in project['slides'] if s['id'] == slide_id)['elements']
    source_file = project_dir / 'build' / 'reconstruction' / (slide_id + '_ocr.json')
    sources = json.loads(source_file.read_text()).get('regions', []) if source_file.is_file() else []
    status = json.loads((folder / 'status.json').read_text())
    old_objects = [{'id': e.get('id'), 'kind': e.get('kind'), 'text': e.get('text', ''),
        'box': e.get('box'), 'style': e.get('style', {}), 'line_id': e.get('line_id'),
        'run_id': e.get('run_id'), 'run_order': e.get('run_order'),
        'text_layout': e.get('text_layout', {}), 'asset': e.get('asset'),
        'layout_locks': e.get('layout_locks', {})} for e in elements[:400]]
    old_bytes = len(encoded(old_objects).encode()) + len(encoded(sources)[:16000].encode())
    new_bytes = len(encoded(model_object_context(elements)).encode()) + len(encoded(
        table_view(source_view(sources, [object_view(e) for e in elements]))).encode())
    reflection_issues=[x for x in status.get('issues',[]) if isinstance(x,dict)
        and isinstance(x.get('region'),list) and len(x['region'])==4]
    reflection_metrics=None
    if reflection_issues:
        focused_elements,focused_sources=focus_reflection_context(elements,sources,reflection_issues[0])
        focused_bytes=len(encoded(model_object_context(focused_elements)).encode())+len(encoded(
            table_view(source_view(focused_sources,[object_view(e) for e in focused_elements]))).encode())
        reflection_metrics={'sample_issue_id':reflection_issues[0].get('issue_id'),
            'before_bytes':new_bytes,'after_bytes':focused_bytes,
            'before_objects':len(elements),'after_objects':len(focused_elements),
            'before_source_regions':len(sources),'after_source_regions':len(focused_sources),
            'byte_reduction_fraction':round(1-focused_bytes/max(new_bytes,1),4)}
    return {'slide_id': slide_id, 'objects': len(elements), 'source_regions': len(sources),
        'before_objects_and_ocr_bytes': old_bytes, 'after_objects_and_ocr_bytes': new_bytes,
        'byte_reduction_fraction': round(1 - new_bytes / max(old_bytes, 1), 4),
        'before_history_bytes_full_local_archive': len(encoded(status.get('iterations', [])).encode()),
        'before_history_bytes_sent_with_legacy_slice': len(encoded(status.get('iterations', []))[:12000].encode()),
        'after_history_bytes': len(encoded(history_view(status.get('iterations'))).encode()),
        'reflection_scoped_objects_and_ocr':reflection_metrics,
        'provider_usage': 'not_measured_offline', 'api_calls': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, required=True)
    parser.add_argument('--candidate', action='append', required=True)
    args = parser.parse_args()
    print(json.dumps([benchmark(args.project_dir, *candidate.split(':', 1))
                     for candidate in args.candidate], ensure_ascii=False, indent=2))
