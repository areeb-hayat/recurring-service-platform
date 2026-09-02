"""Tenant root and the tenant-scoping context.

P0 §4: single database, single schema, ``tenant_id`` on every business row.
Per-tenant business configuration lives on this row — never as a code constant.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Numeric, SmallInteger, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import DEFAULT_TIMEZONE
from app.core.db import Base, ROW_VERSION_SEQUENCE, uuid_pk, utc_timestamp
from app.core.ids import new_id

__all__ = ["Tenant", "DEFAULT_REMINDER_SCHEDULE"]

# P0 §10 default; stored as tenant data so it is configuration, not a constant.
DEFAULT_REMINDER_SCHEDULE = [
    {"day": 1, "kind": "STATEMENT"},
    {"day": 4, "kind": "REMINDER"},
    {"day": 8, "kind": "REMINDER"},
    {"day": 12, "kind": "REMINDER"},
    {"day": 15, "kind": "FINAL"},
]


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # --- business configuration (P0 §4, §13) ---
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="PKR")
    currency_exponent: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="2"
    )
    unit_label: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unit"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=DEFAULT_TIMEZONE
    )
    cycle_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="MONTHLY"
    )
    cycle_start_day: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="1"
    )
    reminder_schedule: Mapped[list] = mapped_column(JSONB, nullable=False)
    default_unit_price_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    default_quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, server_default="0"
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    updated_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    row_version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text(f"nextval('{ROW_VERSION_SEQUENCE}')"),
    )

    __table_args__ = (
        CheckConstraint("currency_exponent BETWEEN 0 AND 4", name="currency_exponent_range"),
        CheckConstraint("default_unit_price_minor >= 0", name="default_price_non_negative"),
        CheckConstraint("default_quantity >= 0", name="default_quantity_non_negative"),
        CheckConstraint("cycle_start_day BETWEEN 1 AND 28", name="cycle_start_day_range"),
        CheckConstraint("status IN ('ACTIVE','SUSPENDED')", name="status_valid"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Tenant {self.slug}>"
