"""Domain errors and the single machine-readable API error envelope.

P0 §15 freezes the shape:

    {"error": {"code": "...", "detail": "...", "field_errors": {...}}}
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DomainError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ValidationFailed",
    "ConflictError",
    "ServiceAlreadyRecordedError",
    "IdempotencyKeyReuseError",
]


class DomainError(Exception):
    """Base class for errors that map onto a stable API error code."""

    status_code: int = 400
    code: str = "DOMAIN_ERROR"

    def __init__(
        self,
        detail: str,
        *,
        code: str | None = None,
        field_errors: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        if code is not None:
            self.code = code
        self.field_errors = field_errors or {}
        self.extra = extra or {}

    def to_envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "detail": self.detail}
        if self.field_errors:
            error["field_errors"] = self.field_errors
        error.update(self.extra)
        return {"error": error}


class AuthenticationError(DomainError):
    status_code = 401
    code = "UNAUTHENTICATED"


class PermissionDeniedError(DomainError):
    status_code = 403
    code = "PERMISSION_DENIED"


class NotFoundError(DomainError):
    """Also used for cross-tenant access: SEC-4 requires 404, never 403."""

    status_code = 404
    code = "NOT_FOUND"


class ValidationFailed(DomainError):
    status_code = 422
    code = "VALIDATION"


class ConflictError(DomainError):
    status_code = 409
    code = "CONFLICT"


class ServiceAlreadyRecordedError(ConflictError):
    """SYN-4: the (tenant, customer, service_date) active slot is taken."""

    code = "SERVICE_ALREADY_RECORDED"


class IdempotencyKeyReuseError(ConflictError):
    """SYN-14: same operation_id replayed with a different request payload.

    Fails closed. Returning the earlier result would silently discard the new
    request; applying it would break the one-operation-one-effect guarantee.
    """

    code = "IDEMPOTENCY_KEY_REUSE"
