"""Host-established evidence and scoped user authorization, not model self-attestation."""
from __future__ import annotations
from dataclasses import dataclass, field
from .errors import EvidenceError, PermissionDenied, ValidationError
from .scene import element, slide

@dataclass(frozen=True)
class PermissionPolicy:
    mode: str = 'restore'                 # restore | free
    allowed_slides: frozenset[str] = field(default_factory=frozenset)
    permitted_elements: frozenset[str]|None = None  # None means all on permitted slides
    allowed_operations: frozenset[str] = field(default_factory=lambda:frozenset({
        'set_properties','set_rich_runs','set_geometry','nudge_element','align_elements',
        'distribute_elements','create_element','delete_element','duplicate_element',
        'reorder_element','set_background'}))
    allow_text_changes: bool = False     # free mode needs explicit authorization too

    def __post_init__(self):
        if self.mode not in ('restore','free'):raise ValidationError('unknown edit mode')
        if not self.allowed_slides:raise ValidationError('allowed_slides required')

@dataclass(frozen=True)
class TrustedEvidence:
    """Created by host after OCR/Clean/Design and user verification; model gets IDs only."""
    evidence_id: str
    slide_id: str
    text: str
    box: tuple[float,float,float,float]
    kind: str = 'missing_text'
    source_hash: str = ''
    verified: bool = False

class EvidenceRegistry:
    def __init__(self, entries:list[TrustedEvidence]|None=None):
        self._entries={e.evidence_id:e for e in (entries or [])}

    def get(self,eid:str, sid:str, text:str|None=None)->TrustedEvidence:
        e=self._entries.get(eid)
        if not e or e.slide_id!=sid or not e.verified:
            raise EvidenceError('host-verified evidence ID required')
        if text is not None and e.text!=text:
            raise EvidenceError('proposed text differs from evidence exactly, including whitespace')
        return e

    def add_by_host(self,entry:TrustedEvidence):
        """Call only from trusted OCR/asset validation or explicit human-confirmation route."""
        self._entries[entry.evidence_id]=entry


def enforce_scope(policy:PermissionPolicy,project:dict,sid:str,operation:str,*,eid:str|None=None,mutating_text=False):
    if sid not in policy.allowed_slides: raise PermissionDenied(f'slide not authorized: {sid}')
    if operation not in policy.allowed_operations:raise PermissionDenied(f'operation not authorized: {operation}')
    if eid and policy.permitted_elements is not None and eid not in policy.permitted_elements:
        raise PermissionDenied(f'element outside authorized scope: {eid}')
    if mutating_text and policy.mode=='free' and not policy.allow_text_changes:
        raise PermissionDenied('free mode text edits require explicit user text authorization')
    if mutating_text and policy.mode=='restore' and not policy.allow_text_changes:
        raise PermissionDenied('restore mode text mutation requires verified source evidence')
    slide(project,sid)


def require_text_evidence(policy:PermissionPolicy,registry:EvidenceRegistry, sid:str, text:str, evid:str|None):
    if policy.mode=='restore' and not policy.allow_text_changes:
        return registry.get(evid or '',sid,text)
    if not policy.allow_text_changes:
        raise PermissionDenied('content creation/change not authorized in free mode')
    return None
