"""Platform commercial position — P0 §11.1 group C.

    earned + adjustments − settled = outstanding

Four integer sums over the three authoritative commission tables, and nothing
derived from another. This is the *only* definition of commission outstanding in
the system, exactly as FIN-4 is the only definition of a customer balance.

It is deliberately separate from :mod:`app.billing.reporting`, which owns the
tenant-facing §11.1 groups A and B (business generated, billed value, collected,
outstanding). P3 does not touch those calculations: it reads the commission
tables the engine wrote, never re-derives commission from the ledger.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.commission.models import (
    CommissionAdjustment,
    CommissionEvent,
    CommissionSettlement,
)

__all__ = ["CommissionPosition", "commission_position", "serialize_position"]


@dataclass(frozen=True, slots=True)
class CommissionPosition:
    earned_minor: int
    adjustments_minor: int
    settled_minor: int
    outstanding_minor: int


def _sum(session: Session, column, *conditions) -> int:
    return int(
        session.execute(
            select(func.coalesce(func.sum(column), 0)).where(*conditions)
        ).scalar_one()
    )


def commission_position(session: Session, ctx) -> CommissionPosition:
    """The four figures for one tenant, computed on read.

    Nothing is cached and nothing is stamped onto a row when a settlement lands —
    which is precisely why a partial settlement needs no special handling and an
    over-settlement needs no error: both are just arithmetic over immutable rows.
    """
    earned = _sum(
        session,
        CommissionEvent.commission_minor,
        CommissionEvent.tenant_id == ctx.tenant_id,
    )
    adjustments = _sum(
        session,
        CommissionAdjustment.amount_minor,
        CommissionAdjustment.tenant_id == ctx.tenant_id,
    )
    settled = _sum(
        session,
        CommissionSettlement.amount_minor,
        CommissionSettlement.tenant_id == ctx.tenant_id,
    )
    return CommissionPosition(
        earned_minor=earned,
        adjustments_minor=adjustments,
        settled_minor=settled,
        outstanding_minor=earned + adjustments - settled,
    )


def serialize_position(position: CommissionPosition, ctx) -> dict[str, Any]:
    return {
        "tenant_id": str(ctx.tenant_id),
        **asdict(position),
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
    }
