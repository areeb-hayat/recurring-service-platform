"""Customer — a tenant-owned record, never a login principal (SEC-7).

Historical prices are protected by snapshots on daily service records, not by a
price-history table (P0 §6): changing ``unit_price_minor`` here must never alter
anything already recorded (FIN-6).

P8 adds the search half of customer identity: ``Customer.normalized_name`` and
:class:`CustomerAlias`. Both hold *comparison keys* produced by the single
normalization path (:mod:`app.search.normalize`) and neither is ever displayed —
``name`` and ``alias`` keep exactly what the owner typed.
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
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, ROW_VERSION_SEQUENCE, quantity, uuid_fk, uuid_pk, utc_timestamp
from app.core.ids import new_id

__all__ = ["Customer", "CustomerAlias", "AliasStatus"]


class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # P8. The comparison key for ``name``, written by ``normalize_text`` at every
    # write path and never shown to anybody. It exists as a column rather than as
    # ``lower(name)`` in a WHERE clause so that there is exactly one definition of
    # "the same name" in the system, in Python, testable on its own.
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
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
        # Serves exact-name resolution and the anchored prefix search.
        Index("ix_customer_tenant_id_normalized_name", "tenant_id", "normalized_name"),
        CheckConstraint("default_quantity >= 0", name="default_quantity_non_negative"),
        CheckConstraint("unit_price_minor >= 0", name="unit_price_non_negative"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="status_valid"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
    )


class AliasStatus:
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class CustomerAlias(Base):
    """A name a customer is actually called (P8).

    "Muhammad Ahmed Khan" is on the books; the round calls him "Ahmed bhai" and
    the register in his shop calls him "Chacha Ahmed". An alias records that,
    once, on the server, so every channel that ever has to identify him — the
    website today, a transcript or an inbound message later — reads the same
    table instead of each growing its own guesswork.

    **Not a sync entity of its own.** There is no ``row_version`` here. An alias
    write bumps the *owning customer's* ``row_version`` and the alias travels
    inside the customer's payload, so the change feed carries it with no new
    entity, no new cursor and no new ordering question (see
    ``app/sync/changes.py``).

    **Aliases are not unique across customers, on purpose.** Two brothers can
    both be "Ahmed bhai". That is precisely the case the resolver must answer
    with AMBIGUOUS rather than a guess, so the schema must be able to represent
    it. What is unique is one *active* alias spelling per customer.

    **Correction, never deletion.** An alias that is no longer used goes
    ``INACTIVE`` and stays; the row is protected against DELETE by a trigger, and
    every add, correction and deactivation writes an audit event carrying the
    before and after text.
    """

    __tablename__ = "customer_alias"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    customer_id: Mapped[uuid_fk]

    #: Exactly what the owner typed. This is what is shown back to them.
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    #: ``normalize_text(alias)``. Never displayed.
    normalized: Mapped[str] = mapped_column(String(200), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    updated_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # SEC-2: an alias can only ever name its own tenant's customer.
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customer.tenant_id", "customer.id"],
            name="fk_customer_alias_tenant_id_customer_id",
        ),
        Index(
            "uq_customer_alias_active_normalized",
            "tenant_id",
            "customer_id",
            "normalized",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        # The search index: one lookup for every alias in the tenant that starts
        # with, or equals, a normalized query.
        Index("ix_customer_alias_tenant_id_normalized", "tenant_id", "normalized"),
        # Loading every alias of a batch of customers in one statement — the
        # answer to "no N+1 alias queries".
        Index("ix_customer_alias_tenant_id_customer_id", "tenant_id", "customer_id"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="status_valid"),
        CheckConstraint("length(btrim(alias)) > 0", name="alias_not_blank"),
        CheckConstraint("length(btrim(normalized)) > 0", name="normalized_not_blank"),
        CheckConstraint(
            "(status = 'INACTIVE') = (deactivated_at IS NOT NULL)",
            name="deactivated_at_matches_status",
        ),
    )
