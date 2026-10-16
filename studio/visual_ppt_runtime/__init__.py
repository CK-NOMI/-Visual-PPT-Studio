"""Visual PPT Studio Editing Runtime v0.2: pure, isolated, transactional edits."""
from .errors import (EditingError,ValidationError,PermissionDenied,RevisionConflict,
                     EvidenceError,RendererUnavailable,TransactionError)
from .permissions import PermissionPolicy,TrustedEvidence,EvidenceRegistry
from .transactions import EditingSession,Transaction
from .tools import EditingTools,RenderOutcome,TOOLS
from .agent import DeepSeekChatProvider,VisualEditingAgent
from .render import StudioLibreOfficeRenderer
from .scene import signature,validate_scene,groups_for_page,points_to_box,box_to_points
from .export_bridge import build_with_extensions,patch_pptx_file
__all__=['EditingSession','Transaction','EditingTools','RenderOutcome','TOOLS',
         'PermissionPolicy','TrustedEvidence','EvidenceRegistry','DeepSeekChatProvider',
         'VisualEditingAgent','StudioLibreOfficeRenderer','signature','validate_scene',
         'groups_for_page','points_to_box','box_to_points','build_with_extensions','patch_pptx_file','EditingError','ValidationError','PermissionDenied','RevisionConflict',
         'EvidenceError','RendererUnavailable','TransactionError']
