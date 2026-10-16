"""Issue-scoped atomic transactions; rejecting one preserves independent edits.

A pending candidate may contain many transactions. Accepted-only replay produces
an explicit commit proposal. Nothing writes the source project by default.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time
from threading import RLock
from typing import Any, Callable
from uuid import uuid4
from .scene import cloned, signature, slide, element, validate_box, validate_style, validate_element, validate_scene, groups_for_page, _hex, validate_rich_runs, points_to_box, box_to_points
from .permissions import PermissionPolicy, EvidenceRegistry, enforce_scope, require_text_evidence
from .errors import ValidationError, RevisionConflict, PermissionDenied, TransactionError, EvidenceError

@dataclass
class Transaction:
    tx_id: str
    issue_id: str
    slide_id: str
    operations: list[dict]
    status: str = 'pending'           # pending | accepted | rejected
    before_sha: str = ''
    after_sha: str = ''
    changes: list[dict] = field(default_factory=list)
    notes: str = ''
    created_at: float = field(default_factory=time.time)
    qa: dict = field(default_factory=dict)

    def to_dict(self):
        return cloned(self.__dict__)

class EditingSession:
    def __init__(self, project:dict, policy:PermissionPolicy, evidence:EvidenceRegistry|None=None,
                 *, root:Path|None=None, session_id:str|None=None):
        validate_scene(project)
        for sid in policy.allowed_slides:slide(project,sid)
        self.base=cloned(project)
        self.base_sha=signature(project)
        self.working=cloned(project)
        self.policy=policy
        self.evidence=evidence or EvidenceRegistry()
        self.session_id=session_id or uuid4().hex
        self.root=Path(root).resolve() if root else None
        self.transactions:list[Transaction]=[]
        self._lock=RLock()
        self._version=0
        self._redo_history:list[Transaction]=[]
        if self.root:
            self.root.mkdir(parents=True,exist_ok=True)
            self._persist()

    def page(self,sid:str,include_elements:bool=True)->dict:
        with self._lock:
            s=cloned(slide(self.working,sid))
            if not include_elements:s['elements']=[{'id':e['id'],'kind':e['kind']} for e in s['elements']]
            return {'slide':s,'groups':groups_for_page(slide(self.working,sid)),
                    'draft_version':self._version,'draft_sha':signature(self.working),
                    'transactions':[t.to_dict() for t in self.transactions if t.slide_id==sid]}

    def get_element(self,sid,eid):
        with self._lock:return cloned(element(slide(self.working,sid),eid))

    def execute(self,sid:str,issue_id:str,ops:list[dict],*,tx_id:str|None=None)->dict:
        """Perform all operations or none; replay-safe transaction IDs."""
        with self._lock:
            if not isinstance(issue_id,str) or not issue_id or len(issue_id)>128:raise ValidationError('issue_id required')
            if not isinstance(ops,list) or not 1<=len(ops)<=40:raise ValidationError('transaction requires 1..40 operations')
            tx_id=tx_id or uuid4().hex
            if not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}',tx_id):raise ValidationError('invalid transaction ID')
            existing=next((t for t in self.transactions if t.tx_id==tx_id),None)
            if existing:
                if existing.slide_id==sid and existing.issue_id==issue_id and existing.operations==ops:
                    return existing.to_dict()   # idempotent retries
                raise TransactionError('transaction ID reused with different content')
            before=cloned(self.working)
            after=cloned(self.working)
            for op in ops:self._apply(after,sid,op)
            validate_scene(after)
            if after==before:raise TransactionError('no actual changes; transaction not created')
            before_by={e['id']:e for e in slide(before,sid)['elements']}
            after_by={e['id']:e for e in slide(after,sid)['elements']}
            changes=[{'id':eid,'before':cloned(before_by.get(eid)), 'after':cloned(after_by.get(eid))}
                     for eid in sorted(before_by.keys()|after_by.keys()) if before_by.get(eid)!=after_by.get(eid)]
            if slide(before,sid).get('background')!=slide(after,sid).get('background'):
                changes.append({'property':'background','before':slide(before,sid).get('background'),
                                'after':slide(after,sid).get('background')})
            # Reorder alone requires a record as well.
            order_b=[e['id'] for e in slide(before,sid)['elements']]
            order_a=[e['id'] for e in slide(after,sid)['elements']]
            if order_b!=order_a:changes.append({'property':'element_order','before':order_b,'after':order_a})
            tx=Transaction(tx_id,issue_id,sid,cloned(ops),before_sha=signature(before),after_sha=signature(after),changes=changes)
            self.working=after
            self.transactions.append(tx)
            self._version+=1
            self._redo_history.clear()
            self._persist()
            return tx.to_dict()

    def _apply(self,project:dict,sid:str,op:dict):
        if not isinstance(op,dict):raise ValidationError('operation must be an object')
        action=op.get('action')
        page=slide(project,sid)
        eid=op.get('element_id')
        if action not in self.policy.allowed_operations:raise PermissionDenied(f'operation disabled: {action}')
        enforce_scope(self.policy,project,sid,action,eid=eid)
        if action=='set_properties':
            if not isinstance(eid,str):raise ValidationError('element_id required')
            target=element(page,eid)
            changes=op.get('changes')
            if not isinstance(changes,dict) or not changes:raise ValidationError('changes required')
            bad=set(changes)-{'box','box_pt','style','text','rich_runs','rotation'}
            if bad:raise ValidationError(f'unsupported properties: {sorted(bad)}')
            if 'box' in changes and 'box_pt' in changes:raise ValidationError('choose box or box_pt, not both')
            if 'box' in changes or 'box_pt' in changes:
                target['box']=validate_box(changes['box'],allow_connector_endpoint=target['kind']=='line') if 'box' in changes else points_to_box(changes['box_pt'])
                if target.get('text_layout'):
                    target.pop('text_layout',None);target['runtime_layout_dirty']=True
            if 'rotation' in changes:
                from .scene import _number
                target['rotation']=_number(changes['rotation'],0,360,'rotation')
            if 'style' in changes:
                patch=validate_style(target['kind'],changes['style'])
                target.setdefault('style',{}).update(patch)
                # Existing measured parameters are derived; invalidate rather than silently reuse stale font geometry.
                if target.get('text_layout') and (('box' in changes) or any(k in patch for k in ('size','font','eastAsianFont','char_spacing_pt','bold','italic'))):
                    target.pop('text_layout',None)
                    target['runtime_layout_dirty']=True
            if 'text' in changes:
                text=changes['text']
                if target['kind'] not in ('text','formula') or not isinstance(text,str):raise ValidationError('text only allowed on text/formula')
                if text!=target.get('text',''):
                    require_text_evidence(self.policy,self.evidence,sid,text,op.get('evidence_id'))
                    target['text']=text
                    target.pop('rich_runs',None)
                    target.pop('text_layout',None);target['runtime_layout_dirty']=True
            if 'rich_runs' in changes:
                if target['kind']!='text':raise ValidationError('rich_runs requires text element')
                target['rich_runs']=validate_rich_runs(changes['rich_runs'],target.get('text',''))
                target.pop('text_layout',None);target['runtime_layout_dirty']=True
        elif action=='set_rich_runs':
            target=element(page,eid)
            if target['kind']!='text':raise ValidationError('rich_runs requires text')
            target['rich_runs']=validate_rich_runs(op.get('runs'),target.get('text',''))
            target.pop('text_layout',None);target['runtime_layout_dirty']=True
        elif action=='set_geometry':
            target=element(page,eid)
            has_box='box' in op;has_pt='box_pt' in op
            if has_box==has_pt:raise ValidationError('specify one of box or box_pt')
            target['box']=validate_box(op['box'],allow_connector_endpoint=target['kind']=='line') if has_box else points_to_box(op['box_pt'])
            target.pop('text_layout',None);target['runtime_layout_dirty']=True
        elif action=='nudge_element':
            target=element(page,eid)
            delta=op.get('delta_pt')
            if not isinstance(delta,list) or len(delta)!=2:raise ValidationError('delta_pt must be [dx,dy]')
            for v in delta:
                if isinstance(v,bool) or not isinstance(v,(int,float)) or not -960<=v<=960:raise ValidationError('delta_pt out of range')
            x,y,w,h=box_to_points(target['box'])
            target['box']=points_to_box([x+delta[0],y+delta[1],w,h]);target.pop('text_layout',None);target['runtime_layout_dirty']=True
        elif action=='align_elements':
            ids=op.get('element_ids');axis=op.get('alignment')
            if not isinstance(ids,list) or len(set(ids))!=len(ids) or not 2<=len(ids)<=100:raise ValidationError('need 2..100 unique element_ids')
            if axis not in ('left','right','hcenter','top','bottom','vcenter'):raise ValidationError('unsupported alignment')
            for e_id in ids:enforce_scope(self.policy,project,sid,action,eid=e_id)
            objs=[element(page,e_id) for e_id in ids];ref=objs[0]['box']
            for obj in objs[1:]:
                x,y,w,h=obj['box'];rx,ry,rw,rh=ref
                if axis=='left':x=rx
                elif axis=='right':x=rx+rw-w
                elif axis=='hcenter':x=rx+rw/2-w/2
                elif axis=='top':y=ry
                elif axis=='bottom':y=ry+rh-h
                elif axis=='vcenter':y=ry+rh/2-h/2
                obj['box']=validate_box([x,y,w,h],allow_connector_endpoint=obj['kind']=='line')
                obj.pop('text_layout',None);obj['runtime_layout_dirty']=True
        elif action=='distribute_elements':
            ids=op.get('element_ids');axis=op.get('axis','horizontal')
            if not isinstance(ids,list) or len(set(ids))!=len(ids) or not 3<=len(ids)<=100:raise ValidationError('need 3..100 unique element_ids')
            if axis not in ('horizontal','vertical'):raise ValidationError('axis must be horizontal|vertical')
            for e_id in ids:enforce_scope(self.policy,project,sid,action,eid=e_id)
            objs=sorted([element(page,e_id) for e_id in ids],key=lambda o:o['box'][0 if axis=='horizontal' else 1])
            ind=0 if axis=='horizontal' else 1; extent=2 if axis=='horizontal' else 3
            span=objs[-1]['box'][ind]+objs[-1]['box'][extent]-objs[0]['box'][ind]
            gap=(span-sum(o['box'][extent] for o in objs))/(len(objs)-1)
            if gap<0:raise ValidationError('objects cannot be distributed without overlapping bounds')
            cursor=objs[0]['box'][ind]+objs[0]['box'][extent]
            for obj in objs[1:-1]:
                box=list(obj['box']);box[ind]=cursor+gap
                obj['box']=validate_box(box,allow_connector_endpoint=obj['kind']=='line')
                obj.pop('text_layout',None);obj['runtime_layout_dirty']=True
                cursor=box[ind]+box[extent]
        elif action=='create_element':
            obj=op.get('element')
            if not isinstance(obj,dict):raise ValidationError('element payload required')
            # Allow only explicitly supported creation fields; preserve known source-tracking attributes.
            approved={'id','kind','box','text','style','asset','source_region_id','source_box','line_id','run_order','semantic_group_id','rich_runs','rotation'}
            if set(obj)-approved:raise ValidationError(f'unsupported create fields: {sorted(set(obj)-approved)}')
            obj=validate_element(obj,allowed_assets=set((page.get('images') or {}).values()))
            if any(e['id']==obj['id'] for e in page['elements']):raise TransactionError('element ID already exists')
            if self.policy.permitted_elements is not None and obj['id'] not in self.policy.permitted_elements:
                raise PermissionDenied('new element ID outside authorized scope')
            if obj['kind'] in ('text','formula'):
                evid=require_text_evidence(self.policy,self.evidence,sid,obj['text'],op.get('evidence_id'))
                if evid:
                    if not obj.get('source_region_id') or obj['source_region_id']!=evid.evidence_id:
                        raise EvidenceError('new object must bind to the trusted source region')
                    # Box is allowed to shift for alignment but must overlap evidence region.
                    if _iou(obj['box'],evid.box)<0.08:
                        raise EvidenceError('new object does not overlap source text region')
                    for e in page['elements']:
                        if e.get('source_region_id')==evid.evidence_id:
                            raise TransactionError('source region already has a native element')
            page['elements'].append(obj)
        elif action=='delete_element':
            target=element(page,eid)
            if target.get('kind') in ('text','formula') and not self.policy.allow_text_changes:
                raise PermissionDenied('text deletion requires explicit content edit authorization')
            page['elements'].remove(target)
        elif action=='duplicate_element':
            target=element(page,eid)
            nid=op.get('new_id')
            if not isinstance(nid,str) or any(e['id']==nid for e in page['elements']):raise ValidationError('unique new_id required')
            new=cloned(target);new['id']=nid
            new.pop('source_region_id',None);new.pop('source_box',None)
            new['box']=validate_box(op.get('box',new['box']),allow_connector_endpoint=new['kind']=='line')
            if self.policy.permitted_elements is not None and nid not in self.policy.permitted_elements:
                raise PermissionDenied('duplicate ID outside authorized scope')
            # Free edit text clone is an explicit content-creation action.
            if target['kind'] in ('text','formula'):
                if not self.policy.allow_text_changes:
                    raise PermissionDenied('text duplication requires explicit content edit authorization')
            page['elements'].append(new)
        elif action=='reorder_element':
            target=element(page,eid)
            at=op.get('index')
            if isinstance(at,bool) or not isinstance(at,int) or not 0<=at<len(page['elements']):raise ValidationError('invalid z-order index')
            page['elements'].remove(target);page['elements'].insert(at,target)
        elif action=='set_background':
            page['background']=_hex(op.get('color'),'background')
        else:raise ValidationError(f'unsupported operation: {action}')

    def mark(self,tx_id:str,accepted:bool, *, qa:dict|None=None,host_approval:bool=False):
        """Host ONLY: model-facing tools intentionally cannot approve their own edits."""
        with self._lock:
            tx=next((t for t in self.transactions if t.tx_id==tx_id),None)
            if tx is None:raise TransactionError('unknown transaction')
            if tx.status!='pending':raise TransactionError('transaction already decided')
            if accepted and not host_approval and not (qa and qa.get('hard_checks_pass') is True and qa.get('visual_checks_pass') is True):
                raise TransactionError('accept needs trusted host QA verdict or explicit user approval')
            previous_status=tx.status
            previous_qa=cloned(tx.qa)
            previous_working=self.working
            tx.status='accepted' if accepted else 'rejected'
            tx.qa=cloned(qa or {})
            try:self._replay()
            except Exception:
                tx.status=previous_status;tx.qa=previous_qa;self.working=previous_working
                raise
            self._version+=1
            self._persist()
            return tx.to_dict()

    def _replay(self):
        """Rebuild all non-rejected edits; independent accepted changes survive rejection."""
        candidate=cloned(self.base)
        for tx in self.transactions:
            if tx.status=='rejected':continue
            for op in tx.operations:self._apply(candidate,tx.slide_id,op)
        validate_scene(candidate)
        self.working=candidate

    def undo(self):
        with self._lock:
            tx=next((t for t in reversed(self.transactions) if t.status!='rejected'),None)
            if tx is None:raise TransactionError('nothing to undo')
            # Undo last active transaction, including accepted edits, without editing original.
            old_tx=cloned(tx)
            tx.status='rejected';tx.qa={'undo':True}
            self._redo_history.append(old_tx)
            self._replay();self._version+=1;self._persist()
            return tx.to_dict()

    def redo(self):
        with self._lock:
            if not self._redo_history:raise TransactionError('nothing to redo')
            tx=self._redo_history.pop()
            current=next(t for t in self.transactions if t.tx_id==tx.tx_id)
            old=current.status;current.status=tx.status
            current.qa=cloned(tx.qa)
            try:self._replay()
            except Exception:
                current.status=old;self._redo_history.append(tx);raise
            self._version+=1;self._persist()
            return current.to_dict()

    def prepared_commit(self,current_project:dict)->dict:
        """Return accepted-only candidate; caller must perform atomic compare-and-save."""
        with self._lock:
            if signature(current_project)!=self.base_sha:raise RevisionConflict('source changed since session opened')
            final=cloned(self.base)
            accepted=[t for t in self.transactions if t.status=='accepted']
            for tx in accepted:
                for op in tx.operations:self._apply(final,tx.slide_id,op)
            validate_scene(final)
            return {'project':final,'base_signature':self.base_sha,
                    'candidate_signature':signature(final),'accepted_transaction_ids':[t.tx_id for t in accepted],
                    'unreviewed_transaction_ids':[t.tx_id for t in self.transactions if t.status=='pending'],
                    'has_changes':final!=self.base}

    def save_candidate(self,path:Path)->dict:
        """Write draft to NEW path; never overwrite the original project's project.json."""
        path=Path(path)
        if path.exists():raise TransactionError('candidate destination already exists')
        if path.name=='project.json':raise TransactionError('use candidate filename, never project.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(self.working,ensure_ascii=False,indent=2),encoding='utf-8')
        return {'path':str(path),'sha256':signature(self.working)}

    def _persist(self):
        if not self.root:return
        payload={'session_id':self.session_id,'base':self.base,'base_sha':self.base_sha,
                 'transactions':[t.to_dict() for t in self.transactions],
                 'policy':{'mode':self.policy.mode,'allowed_slides':sorted(self.policy.allowed_slides),
                           'permitted_elements':sorted(self.policy.permitted_elements) if self.policy.permitted_elements is not None else None,
                           'allowed_operations':sorted(self.policy.allowed_operations),
                           'allow_text_changes':self.policy.allow_text_changes},
                 'draft_version':self._version}
        target=self.root/'session.json';tmp=self.root/'session.json.tmp'
        tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        tmp.replace(target)

    @classmethod
    def resume(cls,path:Path,evidence:EvidenceRegistry|None=None):
        """Host must reattach trusted evidence registry; it is never reconstructed from model output."""
        p=Path(path).resolve();data=json.loads((p/'session.json').read_text(encoding='utf-8'))
        policy=PermissionPolicy(**{**data['policy'],
             'allowed_slides':frozenset(data['policy']['allowed_slides']),
             'permitted_elements':frozenset(data['policy']['permitted_elements']) if data['policy']['permitted_elements'] is not None else None,
             'allowed_operations':frozenset(data['policy']['allowed_operations'])})
        obj=cls(data['base'],policy,evidence,root=None,session_id=data['session_id'])
        if obj.base_sha!=data['base_sha']:raise RevisionConflict('saved base signature mismatch')
        obj.transactions=[Transaction(**t) for t in data['transactions']]
        obj._version=data['draft_version'];obj.root=p
        obj._replay()
        return obj

def _iou(a,b):
    ax,ay,aw,ah=a;bx,by,bw,bh=b
    ix=max(0,min(ax+aw,bx+bw)-max(ax,bx));iy=max(0,min(ay+ah,by+bh)-max(ay,by))
    inter=ix*iy
    return inter/max(1e-10,aw*ah+bw*bh-inter)
