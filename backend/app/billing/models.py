"""LedgerEntry — the single derivation source for every balance (P0 §5.3).

**Append-only.** No UPDATE, no DELETE, no status column, ever (FIN-12, AUD-7).
Voids and corrections append compensating entries; the original is never touched.

``outstanding(customer) = SUM(amount_minor)`` for that tenant and customer
(FIN-4). Nothing caches it.

Sign convention: positive increases what the customer owes.

P1 note — ``posting_cycle_id`` is present and nullable but carries no foreign key
yet, because ``billing_cycle`` is a P2 table. P2 adds the FK and the resolution
rule (P0 §5.5 late corrections) without altering this table's shape or the
correction semantics built on it.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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

__all__ = ["LedgerEntry", "EntryKind", "SourceType"]


class EntryKind:
    OPENING = "OPENING"
    CHARGE = "CHARGE"
    PAYMENT = "PAYMENT"  # P2 — no payments exist in P1
    ADJUSTMENT = "ADJUSTMENT"


class SourceType:
    """The document an entry derives from.

    Decides adjustment *origin* (P0 §5.3): a ``daily_service_record`` adjustment
    is service-origin and moves business generated; a ``payment`` adjustment is
    payment-origin and moves collections. Reporting must filter on this, never
    on ``entry_kind`` alone.
    """

    DAILY_SERVICE_RECORD = "daily_service_record"
    PAYMENT = "payment"  # P2
    OPENING_BALANCE = "opening_balance"


class LedgerEntry(Base):
    __tablename__ = "ledger_entry"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    customer_id: Mapped[uuid_fk]

    entry_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_on: Mapped[business_day]
    posting_cycle_id: Mapped[uuid_nullable]  # FK added in P2 with billing_cycle

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
        Index("ix_ledger_entry_tenant_id_customer_id_id", "tenant_id", "customer_id", "id"),
        Index("ix_ledger_entry_tenant_id_posting_cycle_id", "tenant_id", "posting_cycle_id"),
        # A zero-value entry is meaningless; callers must skip instead of posting one.
        CheckConstraint("amount_minor <> 0", name="amount_non_zero"),
        CheckConstraint(
            "entry_kind IN ('OPENING','CHARGE','PAYMENT','ADJUSTMENT')",
            name="entry_kind_valid",
        ),
    )
