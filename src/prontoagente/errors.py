"""Domain errors mapped by the HTTP layer."""


class DomainError(Exception):
    """Base class carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConflictError(DomainError):
    """The request conflicts with persisted state or idempotency history."""


class NotFoundError(DomainError):
    """The requested workflow does not exist."""


class AuthenticationError(DomainError):
    """The request does not carry a valid API credential."""


class AuthorizationError(DomainError):
    """The authenticated principal lacks an allowed role."""
