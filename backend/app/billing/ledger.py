"""Ledger posting and balance derivation.

The only module that writes :class:`LedgerEntry`, and it only ever appends
(FIN-12, AUD-7). There is no update and no delete function here by design.

Balances are computed, never stored (FIN-4, P0 §6 "deliberately not created:
a balance cache table").
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.billing.models import EntryKind, LedgerEntry, SourceType
from app.tenancy.context import TenantContext

__all__ = ["post_entry", "post_service_charge", "post_service_adjustment", "outstanding_minor"]


def post_entry(
    session: Session,
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID,
    entry_kind: str,
    amount_minor: int,
    occurred_on,
    source_type: str,
    source_id: uuid.UUID,
) -> LedgerEntry | None:
    """Append one ledger entry, or none if the amount is zero.

    A zero-value entry carries no financial meaning and is refused by the
    ``amount_non_zero`` CHECK, so this returns ``None`` instead. Callers must
    treat "no entry" as a legitimate outcome — a correction that changes nothing,
    or a voided SKIP, genuinely has no ledger effect.
    """
    if amount_minor == 0:
        return None
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise TypeError("amount_minor must be an int in minor units")

    entry = LedgerEntry(
        tenant_id=ctx.tenant_id,
        customer_id=customer_id,
        entry_kind=entry_kind,
        amount_minor=amount_minor,
        occurred_on=occurred_on,
        posting_cycle_id=None,  # resolved by P2 when billing_cycle exists
        source_type=source_type,
        source_id=source_id,
        created_by_user_id=ctx.user_id,
    )
    session.add(entry)
    return entry


def post_service_charge(session: Session, ctx: TenantContext, record) -> LedgerEntry | None:
    """CHARGE for an accepted SERVICE record. A SKIP posts nothing (FIN-7)."""
    return post_entry(
        session,
        ctx,
        customer_id=record.customer_id,
        entry_kind=EntryKind.CHARGE,
        amount_minor=record.charge_minor,
        occurred_on=record.service_date,
        source_type=SourceType.DAILY_SERVICE_RECORD,
        source_id=record.id,
    )


def post_service_adjustment(
    session: Session,
    ctx: TenantContext,
    *,
    customer_id: uuid.UUID,
    amount_minor: int,
    occurred_on,
    source_id: uuid.UUID,
) -> LedgerEntry | None:
    """Service-origin ADJUSTMENT (P0 §5.3).

    ``source_type`` is ``daily_service_record``, which is what makes this
    adjustment count towards business generated and not towards collections.
    """
    return post_entry(
        session,
        ctx,
        customer_id=customer_id,
        entry_kind=EntryKind.ADJUSTMENT,
        amount_minor=amount_minor,
        occurred_on=occurred_on,
        source_type=SourceType.DAILY_SERVICE_RECORD,
        source_id=source_id,
    )


def outstanding_minor(session: Session, ctx: TenantContext, customer_id: uuid.UUID) -> int:
    """FIN-4: the one and only definition of a balance.

    Computed on demand from the append-only ledger. Nothing caches this.
    """
    total = session.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
            LedgerEntry.tenant_id == ctx.tenant_id,
            LedgerEntry.customer_id == customer_id,
        )
    ).scalar_one()
    return int(total)
