"""Commission tables (P0 §6, §11).

Four tables and nothing else: the plan is data, the earning event is immutable
history, the adjustment is the only way history moves, and the settlement is an
independent additive record of money paid.

    commission_outstanding = Σ commission_event.commission_minor
                           + Σ commission_adjustment.amount_minor
                           − Σ commission_settlement.amount_minor

**No settlement reference exists on any earning row** (COM-11). A nullable
``settlement_id`` cannot express a half-settled event or an event spanning two
settlements, which is exactly why V1 settles in aggregate. If per-event
allocation is ever required it arrives as a dedicated allocation table, never as
a column retrofitted onto immutable history.

**No ``row_version``** (P0 §6). Commission is platform-only and appears nowhere
in the client's offline snapshot, so versioning these rows would make them sync
entities by accident.

``commission_event``, ``commission_adjustment`` and ``commission_settlement`` are
immutable, enforced in the database by an UPDATE/DELETE trigger rather than by
the absence of a route (AUD-1, COM-3, COM-6). ``commission_plan`` is *not*: its
open-ended ``effective_to`` is closed when a successor plan begins, which is the
single permitted lifecycle transition on it.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, business_day, uuid_fk, uuid_nullable, uuid_pk, utc_timestamp
from app.core.ids import new_id

__all__ = [
    "CommissionBasis",
    "CommissionSourceType",
    "CommissionPlan",
    "CommissionEvent",
    "CommissionAdjustment",
    "CommissionSettlement",
]


class CommissionBasis:
    """The four frozen bases (P0 §11). There is no fifth.

    Each names the §11.1 derivation its monetary base follows, which is what
    keeps a `COLLECTED_VALUE` plan reacting to a payment void while a
    `RECORDED_VALUE` plan ignores that same void.
    """

    RECORDED_VALUE = "RECORDED_VALUE"  # accepted service value (FIN-14)
    BILLED_VALUE = "BILLED_VALUE"  # issued statement value (FIN-15)
    COLLECTED_VALUE = "COLLECTED_VALUE"  # accepted payment value (FIN-16)
    PER_EVENT = "PER_EVENT"  # fixed amount per accepted SERVICE record

    ALL = (RECORDED_VALUE, BILLED_VALUE, COLLECTED_VALUE, PER_EVENT)
    # The three bases whose commission is a rate applied to a monetary base. The
    # remaining one, PER_EVENT, is a fixed amount and carries no rate at all.
    RATED = (RECORDED_VALUE, BILLED_VALUE, COLLECTED_VALUE)


class CommissionSourceType:
    """The business fact an event or adjustment derives from.

    The first two values are deliberately identical to
    :class:`app.billing.models.SourceType`, so a commission row and the ledger
    row describing the same fact carry the same source identity. A test asserts
    they have not drifted apart.
    """

    DAILY_SERVICE_RECORD = "daily_service_record"
    PAYMENT = "payment"
    STATEMENT = "statement"

    ALL = (DAILY_SERVICE_RECORD, PAYMENT, STATEMENT)


_SOURCE_TYPE_SQL = "('daily_service_record','payment','statement')"
_BASIS_SQL = "('RECORDED_VALUE','BILLED_VALUE','COLLECTED_VALUE','PER_EVENT')"


class CommissionPlan(Base):
    """The commercial deal, as data (COM-1).

    Nothing about rate, basis or currency is hard-coded anywhere; all three are
    read from the plan effective for the business date of the source fact.

    Exactly one of ``rate_bp`` / ``fixed_amount_minor`` is set, and which one is
    decided by the basis: a rate for the three value bases, a fixed amount for
    ``PER_EVENT``. Both halves are one CHECK, so a plan that is configured for
    neither — or for both — cannot exist even by direct SQL.

    Effective ranges never overlap for a tenant. That is an ``EXCLUDE`` constraint
    over ``(tenant_id, daterange(effective_from, effective_to, '[]'))``, declared
    in the migration because SQLAlchemy's ``ExcludeConstraint`` cannot express the
    range expression portably. At most one plan is therefore effective on any
    date, which is what makes plan resolution a lookup rather than a policy.
    """

    __tablename__ = "commission_plan"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))

    basis: Mapped[str] = mapped_column(String(24), nullable=False)
    rate_bp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fixed_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    effective_from: Mapped[business_day]
    # NULL means open-ended. Closed only when a successor plan begins.
    effective_to: Mapped[date | None] = mapped_column(nullable=True)

    created_by_user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        # Composite target for (tenant_id, plan_id) on commission_event.
        UniqueConstraint("tenant_id", "id", name="uq_commission_plan_tenant_id_id"),
        Index(
            "ix_commission_plan_tenant_id_effective_from", "tenant_id", "effective_from"
        ),
        CheckConstraint(f"basis IN {_BASIS_SQL}", name="basis_valid"),
        CheckConstraint(
            "rate_bp IS NULL OR rate_bp BETWEEN 0 AND 10000", name="rate_bp_range"
        ),
        CheckConstraint(
            "fixed_amount_minor IS NULL OR fixed_amount_minor >= 0",
            name="fixed_amount_non_negative",
        ),
        # Exactly one of the two, and the right one for the basis.
        CheckConstraint(
            "(basis = 'PER_EVENT' AND fixed_amount_minor IS NOT NULL AND rate_bp IS NULL)"
            " OR (basis <> 'PER_EVENT' AND rate_bp IS NOT NULL "
            "AND fixed_amount_minor IS NULL)",
            name="exactly_one_term_for_basis",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
    )


class CommissionEvent(Base):
    """Earned commission history — immutable (COM-3).

    Every term in force is copied onto the row at creation, so a renegotiated
    rate cannot rewrite what was already earned. ``plan_id`` records *which* plan
    produced it; the snapshots are what the arithmetic actually used, and the two
    are never re-read from each other.

    ``(tenant_id, source_type, source_id)`` is unique (COM-5): one source fact
    yields at most one earning event, which is what makes a replayed operation
    incapable of creating a second one.

    Created only inside the transaction that accepts the source business event
    (COM-2). No offline device ever creates one.
    """

    __tablename__ = "commission_event"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    plan_id: Mapped[uuid_fk]

    basis_snapshot: Mapped[str] = mapped_column(String(24), nullable=False)
    rate_bp_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fixed_amount_minor_snapshot: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid_fk]

    base_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    commission_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_on: Mapped[business_day]
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_commission_event_tenant_id_id"),
        # SEC-2: an event can only cite a plan owned by its own tenant.
        ForeignKeyConstraint(
            ["tenant_id", "plan_id"],
            ["commission_plan.tenant_id", "commission_plan.id"],
            name="fk_commission_event_tenant_id_plan_id",
        ),
        # COM-5: one source fact, at most one earning event.
        UniqueConstraint(
            "tenant_id",
            "source_type",
            "source_id",
            name="uq_commission_event_tenant_id_source_type_source_id",
        ),
        Index("ix_commission_event_tenant_id_occurred_on", "tenant_id", "occurred_on"),
        CheckConstraint(f"basis_snapshot IN {_BASIS_SQL}", name="basis_snapshot_valid"),
        CheckConstraint(
            f"source_type IN {_SOURCE_TYPE_SQL}", name="source_type_valid"
        ),
        CheckConstraint(
            "rate_bp_snapshot IS NULL OR rate_bp_snapshot BETWEEN 0 AND 10000",
            name="rate_bp_snapshot_range",
        ),
        # The snapshot carries the term its basis actually used, and only that one.
        CheckConstraint(
            "(basis_snapshot = 'PER_EVENT' AND fixed_amount_minor_snapshot IS NOT NULL "
            "AND rate_bp_snapshot IS NULL)"
            " OR (basis_snapshot <> 'PER_EVENT' AND rate_bp_snapshot IS NOT NULL "
            "AND fixed_amount_minor_snapshot IS NULL)",
            name="exactly_one_snapshot_for_basis",
        ),
    )


class CommissionAdjustment(Base):
    """The only way earned history moves (COM-4) — immutable, signed, traceable.

    A correction, void or reversal of a commissionable source never rewrites the
    original event; it appends one adjustment computed with that event's
    **original snapshotted terms**, linked to the event and carrying its own
    source reference.

    ``(tenant_id, source_type, source_id)`` is unique (COM-5). The source
    identity is the record or payment whose accepted life ended, which is the
    same identity the compensating *ledger* entry carries — and a document can
    end its life exactly once, so the key can never collide.
    """

    __tablename__ = "commission_adjustment"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    commission_event_id: Mapped[uuid_fk]

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid_fk]

    # The actor whose accepted correction caused it. NULL for none: an adjustment
    # is a consequence of a business event, not itself an authored document.
    created_by_user_id: Mapped[uuid_nullable] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "commission_event_id"],
            ["commission_event.tenant_id", "commission_event.id"],
            name="fk_commission_adjustment_tenant_id_commission_event_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "source_type",
            "source_id",
            name="uq_commission_adjustment_tenant_id_source_type_source_id",
        ),
        Index(
            "ix_commission_adjustment_tenant_id_commission_event_id",
            "tenant_id",
            "commission_event_id",
        ),
        CheckConstraint(
            f"source_type IN {_SOURCE_TYPE_SQL}", name="source_type_valid"
        ),
        CheckConstraint("length(btrim(reason)) > 0", name="reason_not_blank"),
    )


class CommissionSettlement(Base):
    """Money actually settled between the platform and the tenant (COM-6).

    Independent and append-only. It references no earning event, stamps nothing
    on one, and rewrites nothing — which is precisely what makes partial
    settlement representable: earn 1000, settle 400, settle 600 leaves three
    immutable rows and an outstanding that moved 1000 → 600 → 0.

    ``amount_minor > 0``: a settlement is money that moved from the tenant to the
    platform. A negative row would be a commission adjustment wearing a
    settlement's clothes — moving outstanding with no snapshotted terms, no link
    to an earning event and no source fact, which is exactly the traceability
    COM-4 exists to guarantee. Commission only ever moves through an adjustment.

    That is a different question from the sign of the *aggregate*:
    over-settlement stays representable, because settling 1200 against 1000
    earned is a positive row that drives outstanding to −200 (A-COM-6b).
    """

    __tablename__ = "commission_settlement"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))

    period_start: Mapped[business_day]
    period_end: Mapped[business_day]
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    settled_on: Mapped[business_day]
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Platform scope only (P0 §6). Never a tenant user.
    created_by_user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index(
            "ix_commission_settlement_tenant_id_settled_on", "tenant_id", "settled_on"
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
    )
