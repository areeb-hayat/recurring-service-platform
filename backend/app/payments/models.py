"""Payment — an accepted money-in fact, recorded by the owner (P0 §6, PAY-1..9).

V1 payments are **manual only**. There is no gateway, no provider reference, no
callback and no externally verified state, and none is representable here: the
table has no provider column and no attempt/intent sibling (PAY-1, A-PAY-1). If
online payments ever return they arrive as a new port over this unchanged ledger.

Duplicate protection is ``operation_id`` and nothing else (PAY-5, PAY-6). There
is deliberately **no** amount/date natural key: two genuine cash payments of the
same amount from the same customer on the same day are legal and must post
twice. A unique index on (customer, received_on, amount_minor) would be a
correctness bug, not a safety feature.

Lifecycle is the single ``RECORDED -> VOIDED`` transition (AUD-2). The row is
never deleted and its amount is never edited; a void appends a compensating
payment-origin ADJUSTMENT to the ledger (PAY-7).

``row_version`` is drawn from the shared sequence on insert and advanced on the
void transition. P0 §7.1 puts **payment history** in the client's authoritative
offline snapshot and §7.4 drives the delta from ``row_version > since``, so the
payment row needs its own cursor value: the ledger entry a payment posts is a
different record, and a client pulling payment history cannot page on it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
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

__all__ = ["Payment", "PaymentMethod", "PaymentStatus"]


class PaymentMethod:
    """PAY-2. The complete V1 vocabulary — no online or provider state exists."""

    CASH = "CASH"
    BANK_TRANSFER = "BANK_TRANSFER"
    OTHER = "OTHER"

    ALL = ("CASH", "BANK_TRANSFER", "OTHER")


class PaymentStatus:
    RECORDED = "RECORDED"
    VOIDED = "VOIDED"


class Payment(Base):
    __tablename__ = "payment"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    customer_id: Mapped[uuid_fk]

    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    received_on: Mapped[business_day]
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="RECORDED")
    voided_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    voided_by_user_id: Mapped[uuid_nullable] = mapped_column(ForeignKey("app_user.id"))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    operation_id: Mapped[uuid_fk]
    recorded_by_user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("app_user.id"))
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ONLINE")
    recorded_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    row_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(f"nextval('{ROW_VERSION_SEQUENCE}')")
    )

    __table_args__ = (
        # SEC-2 / PAY-4: a payment can never attach to another tenant's customer.
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_payment_tenant_id_customer_id",
        ),
        Index(
            "ix_payment_tenant_id_customer_id_received_on",
            "tenant_id",
            "customer_id",
            "received_on",
        ),
        # PAY-3 at the database level, not only in application validation.
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(
            "method IN ('CASH','BANK_TRANSFER','OTHER')", name="method_valid"
        ),
        CheckConstraint("status IN ('RECORDED','VOIDED')", name="status_valid"),
        CheckConstraint("source IN ('ONLINE','SYNC','IMPORT')", name="source_valid"),
        # AUD-3/AUD-6: a void always carries its reason, actor and timestamp.
        CheckConstraint(
            "(status = 'VOIDED') = (voided_at IS NOT NULL)",
            name="voided_at_matches_status",
        ),
        CheckConstraint(
            "status <> 'VOIDED' OR (voided_reason IS NOT NULL "
            "AND voided_by_user_id IS NOT NULL)",
            name="void_requires_reason_and_actor",
        ),
    )
