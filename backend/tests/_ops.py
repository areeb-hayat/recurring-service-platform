"""Shared helpers for the P2 financial-engine suites.

Every mutation goes through :func:`app.sync.idempotency.execute_idempotent`, the
same way the API layer drives it, so these tests exercise the real transaction
boundary rather than a shortcut around it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing.models import LedgerEntry
from app.core.clock import FixedClock
from app.core.ids import uuid7
from app.identity.models import Role
from app.payments.commands import (
    RecordPaymentInput,
    VoidPaymentInput,
    record_payment,
    void_payment,
)
from app.service.commands import (
    CorrectServiceInput,
    RecordServiceInput,
    VoidServiceInput,
    correct_service,
    record_service,
    void_service,
)
from app.tenancy.context import Principal, TenantContext


def ctx_at(fixture, when: datetime) -> TenantContext:
    """The same tenant, seen from a different instant.

    Cycles are calendar ranges, so testing carry-forward across three months means
    moving the clock, not inventing a second tenant.
    """
    return TenantContext.build(
        principal=Principal(
            user_id=fixture.owner.id,
            role=Role.OWNER_ADMIN,
            scope="TENANT",
            tenant_id=fixture.tenant.id,
        ),
        tenant=fixture.tenant,
        clock=FixedClock(when),
    )


def auth_at(fixture, settings, when: datetime) -> dict[str, str]:
    """Bearer headers valid at ``when``.

    Access-token expiry is checked against the *injected* clock (P1), so a test
    that advances the clock to a period boundary must also present a token issued
    there — otherwise it gets a truthful 401 instead of the behaviour under test.
    """
    from app.core.security import encode_access_token

    token = encode_access_token(
        secret=settings.jwt_secret,
        user_id=str(fixture.owner.id),
        scope="TENANT",
        role=Role.OWNER_ADMIN,
        tenant_id=str(fixture.tenant.id),
        issued_at=when,
        expires_in_minutes=60,
    )
    return {"Authorization": f"Bearer {token}"}


def _run(db: Session, ctx, op_type: str, payload: dict, perform):
    from app.sync.idempotency import execute_idempotent

    operation_id = payload.pop("_operation_id", None) or uuid7()
    return execute_idempotent(
        db,
        ctx,
        operation_id=operation_id,
        op_type=op_type,
        payload={**payload, "_op": str(operation_id)},
        perform=lambda: perform(operation_id),
    )


def do_record(db, ctx, customer, **kw):
    op = kw.pop("operation_id", None)
    data = RecordServiceInput(customer_id=customer.id, **kw)
    return _run(
        db,
        ctx,
        "service.record",
        {"customer_id": str(customer.id), "_operation_id": op},
        lambda operation_id: record_service(db, ctx, data, operation_id=operation_id),
    )


def do_correct(db, ctx, record_id, **kw):
    op = kw.pop("operation_id", None)
    data = CorrectServiceInput(**kw)
    return _run(
        db,
        ctx,
        "service.correct",
        {"record_id": str(record_id), "_operation_id": op},
        lambda operation_id: correct_service(
            db, ctx, record_id, data, operation_id=operation_id
        ),
    )


def do_void(db, ctx, record_id, reason="void", operation_id=None):
    return _run(
        db,
        ctx,
        "service.void",
        {"record_id": str(record_id), "_operation_id": operation_id},
        lambda op: void_service(
            db, ctx, record_id, VoidServiceInput(reason=reason), operation_id=op
        ),
    )


def do_pay(db, ctx, customer, amount_minor, **kw):
    op = kw.pop("operation_id", None)
    data = RecordPaymentInput(customer_id=customer.id, amount_minor=amount_minor, **kw)
    return _run(
        db,
        ctx,
        "payment.record",
        {"customer_id": str(customer.id), "_operation_id": op},
        lambda operation_id: record_payment(db, ctx, data, operation_id=operation_id),
    )


def do_void_payment(db, ctx, payment_id, reason="void", operation_id=None):
    return _run(
        db,
        ctx,
        "payment.void",
        {"payment_id": str(payment_id), "_operation_id": operation_id},
        lambda op: void_payment(
            db, ctx, payment_id, VoidPaymentInput(reason=reason), operation_id=op
        ),
    )


def do_close_cycle(db, ctx, cycle_id, operation_id=None):
    return _run(
        db,
        ctx,
        "billing.close_cycle",
        {"cycle_id": str(cycle_id), "_operation_id": operation_id},
        lambda op: _close(db, ctx, cycle_id, op),
    )


def _close(db, ctx, cycle_id, operation_id):
    from app.billing.cycles import close_cycle

    return close_cycle(db, ctx, cycle_id, operation_id=operation_id)


def day_after(day):
    """The first business date on which a cycle ending on ``day`` may close."""
    from datetime import timedelta

    return day + timedelta(days=1)


def close_after_period_end(db, fixture, cycle, operation_id=None):
    """Close a cycle the only way V1 permits: after its ``period_end`` has passed.

    ``period_end`` is inclusive, so the earliest valid close is the following
    day. A test that wants a closed cycle therefore moves the clock past the
    boundary rather than closing on or inside the period.
    """
    ctx = ctx_at(fixture, _noon_utc(day_after(cycle.period_end)))
    return ctx, do_close_cycle(db, ctx, cycle.id, operation_id=operation_id)


def _noon_utc(day):
    """Midday UTC on ``day`` — comfortably inside the same date in Asia/Karachi."""
    from datetime import datetime, timezone

    return datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)


def entries(db, ctx, customer_id: uuid.UUID) -> list[LedgerEntry]:
    return list(
        db.execute(
            select(LedgerEntry)
            .where(
                LedgerEntry.tenant_id == ctx.tenant_id,
                LedgerEntry.customer_id == customer_id,
            )
            .order_by(LedgerEntry.created_at, LedgerEntry.id)
        )
        .scalars()
        .all()
    )
