"""Dispatch for one synchronised operation, and the four verdicts (P0 §7.3).

This module adds **no** business logic. It parses an envelope with the same
model the online route uses, runs the same domain command through the same
:func:`~app.sync.idempotency.execute_idempotent`, and translates whatever comes
back into one of ``APPLIED`` / ``DUPLICATE`` / ``REJECTED`` / ``CONFLICT``. SYN-8
is therefore true by construction rather than by inspection: there is no second
validation path to keep in step and no privileged offline branch.

Each operation is applied in **its own transaction** — ``execute_idempotent``
commits, and a domain failure rolls back only that operation — so one bad entry
in a batch cannot undo the entries beside it.

What is deliberately *not* caught here is an unexpected (non-``DomainError``)
exception. That is a defect, not a verdict about the operation: swallowing it as
``REJECTED`` would make a server bug permanently terminal for a device's queued
work. It propagates, the request fails, and the client keeps the whole batch
queued for retry — the only failure mode that cannot lose an entry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError, IdempotencyKeyReuseError
from app.service.commands import (
    RecordServiceInput,
    record_service,
    serialize_record,
)
from app.service.models import DailyServiceRecord, RecordStatus, Source
from app.sync.envelope import (
    SUPPORTED_OP_TYPES,
    ServiceOperationPayload,
    op_type_for_kind,
)
from app.sync.idempotency import execute_idempotent
from app.sync.models import SyncOperation
from app.tenancy.context import TenantContext

__all__ = ["SyncEnvelope", "SyncResult", "SyncStatus", "apply_operation"]


class SyncStatus:
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class SyncEnvelope:
    """One P0 §7.2 envelope, as received."""

    operation_id: uuid.UUID
    op_type: str
    payload: dict[str, Any]
    # Advisory only. Nothing authoritative reads a device clock (P0 §7.2, R4).
    client_created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    operation_id: uuid.UUID
    status: str
    entity: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    server_state: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "operation_id": str(self.operation_id),
            "status": self.status,
        }
        if self.entity is not None:
            out["entity"] = self.entity
        if self.error is not None:
            out["error"] = self.error
        if self.server_state is not None:
            out["server_state"] = self.server_state
        return out


def _rejected(envelope: SyncEnvelope, error: dict[str, Any]) -> SyncResult:
    return SyncResult(
        operation_id=envelope.operation_id, status=SyncStatus.REJECTED, error=error
    )


def _validation_error(detail: str, field_errors: dict[str, str]) -> dict[str, Any]:
    """The same envelope shape the HTTP validation handler produces (P0 §15)."""
    error: dict[str, Any] = {"code": "VALIDATION", "detail": detail}
    if field_errors:
        error["field_errors"] = field_errors
    return error


def _active_record(
    session: Session, ctx: TenantContext, customer_id: uuid.UUID, service_date: date
) -> DailyServiceRecord | None:
    return session.execute(
        select(DailyServiceRecord).where(
            DailyServiceRecord.tenant_id == ctx.tenant_id,
            DailyServiceRecord.customer_id == customer_id,
            DailyServiceRecord.service_date == service_date,
            DailyServiceRecord.status == RecordStatus.ACTIVE,
        )
    ).scalar_one_or_none()


def _registered_result(
    session: Session, ctx: TenantContext, operation_id: uuid.UUID
) -> dict[str, Any] | None:
    row = session.execute(
        select(SyncOperation).where(
            SyncOperation.tenant_id == ctx.tenant_id,
            SyncOperation.operation_id == operation_id,
        )
    ).scalar_one_or_none()
    return (row.result or None) if row is not None else None


def _conflict_state(
    session: Session,
    ctx: TenantContext,
    envelope: SyncEnvelope,
    exc: ConflictError,
    parsed: ServiceOperationPayload | None,
) -> dict[str, Any] | None:
    """The authoritative state an owner needs to judge the conflict (SYN-7).

    Never a merge and never a suggestion — just what the server holds, so the
    person can decide. ``None`` where the conflict is not about one row (a cycle
    that has not been rolled over, say); the error code carries that case.
    """
    if isinstance(exc, IdempotencyKeyReuseError):
        # SYN-14: the id is already bound to a different request. The state that
        # matters is what that first request actually did.
        return _registered_result(session, ctx, envelope.operation_id)
    if parsed is None:
        return None
    service_date = parsed.service_date or ctx.today
    record = _active_record(session, ctx, parsed.customer_id, service_date)
    return serialize_record(record, ctx) if record is not None else None


def apply_operation(
    session: Session, ctx: TenantContext, envelope: SyncEnvelope
) -> SyncResult:
    """Apply one envelope and return its verdict. Never raises a ``DomainError``."""
    if envelope.op_type not in SUPPORTED_OP_TYPES:
        return _rejected(
            envelope,
            _validation_error(
                f"unsupported op_type {envelope.op_type!r}",
                {"op_type": "not an operation this server accepts"},
            ),
        )

    try:
        parsed = ServiceOperationPayload.model_validate(envelope.payload)
    except ValidationError as exc:
        return _rejected(
            envelope,
            _validation_error(
                "request validation failed",
                {
                    ".".join(str(p) for p in err["loc"]) or "payload": err["msg"]
                    for err in exc.errors()
                },
            ),
        )

    # The op type and the kind are two statements of the same fact; a disagreement
    # is a client defect and is refused rather than silently resolved in favour of
    # one of them.
    if envelope.op_type != op_type_for_kind(parsed.kind):
        return _rejected(
            envelope,
            _validation_error(
                f"op_type {envelope.op_type!r} does not match kind {parsed.kind!r}",
                {"op_type": "does not match the payload kind"},
            ),
        )

    data = RecordServiceInput(
        customer_id=parsed.customer_id,
        kind=parsed.kind,
        quantity=parsed.quantity,
        service_date=parsed.service_date,
        input_method=parsed.input_method,
        # Transport provenance. The *only* thing that differs from the online
        # call, and it changes no behaviour (P0 §6).
        source=Source.SYNC,
    )
    try:
        outcome = execute_idempotent(
            session,
            ctx,
            operation_id=envelope.operation_id,
            op_type=envelope.op_type,
            # Hash the canonical parsed payload, not the raw dict: an omitted
            # optional and its default are the same request, and must not read as
            # two different ones under a single operation_id.
            payload=parsed.model_dump(mode="json"),
            perform=lambda: record_service(
                session, ctx, data, operation_id=envelope.operation_id
            ),
        )
    except ConflictError as exc:
        session.rollback()
        return SyncResult(
            operation_id=envelope.operation_id,
            status=SyncStatus.CONFLICT,
            error=exc.to_envelope()["error"],
            server_state=_conflict_state(session, ctx, envelope, exc, parsed),
        )
    except DomainError as exc:
        session.rollback()
        return _rejected(envelope, exc.to_envelope()["error"])

    return SyncResult(
        operation_id=envelope.operation_id,
        status=outcome.status,
        entity=outcome.result,
    )
