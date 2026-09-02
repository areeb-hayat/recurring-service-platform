"""DailyServiceRecord — the `[-] qty [+] CONFIRM / SKIP` business fact.

Key frozen properties (P0 §6):

* **SYN-4** — at most one ``ACTIVE`` row per ``(tenant, customer, service_date)``,
  enforced by a *partial unique index*. That index is the concurrency guarantee;
  application code never pre-reads to decide.
* **FIN-7** — a ``SKIP`` is a real row with ``quantity = 0`` and
  ``charge_minor = 0`` that creates no ledger entry. Enforced by a CHECK so the
  invariant cannot be broken even by a direct SQL insert.
* **AUD-2** — immutable except the single ``ACTIVE -> SUPERSEDED | VOIDED``
  transition (plus ``superseded_by_id``), performed in the same transaction as
  its replacement or compensation.
* ``source`` is *transport*; ``input_method`` is *provenance*. Orthogonal, and
  provenance never changes behaviour (P0 §6, VOI-8).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import (
    Base,
    ROW_VERSION_SEQUENCE,
    business_day,
    quantity,
    uuid_fk,
    uuid_nullable,
    uuid_pk,
    utc_timestamp,
)
from app.core.ids import new_id

__all__ = ["DailyServiceRecord", "ServiceKind", "RecordStatus", "Source", "InputMethod"]


class ServiceKind:
    SERVICE = "SERVICE"
    SKIP = "SKIP"


class RecordStatus:
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    VOIDED = "VOIDED"


class Source:
    """Transport: how the write reached the server."""

    ONLINE = "ONLINE"
    SYNC = "SYNC"
    IMPORT = "IMPORT"


class InputMethod:
    """Provenance: how the human expressed it. Metadata only."""

    BUTTON = "BUTTON"
    VOICE = "VOICE"


class DailyServiceRecord(Base):
    __tablename__ = "daily_service_record"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    customer_id: Mapped[uuid_fk]

    service_date: Mapped[business_day]
    quantity: Mapped[quantity]
    # FIN-6: price and label snapshotted at acceptance; never re-read from customer.
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_label: Mapped[str] = mapped_column(String(32), nullable=False)
    charge_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")

    # AUD-4: the correction chain, walkable in both directions.
    corrects_id: Mapped[uuid_nullable]
    superseded_by_id: Mapped[uuid_nullable]
    # AUD-5: on a correcting row, new charge - superseded charge.
    adjustment_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    recorded_by_user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("app_user.id"))
    operation_id: Mapped[uuid_fk]
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ONLINE")
    input_method: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="BUTTON"
    )
    recorded_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    row_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(f"nextval('{ROW_VERSION_SEQUENCE}')")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_daily_service_record_tenant_id_id"),
        # SEC-2: cross-tenant reference is physically impossible.
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_daily_service_record_tenant_id_customer_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "corrects_id"],
            ["daily_service_record.tenant_id", "daily_service_record.id"],
            name="fk_daily_service_record_tenant_id_corrects_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "superseded_by_id"],
            ["daily_service_record.tenant_id", "daily_service_record.id"],
            name="fk_daily_service_record_tenant_id_superseded_by_id",
        ),
        # SYN-4: THE duplicate-service guarantee.
        Index(
            "uq_daily_service_record_active_day",
            "tenant_id",
            "customer_id",
            "service_date",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "ix_daily_service_record_tenant_id_customer_id_service_date",
            "tenant_id",
            "customer_id",
            "service_date",
        ),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("unit_price_minor >= 0", name="unit_price_non_negative"),
        CheckConstraint("charge_minor >= 0", name="charge_non_negative"),
        CheckConstraint("kind IN ('SERVICE','SKIP')", name="kind_valid"),
        CheckConstraint(
            "status IN ('ACTIVE','SUPERSEDED','VOIDED')", name="status_valid"
        ),
        CheckConstraint(
            "source IN ('ONLINE','SYNC','IMPORT')", name="source_valid"
        ),
        CheckConstraint(
            "input_method IN ('BUTTON','VOICE')", name="input_method_valid"
        ),
        # FIN-7 at the database level: a SKIP can never carry quantity or charge.
        CheckConstraint(
            "kind <> 'SKIP' OR (quantity = 0 AND charge_minor = 0)",
            name="skip_is_zero",
        ),
    )
