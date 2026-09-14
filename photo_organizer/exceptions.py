class OrganizerError(Exception):
    """Base application error."""


class ValidationError(OrganizerError):
    """Raised when source or destination paths are unsafe."""


class ReuseNotSafeError(ValidationError):
    """Raised when a previous analysis cannot be safely reused."""


class OperationCancelled(OrganizerError):
    """Raised when the user cancels analysis."""


class CopyVerificationError(OrganizerError):
    """Raised when a copied file does not verify correctly."""
