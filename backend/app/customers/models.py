"""Customer — a tenant-owned record, never a login principal (SEC-7).

Historical prices are protected by snapshots on daily service records, not by a
price-history table (P0 §6): changing ``unit_price_minor`` here must never alter
anything already recorded (FIN-6).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, ROW_VERSION_SEQUENCE, quantity, uuid_fk, uuid_pk, utc_timestamp
from app.core.ids import new_id

__all__ = ["Customer"]


class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    whatsapp_e164: Mapped[str | None] = mapped_column(String(20), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Indexed: powers "unpaid customers in G-10" (P0 §6, §12.1).
    area: Mapped[str | None] = mapped_column(String(120), nullable=True)

    default_quantity: Mapped[quantity] = mapped_column(server_default="0")
    unit_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    updated_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    row_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text(f"nextval('{ROW_VERSION_SEQUENCE}')")
    )

    __table_args__ = (
        # SEC-2: the composite-FK target every child points at.
        UniqueConstraint("tenant_id", "id", name="uq_customer_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_customer_tenant_id_code"),
        Index("ix_customer_tenant_id_area", "tenant_id", "area"),
        Index("ix_customer_tenant_id_name", "tenant_id", "name"),
        CheckConstraint("default_quantity >= 0", name="default_quantity_non_negative"),
        CheckConstraint("unit_price_minor >= 0", name="unit_price_non_negative"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="status_valid"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
    )
