"""Reuse model-proposed issue edits while their read objects are unchanged."""
from copy import deepcopy
import hashlib
import json
from .ai_context import object_view


def _digest(element):
    view=object_view(element)
    # The layout engine materializes an empty lock map during any page edit;
    # absent and empty both mean no locks and must not invalidate other plans.
    view.setdefault('layout_locks',{})
    return hashlib.sha256(json.dumps(view, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class RepairPlanQueue:
    def __init__(self):
        self.plans = {}
        self.hits = 0

    def add(self, plans, slide):
        if not isinstance(plans, list):
            return
        elements = {e.get('id'): e for e in slide.get('elements', [])}
        for plan in plans:
            if not isinstance(plan, dict) or not plan.get('issue_id'):
                continue
            calls = plan.get('tool_calls')
            if not isinstance(calls, list) or not 1 <= len(calls) <= 20:
                continue
            targets = set()
            boxes = []
            create_boxes = []
            valid = True
            for call in calls:
                if not isinstance(call, dict):
                    valid = False;break
                args = call.get('arguments') or {}
                if not isinstance(args, dict) or args.get('slide_id', slide['id']) != slide['id']:
                    valid = False;break
                tool=call.get('tool')
                if tool in {'edit_element', 'delete_element'}:
                    ids = [args.get('element_id')]
                elif call.get('tool') == 'batch_edit':
                    edits = args.get('edits') or []
                    if not isinstance(edits,list) or not edits or any(not isinstance(e,dict) for e in edits):
                        valid=False;break
                    if any(e.get('slide_id',slide['id']) != slide['id'] for e in edits):
                        valid=False;break
                    ids = [e.get('element_id') for e in edits]
                elif tool == 'create_element':
                    element=args.get('element')
                    if (not isinstance(element,dict) or element.get('kind') not in {'text','formula'}
                            or not isinstance(element.get('text'),str) or not element.get('text').strip()):
                        valid=False;break
                    ids=[]
                    boxes.append(element.get('box'))
                    create_boxes.append(element.get('box'))
                else:
                    valid = False;break
                if any(not isinstance(i,str) or i not in elements for i in ids):
                    valid = False;break
                targets.update(ids)
                for eid in ids:
                    element=elements[eid]
                    boxes.append(element.get('box'))
                    if tool=='edit_element':
                        new_box=(args.get('patch') or {}).get('box')
                        if new_box is not None:boxes.append(new_box)
                if tool=='batch_edit':
                    for edit in args.get('edits') or []:
                        new_box=(edit.get('patch') or {}).get('box')
                        if new_box is not None:boxes.append(new_box)
            if valid and (targets or boxes):
                # The whole related line is a read dependency, even when only
                # one of its native runs is explicitly written by this plan.
                lines={elements[i].get('line_id') for i in targets if elements[i].get('line_id')}
                dependencies=targets|{i for i,e in elements.items() if e.get('line_id') in lines}
                self.plans[str(plan['issue_id'])]={'calls':deepcopy(calls),
                    'reads':{i:_digest(elements[i]) for i in dependencies},
                    'targets':targets,'boxes':[list(b) for b in boxes if isinstance(b,(list,tuple)) and len(b)==4],
                    'create_boxes':[list(b) for b in create_boxes if isinstance(b,(list,tuple)) and len(b)==4]}

    @staticmethod
    def _boxes_overlap(left, right):
        try:
            ax,ay,aw,ah=map(float,left);bx,by,bw,bh=map(float,right)
        except (TypeError,ValueError):
            return True
        return min(ax+aw,bx+bw)-max(ax,bx)>0 and min(ay+ah,by+bh)-max(ay,by)>0

    @staticmethod
    def _near_duplicate_box(left, right):
        """Reject near-identical create boxes without blocking nearby repairs."""
        try:
            ax,ay,aw,ah=map(float,left);bx,by,bw,bh=map(float,right)
        except (TypeError,ValueError):
            return True
        intersection=max(0.0,min(ax+aw,bx+bw)-max(ax,bx))*max(0.0,min(ay+ah,by+bh)-max(ay,by))
        smaller=min(max(aw*ah,0.0),max(bw*bh,0.0))
        return smaller>0 and intersection/smaller>=0.85

    def peek_batch(self, issues, slide, *, limit=None, max_calls=1000):
        """Collect plans for every diagnosed issue that remain valid.

        Plans remain queued until the caller accepts the single rendered batch.
        Stale plans are discarded. The issue order is preserved (normally P0→P3).
        """
        elements={e.get('id'):e for e in slide.get('elements',[])}
        chosen=[];calls=[];call_signatures=set()
        limit=max(1,int(limit if limit is not None else len(issues or []) or 1))
        for issue in issues or []:
            issue_id=str((issue or {}).get('issue_id') or '')
            plan=self.plans.get(issue_id)
            if not plan:continue
            if any(i not in elements or _digest(elements[i])!=sha for i,sha in plan['reads'].items()):
                self.plans.pop(issue_id,None);continue
            candidate_calls=plan['calls']
            if len(chosen)>=max(1,int(limit)) or len(calls)+len(candidate_calls)>max(1,int(max_calls)):
                continue
            unique_calls=[]
            for call in candidate_calls:
                signature=json.dumps(call,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                if signature not in call_signatures:
                    call_signatures.add(signature);unique_calls.append(deepcopy(call))
            if len(calls)+len(unique_calls)>max(1,int(max_calls)):
                continue
            # Keep shared-object plans together. The batch is applied to an
            # isolated candidate and checked in one render. A rejected tool
            # does not discard successful operations from other plans.
            chosen.append(issue);calls.extend(unique_calls)
        if not chosen:return {'issues':[],'calls':[]}
        return {'issues':deepcopy(chosen),'calls':calls}

    def consume(self, issues):
        for issue in issues or []:
            issue_id=str((issue or {}).get('issue_id') or '')
            self.plans.pop(issue_id,None)

    def take(self, issue, slide):
        bundle=self.peek_batch([issue],slide,limit=1)
        if not bundle['calls']:
            return []
        self.consume(bundle['issues'])
        self.hits+=1
        return bundle['calls']
