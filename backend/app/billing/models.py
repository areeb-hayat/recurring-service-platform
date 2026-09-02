"""Billing tables: the ledger, billing cycles and issued statements.

LedgerEntry — the single derivation source for every balance (P0 §5.3).

**Append-only.** No UPDATE, no DELETE, no status column, ever (FIN-12, AUD-7).
Voids and corrections append compensating entries; the original is never touched.

``outstanding(customer) = SUM(amount_minor)`` for that tenant and customer
(FIN-4). Nothing caches it.

Sign convention: positive increases what the customer owes.

``posting_cycle_id`` carries a **composite** foreign key to ``billing_cycle``
so an entry can never post into another tenant's cycle. P0 §5.5: an entry always
posts to the tenant's currently ``OPEN`` cycle while keeping the true
``occurred_on``, which is what lets a late correction be billed on the next
statement without rewriting a delivered one.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import (
    Base,
    ROW_VERSION_SEQUENCE,
    business_day,
    uuid_fk,
    uuid_nullable,
    uuid_pk,
    utc_timestamp,
)
from app.core.ids import new_id

__all__ = [
    "LedgerEntry",
    "EntryKind",
    "SourceType",
    "BillingCycle",
    "CycleStatus",
    "Statement",
]


class EntryKind:
    OPENING = "OPENING"
    CHARGE = "CHARGE"
    PAYMENT = "PAYMENT"
    ADJUSTMENT = "ADJUSTMENT"


class SourceType:
    """The document an entry derives from.

    Decides adjustment *origin* (P0 §5.3): a ``daily_service_record`` adjustment
    is service-origin and moves business generated; a ``payment`` adjustment is
    payment-origin and moves collections. Reporting must filter on this, never
    on ``entry_kind`` alone.
    """

    DAILY_SERVICE_RECORD = "daily_service_record"
    PAYMENT = "payment"
    OPENING_BALANCE = "opening_balance"


class LedgerEntry(Base):
    __tablename__ = "ledger_entry"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    customer_id: Mapped[uuid_fk]

    entry_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_on: Mapped[business_day]
    posting_cycle_id: Mapped[uuid_nullable]

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid_fk]

    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    created_by_user_id: Mapped[uuid_nullable] = mapped_column(ForeignKey("app_user.id"))
    row_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(f"nextval('{ROW_VERSION_SEQUENCE}')")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_ledger_entry_tenant_id_customer_id",
        ),
        # One source document can never post the same kind of entry twice.
        UniqueConstraint(
            "tenant_id",
            "source_type",
            "source_id",
            "entry_kind",
            name="uq_ledger_entry_tenant_id_source_type_source_id_entry_kind",
        ),
        # SEC-2: an entry can only post into a cycle owned by its own tenant.
        ForeignKeyConstraint(
            ["tenant_id", "posting_cycle_id"],
            ["billing_cycle.tenant_id", "billing_cycle.id"],
            name="fk_ledger_entry_tenant_id_posting_cycle_id",
        ),
        Index("ix_ledger_entry_tenant_id_customer_id_id", "tenant_id", "customer_id", "id"),
        Index("ix_ledger_entry_tenant_id_posting_cycle_id", "tenant_id", "posting_cycle_id"),
        # A zero-value entry is meaningless; callers must skip instead of posting one.
        CheckConstraint("amount_minor <> 0", name="amount_non_zero"),
        CheckConstraint(
            "entry_kind IN ('OPENING','CHARGE','PAYMENT','ADJUSTMENT')",
            name="entry_kind_valid",
        ),
    )


class CycleStatus:
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class BillingCycle(Base):
    """A tenant's billing period (P0 §5.5, §6).

    Exactly one ``OPEN`` cycle per tenant, enforced by a *partial unique index* —
    the same technique the daily-record active-day guarantee uses, so two
    concurrent requests cannot open two cycles.

    ``period_start``/``period_end`` are tenant-local business dates, not
    instants: a cycle is a range of calendar days in the tenant's timezone.

    Deliberately **no** ``row_version``: a cycle is server-side billing
    scaffolding, not one of the authoritative records P0 §7.1 puts in the client
    snapshot. Adding one for symmetry would make it a sync entity by accident.
    """

    __tablename__ = "billing_cycle"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))

    period_start: Mapped[business_day]
    period_end: Mapped[business_day]
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="OPEN")

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_by_user_id: Mapped[uuid_nullable] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        # Composite target for (tenant_id, posting_cycle_id) and (tenant_id, cycle_id).
        UniqueConstraint("tenant_id", "id", name="uq_billing_cycle_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "period_start", name="uq_billing_cycle_tenant_id_period_start"
        ),
        # P0 §5.5: exactly one OPEN cycle per tenant.
        Index(
            "uq_billing_cycle_one_open_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        CheckConstraint("status IN ('OPEN','CLOSED')", name="status_valid"),
        # A CLOSED cycle carries its closing instant; an OPEN one never does.
        CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL)", name="closed_at_matches_status"
        ),
    )


class Statement(Base):
    """An issued statement — a frozen presentation of one cycle (P0 §5.4, §6).

    **Immutable after issue** (FIN-8), enforced in the database by a trigger that
    rejects UPDATE and DELETE, not merely by the absence of a route.

    The movement columns are split by adjustment *origin*: service adjustments and
    payment reversals are never merged into one figure, because billed value
    (FIN-15) is defined as ``charges + service_adjustments`` and a merged column
    would silently contaminate it with reversed payments.

    ``payments_minor`` and ``payment_reversals_minor`` are stored **positive**;
    the identity subtracts and adds them respectively.

    ``row_version`` is drawn from the shared sequence at issue and never changes
    again — an immutable row needs no second value. It exists because P0 §7.1
    puts **statements** in the client's authoritative offline snapshot and §7.4
    pages that snapshot on ``row_version > since``.
    """

    __tablename__ = "statement"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    customer_id: Mapped[uuid_fk]
    cycle_id: Mapped[uuid_fk]

    issued_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    opening_balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charges_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    service_adjustments_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payments_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payment_reversals_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    closing_balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    service_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, server_default="0"
    )
    # Snapshots: a later tenant reconfiguration must not restate an issued bill.
    unit_label: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    row_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(f"nextval('{ROW_VERSION_SEQUENCE}')")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_statement_tenant_id_customer_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["billing_cycle.tenant_id", "billing_cycle.id"],
            name="fk_statement_tenant_id_cycle_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "customer_id",
            "cycle_id",
            name="uq_statement_tenant_id_customer_id_cycle_id",
        ),
        Index("ix_statement_tenant_id_cycle_id", "tenant_id", "cycle_id"),
        # FIN-8 in the database: the §5.4 identity cannot be violated even by a
        # direct SQL insert that bypasses the application entirely.
        CheckConstraint(
            "closing_balance_minor = opening_balance_minor + charges_minor "
            "+ service_adjustments_minor - payments_minor + payment_reversals_minor",
            name="balance_identity",
        ),
        CheckConstraint("charges_minor >= 0", name="charges_non_negative"),
        CheckConstraint("payments_minor >= 0", name="payments_non_negative"),
        CheckConstraint(
            "payment_reversals_minor >= 0", name="payment_reversals_non_negative"
        ),
        CheckConstraint("service_days >= 0", name="service_days_non_negative"),
        CheckConstraint("total_quantity >= 0", name="total_quantity_non_negative"),
    )
