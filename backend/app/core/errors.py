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
    "CyclePeriodNotEndedError",
    "CycleRolloverRequiredError",
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


class CyclePeriodNotEndedError(ValidationFailed):
    """A billing cycle may not be closed until its ``period_end`` has passed.

    ``period_end`` is **inclusive**, so the period is still running throughout
    that day: business events dated on `period_end` must stay eligible to post to
    the cycle no matter what time of day somebody attempts to close it. The
    earliest valid close is therefore ``business_date > period_end``.

    Closing sooner would end the period somewhere other than where the tenant's
    configuration says it ends, and the days between the close and the real
    boundary would be billed in the *following* cycle. Neither the shortened
    period nor that carry-over was ever a client decision, so V1 refuses instead
    of inventing one. An explicit early-close feature, if it is ever wanted, is a
    separate design with its own product decision — not an override flag here.
    """

    code = "CYCLE_PERIOD_NOT_ENDED"


class ConflictError(DomainError):
    status_code = 409
    code = "CONFLICT"


class CycleRolloverRequiredError(ConflictError):
    """The tenant's only OPEN cycle ended before today, so nothing may post.

    An expired cycle that is still open means the rollover has not happened yet.
    Posting a new event into it would file today's business under a period that
    has already ended — a mis-stated bill, not a late one — so the write fails
    closed and asks for the proper close operation instead.

    Deliberately **not** resolved by auto-closing the stale cycle from inside a
    service or payment command: closing a cycle issues statements, and issuing a
    customer's bill as a side effect of somebody recording a bottle of milk is
    not a decision a write command gets to make. A scheduled rollover calls the
    real close operation.
    """

    code = "CYCLE_ROLLOVER_REQUIRED"


class ServiceAlreadyRecordedError(ConflictError):
    """SYN-4: the (tenant, customer, service_date) active slot is taken."""

    code = "SERVICE_ALREADY_RECORDED"


class IdempotencyKeyReuseError(ConflictError):
    """SYN-14: same operation_id replayed with a different request payload.

    Fails closed. Returning the earlier result would silently discard the new
    request; applying it would break the one-operation-one-effect guarantee.
    """

    code = "IDEMPOTENCY_KEY_REUSE"
