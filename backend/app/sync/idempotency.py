"""The reusable idempotent-mutation mechanism.

Every P1 mutation goes through :func:`execute_idempotent`. P5's bulk sync
endpoint reuses this same function rather than reimplementing the guarantee —
that is why it takes a plain callable and knows nothing about HTTP.

Guarantees (SYN-1, SYN-2, SYN-3, SYN-13, SYN-14):

* first request  -> effect + register row committed **in one transaction**
* exact replay   -> nothing created, no side effect, same logical result
* replay with a different payload -> refused (SYN-14), never silently resolved

It is also where SYN-10's commit-order boundary is taken, for the operations that
write an entity the change feed carries — see :mod:`app.sync.serialization`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyKeyReuseError
from app.sync.models import OperationStatus, SyncOperation
from app.sync.serialization import FEED_WRITING_OP_TYPES, serialize_feed_writes
from app.tenancy.context import PlatformContext, TenantContext

__all__ = ["OperationOutcome", "compute_request_hash", "execute_idempotent"]


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    status: Literal["APPLIED", "DUPLICATE"]
    result: dict[str, Any]
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None

    @property
    def was_replay(self) -> bool:
        return self.status == OperationStatus.DUPLICATE


def compute_request_hash(op_type: str, payload: dict[str, Any]) -> str:
    """Stable hash of the semantic request.

    ``sort_keys`` + ``default=str`` makes the hash independent of key order and
    of Decimal/UUID/date repr, so an identical retry hashes identically while a
    genuinely different payload does not.
    """
    canonical = json.dumps(
        {"op_type": op_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_register_row(
    session: Session, tenant_id: uuid.UUID, operation_id: uuid.UUID
) -> SyncOperation | None:
    return session.execute(
        select(SyncOperation).where(
            SyncOperation.tenant_id == tenant_id,
            SyncOperation.operation_id == operation_id,
        )
    ).scalar_one_or_none()


def _replay(existing: SyncOperation, request_hash: str) -> OperationOutcome:
    if existing.request_hash != request_hash:
        # SYN-14. Returning the old result would silently discard this request;
        # applying it would break one-operation-one-effect. Fail closed.
        raise IdempotencyKeyReuseError(
            "operation_id was already used for a different request; "
            "generate a new operation_id for a new operation",
            extra={"operation_id": str(existing.operation_id)},
        )
    return OperationOutcome(
        status=OperationStatus.DUPLICATE,
        result=existing.result or {},
        entity_type=existing.entity_type,
        entity_id=existing.entity_id,
    )


def execute_idempotent(
    session: Session,
    ctx: TenantContext | PlatformContext,
    *,
    operation_id: uuid.UUID,
    op_type: str,
    payload: dict[str, Any],
    perform: Callable[[], tuple[dict[str, Any], str, uuid.UUID]],
) -> OperationOutcome:
    """Run ``perform`` exactly once for ``(tenant_id, operation_id)``.

    ``perform`` returns ``(result_dict, entity_type, entity_id)`` and must do all
    of its work in the caller's session without committing. This function owns
    the transaction boundary.

    ``ctx`` may be a :class:`~app.tenancy.context.PlatformContext`: a platform
    command targeting a tenant registers under that tenant's ``(tenant_id,
    operation_id)`` key, so P3 reuses this register rather than building a second
    idempotency system beside it.
    """
    request_hash = compute_request_hash(op_type, payload)

    # SYN-10, and it must come first.
    #
    # Before the register is claimed, not after: a transaction that took this
    # lock while already holding an uncommitted register row would wait on the
    # lock while the lock's holder waited on that row's unique index — a deadlock
    # between two *identical* envelopes, which is exactly the case A-SYN-1/2
    # fires five of at once. One order for everybody: lock, then register, then
    # effect.
    if op_type in FEED_WRITING_OP_TYPES:
        serialize_feed_writes(session, ctx.tenant_id)

    existing = _load_register_row(session, ctx.tenant_id, operation_id)
    if existing is not None:
        return _replay(existing, request_hash)

    # Claim (tenant_id, operation_id) BEFORE running the effect, so the register's
    # unique index — not whichever business constraint the effect happens to touch
    # — is the serialization point for concurrent replays.
    #
    # Doing the effect first would let a business constraint fire earlier: five
    # concurrent identical envelopes would collide on the daily-record active-day
    # index and surface as CONFLICT, when an identical replay must be DUPLICATE.
    # Result fields are filled in below, inside the same transaction, so a
    # half-populated register row is never visible to anyone.
    register = SyncOperation(
        tenant_id=ctx.tenant_id,
        operation_id=operation_id,
        user_id=ctx.user_id,
        op_type=op_type,
        request_hash=request_hash,
        status=OperationStatus.APPLIED,
        result=None,
    )
    session.add(register)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        # Another transaction won the race. Postgres blocked this insert until
        # that one committed, so its result is readable now.
        if not _is_operation_id_conflict(exc):
            raise
        winner = _load_register_row(session, ctx.tenant_id, operation_id)
        if winner is None:  # pragma: no cover - only if the winner rolled back
            raise
        return _replay(winner, request_hash)

    result, entity_type, entity_id = perform()

    register.result = result
    register.entity_type = entity_type
    register.entity_id = entity_id

    # SYN-3: effect and register commit together, or neither does.
    session.commit()

    return OperationOutcome(
        status=OperationStatus.APPLIED,
        result=result,
        entity_type=entity_type,
        entity_id=entity_id,
    )


def _is_operation_id_conflict(exc: IntegrityError) -> bool:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint:
        return constraint == "uq_sync_operation_tenant_id_operation_id"
    return "uq_sync_operation_tenant_id_operation_id" in str(exc.orig)
