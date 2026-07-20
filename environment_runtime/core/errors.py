class RuntimeErrorBase(Exception):
    """Base exception for the runtime."""


class NotFoundError(RuntimeErrorBase):
    """Raised when a resource does not exist."""


class ConflictError(RuntimeErrorBase):
    """Raised when a resource update conflicts with current state."""


class ValidationError(RuntimeErrorBase):
    """Raised when domain validation fails."""


class SecurityError(RuntimeErrorBase):
    """Raised when a security boundary is violated."""


class ProviderError(RuntimeErrorBase):
    """Raised when a provider operation fails."""
