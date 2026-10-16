"""Practical CLI for Codex to inspect, test, and run draft-only edits.

No command here writes to the original project.json. Publishing is delegated
back to Visual PPT Studio after trusted QA, revision check, and user approval.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time
import uuid
from .errors import EditingError
from .scene import signature
from .transactions import EditingSession
from .permissions import PermissionPolicy,EvidenceRegistry,TrustedEvidence
from .agent import DeepSeekChatProvider,VisualEditingAgent
from .tools import EditingTools
from .render import StudioLibreOfficeRenderer


def _load(path):return json.loads(Path(path).expanduser().resolve().read_text(encoding='utf-8'))

def _dump(v):print(json.dumps(v,ensure_ascii=False,indent=2))

def _evidence(path):
    if not path:return EvidenceRegistry()
    entries=[]
    for item in _load(path):
        # Model cannot supply these. The HOST must have independently verified them.
        entries.append(TrustedEvidence(**{**item,'box':tuple(item['box'])}))
    return EvidenceRegistry(entries)


def main(argv=None):
    p=argparse.ArgumentParser(description='Visual PPT Editing Runtime: isolated editable drafts only')
    sub=p.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init',help='Create isolated transaction session from Studio project.json')
    init.add_argument('--project-json',required=True)
    init.add_argument('--slide',required=True)
    init.add_argument('--session-dir',required=True)
    init.add_argument('--mode',choices=['restore','free'],default='restore')
    init.add_argument('--allow-text',action='store_true')
    init.add_argument('--evidence-json')
    edit=sub.add_parser('edit',help='Perform atomic operation batch from JSON in existing session')
    edit.add_argument('--session-dir',required=True);edit.add_argument('--slide',required=True)
    edit.add_argument('--issue',required=True);edit.add_argument('--operations',required=True)
    edit.add_argument('--evidence-json')
    inspect=sub.add_parser('inspect',help='Show compact current draft')
    inspect.add_argument('--session-dir',required=True);inspect.add_argument('--slide',required=True)
    review=sub.add_parser('review',help='Manually accept/reject transaction after inspection')
    review.add_argument('--session-dir',required=True);review.add_argument('--tx',required=True)
    group=review.add_mutually_exclusive_group(required=True)
    group.add_argument('--accept',action='store_true');group.add_argument('--reject',action='store_true')
    prepare=sub.add_parser('prepare',help='Accepted-only candidate; does NOT commit live project')
    prepare.add_argument('--session-dir',required=True);prepare.add_argument('--current-project-json',required=True)
    prepare.add_argument('--output-json',required=True)
    agent=sub.add_parser('agent',help='Run real DeepSeek + real Studio renderer; draft-only')
    agent.add_argument('--session-dir',required=True);agent.add_argument('--studio-root',required=True)
    agent.add_argument('--source-project-dir',required=True);agent.add_argument('--design',required=True)
    agent.add_argument('--slide',required=True);agent.add_argument('--output-dir',required=True)
    agent.add_argument('--instruction',default='按照Design尽量还原本页')
    agent.add_argument('--model',default='deepseek-flash');agent.add_argument('--base-url',default='https://api.deepseek.com')
    agent.add_argument('--rounds',type=int,default=3);agent.add_argument('--evidence-json')
    args=p.parse_args(argv)
    try:
        if args.command=='init':
            proj=_load(args.project_json)
            session_path=Path(args.session_dir).resolve();source=Path(args.project_json).resolve()
            if source.is_relative_to(session_path) or session_path.is_relative_to(source.parent):
                raise EditingError('session-dir must be outside original project folder')
            if (session_path/'session.json').exists():raise EditingError('session already exists; use edit/agent/inspect')
            policy=PermissionPolicy(mode=args.mode,allowed_slides=frozenset({args.slide}),
                                    allow_text_changes=args.allow_text)
            session=EditingSession(proj,policy,_evidence(args.evidence_json),root=session_path)
            _dump({'session_dir':str(session.root),'session_id':session.session_id,'source_signature':session.base_sha,
                   'source_project_unmodified':True})
        elif args.command=='edit':
            session=EditingSession.resume(Path(args.session_dir),_evidence(args.evidence_json))
            _dump(session.execute(args.slide,args.issue,_load(args.operations)))
        elif args.command=='inspect':
            session=EditingSession.resume(Path(args.session_dir))
            from .tools import EditingTools
            _dump(EditingTools(session).dispatch('get_page_state',{'slide_id':args.slide,'offset':0,'limit':60}))
        elif args.command=='review':
            session=EditingSession.resume(Path(args.session_dir))
            _dump(session.mark(args.tx,args.accept,host_approval=bool(args.accept),qa={'manual_review':True}))
        elif args.command=='prepare':
            session=EditingSession.resume(Path(args.session_dir))
            data=session.prepared_commit(_load(args.current_project_json))
            out=Path(args.output_json).resolve()
            if out.exists():raise EditingError('output candidate already exists')
            if out.name=='project.json' or out.is_relative_to(Path(args.current_project_json).resolve().parent):
                raise EditingError('cannot write candidate into original project directory')
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(json.dumps(data['project'],ensure_ascii=False,indent=2),encoding='utf-8')
            _dump({k:v for k,v in data.items() if k!='project'}|{'output':str(out),'pending_unreviewed_not_included':True})
        elif args.command=='agent':
            sess=EditingSession.resume(Path(args.session_dir),_evidence(args.evidence_json))
            out=Path(args.output_dir).resolve()
            if out.is_relative_to(Path(args.source_project_dir).resolve()):
                raise EditingError('output-dir cannot be inside source project folder')
            renderer=StudioLibreOfficeRenderer(Path(args.studio_root),Path(args.source_project_dir),out)
            tools=EditingTools(sess,renderer)
            agent=VisualEditingAgent(DeepSeekChatProvider(model=args.model,base_url=args.base_url),tools,
                                    max_rounds=args.rounds)
            # Without trusted host QA, agent changes remain pending, never auto-accepted.
            from ..api_usage import set_context,reset_context,record_task_outcome
            project_id=sess.working.get('id');task_id=uuid.uuid4().hex;started=time.perf_counter()
            usage_token=set_context(task_id=task_id,project_id=project_id,slide_id=args.slide,module='editing_runtime_cli')
            try:
                result=agent.run(args.slide,Path(args.design),instruction=args.instruction,log_path=out/'agent-trace.json')
            except Exception as exc:
                record_task_outcome(task_id=task_id,project_id=project_id,slide_id=args.slide,
                    module='editing_runtime_cli',status='failed',repair_success=None,
                    duration_ms=round((time.perf_counter()-started)*1000),details={'error_class':type(exc).__name__})
                raise
            finally:
                reset_context(usage_token)
            record_task_outcome(task_id=task_id,project_id=project_id,slide_id=args.slide,
                module='editing_runtime_cli',status='cancelled' if result.get('cancelled') else 'complete',
                repair_success=None,duration_ms=round((time.perf_counter()-started)*1000),
                details={'repair_success':'not_measured_without_host_validation'})
            _dump(result)
    except (EditingError,FileNotFoundError,ValueError,KeyError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
