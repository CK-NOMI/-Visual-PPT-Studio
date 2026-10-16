"""Public, actionable exceptions (do not leak API tokens)."""
class EditingError(Exception):
    pass

class ValidationError(EditingError):
    pass

class PermissionDenied(EditingError):
    pass

class RevisionConflict(EditingError):
    pass

class EvidenceError(EditingError):
    pass

class RendererUnavailable(EditingError):
    pass

class TransactionError(EditingError):
    pass
