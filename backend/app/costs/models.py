"""Operating costs — what the business pays its providers (P6).

**This is not the customer ledger and it is not commission.** Three separate
accounting concepts exist in this system and P6 adds the third:

* ``ledger_entry`` — what a *customer* owes the business.
* ``commission_*`` — what the business owes the *platform* (P3, platform scope).
* ``operating_cost_*`` — what the business owes its *providers* (here).

Nothing in this module posts a ledger entry, reads a commission row, or touches
an outstanding balance. An operating cost never appears on a customer's
statement and never moves a commission figure; tests assert both.

Four tables, and the smallest set that answers the four questions the owner
actually asked:

    what does a provider charge?      operating_cost_item + operating_cost_rate
    how much did we use?              operating_cost_usage
    what did they actually invoice?   operating_cost_actual
    what was the difference?          derived: actual - estimated

**Rates are versioned, never rewritten.** ``operating_cost_rate`` carries an
effective range, and ranges may not overlap for one cost item — an EXCLUDE
constraint, the same mechanism ``commission_plan`` uses, because an ambiguous
"rate in force" would be snapshotted onto history nobody can correct. A new rate
closes its open-ended predecessor at ``effective_from - 1 day``; it never edits
one.

**History is not destructive** (AUD-1, AUD-6). A usage row and an actual-invoice
row are corrected the way a daily service record is: the original stays as
``SUPERSEDED``, carrying the reason it was replaced, and the new ``ACTIVE`` row
links back to it. A partial unique index keeps exactly one ACTIVE row per
(item, month), and no delete path exists.

**No ``row_version``.** None of these tables is a client sync entity: the
Operating Costs screen is online-only (P6 §19), so versioning them "for
symmetry" would quietly make them syncable. The column is added the day a screen
needs them offline, not before.

**Currency travels with the money.** Provider invoices are in the provider's
currency, which is routinely not the tenant's billing currency, and V1 has no FX
source. So every rate and every invoice stores its own ``currency`` and
``currency_exponent``, and totals are reported *per currency* rather than summed
across them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, uuid_fk, uuid_nullable, uuid_pk, utc_timestamp
from app.core.ids import new_id

__all__ = [
    "CostItemStatus",
    "CostRecurrence",
    "CostRowStatus",
    "USAGE_QUANTITY_SCALE",
    "OperatingCostItem",
    "OperatingCostRate",
    "OperatingCostUsage",
    "OperatingCostActual",
]

#: Usage is measured, not billed: audio hours, GB-months and millions of tokens
#: need more precision than the ``NUMERIC(12,3)`` FIN-2 fixes for a *service*
#: quantity, and are a different concept. Still exact, still never a float.
USAGE_QUANTITY_SCALE = 6


class CostItemStatus:
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"

    ALL = (ACTIVE, ARCHIVED)


class CostRecurrence:
    """How a fixed provider charge repeats.

    ``ANNUAL`` exists for the one shape the owner's formula names explicitly — a
    yearly domain fee shown as a monthly equivalent. Normalisation happens in
    :mod:`app.costs.estimates`, once, so no screen divides money by twelve.
    """

    MONTHLY = "MONTHLY"
    ANNUAL = "ANNUAL"

    ALL = (MONTHLY, ANNUAL)


class CostRowStatus:
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"

    ALL = (ACTIVE, SUPERSEDED)


class OperatingCostItem(Base):
    """One provider or cost line, configured by the owner.

    Deliberately a **table**, not an enum in code. The owner's current list —
    hosting and database, speech-to-text, intent interpretation, backup storage,
    messaging automation hosting, messaging charges, domain — is what the
    business happens to pay for today; freezing it into application logic would
    mean a code change to record a new supplier. ``code`` is the owner's own
    label, unique within the tenant.
    """

    __tablename__ = "operating_cost_item"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")

    created_by_user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_operating_cost_item_tenant_id_id"),
        UniqueConstraint("tenant_id", "code", name="uq_operating_cost_item_tenant_id_code"),
        CheckConstraint("status IN ('ACTIVE','ARCHIVED')", name="status_valid"),
    )


class OperatingCostRate(Base):
    """A provider's price, valid over a date range.

    Exactly one of two shapes, enforced by a CHECK:

    * **usage priced** — ``unit_price_minor`` plus a ``unit`` label (per audio
      hour, per GB-month, per million tokens). The month's estimate is
      ``round_half_up(measured usage * unit_price_minor)``.
    * **fixed** — ``fixed_amount_minor`` plus a ``recurrence``. The month's
      estimate is that amount, or a twelfth of it for an annual charge.

    Ranges may not overlap for one item, so "the rate in force" is a lookup with
    at most one answer rather than a precedence rule — the same reasoning, and
    the same EXCLUDE mechanism, as ``commission_plan``.
    """

    __tablename__ = "operating_cost_rate"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    cost_item_id: Mapped[uuid_fk]

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    unit_price_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fixed_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fixed_recurrence: Mapped[str | None] = mapped_column(String(16), nullable=True)

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    currency_exponent: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("app_user.id"))
    created_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_operating_cost_rate_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "cost_item_id"],
            ["operating_cost_item.tenant_id", "operating_cost_item.id"],
            name="fk_operating_cost_rate_tenant_id_cost_item_id",
        ),
        Index(
            "ix_operating_cost_rate_tenant_id_cost_item_id_effective_from",
            "tenant_id",
            "cost_item_id",
            "effective_from",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_ordered",
        ),
        # Exactly one pricing shape, and each one complete.
        CheckConstraint(
            "(unit_price_minor IS NOT NULL) <> (fixed_amount_minor IS NOT NULL)",
            name="exactly_one_pricing_shape",
        ),
        CheckConstraint(
            "(unit_price_minor IS NULL) OR (unit IS NOT NULL AND unit_price_minor >= 0)",
            name="usage_rate_complete",
        ),
        CheckConstraint(
            "(fixed_amount_minor IS NULL) OR "
            "(fixed_recurrence IN ('MONTHLY','ANNUAL') AND fixed_amount_minor >= 0)",
            name="fixed_rate_complete",
        ),
        CheckConstraint(
            "fixed_amount_minor IS NOT NULL OR fixed_recurrence IS NULL",
            name="recurrence_only_on_fixed",
        ),
        CheckConstraint("currency_exponent BETWEEN 0 AND 4", name="currency_exponent_valid"),
    )


class OperatingCostUsage(Base):
    """How much of a usage-priced item one month consumed, and what that costs.

    The rate's terms are **snapshotted** onto the row, exactly as a commission
    event snapshots its plan: a rate recorded later must not silently restate a
    month the owner already reviewed. ``inputs`` keeps the working — the commands
    per day and seconds per command behind an audio-hour figure — so a number can
    be explained a year later.

    ``estimated_amount_minor`` is stored rather than recomputed for the same
    reason: it is what the estimate *was*.
    """

    __tablename__ = "operating_cost_usage"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    cost_item_id: Mapped[uuid_fk]
    rate_id: Mapped[uuid_fk]

    #: Always the first day of the month it describes (CHECK enforced).
    period_month: Mapped[date] = mapped_column(Date, nullable=False)

    usage_quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, USAGE_QUANTITY_SCALE), nullable=False
    )
    usage_unit: Mapped[str] = mapped_column(String(40), nullable=False)
    unit_price_minor_snapshot: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    currency_exponent: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")
    supersedes_id: Mapped[uuid_nullable]
    superseded_by_id: Mapped[uuid_nullable]
    correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    recorded_by_user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("app_user.id"))
    recorded_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_operating_cost_usage_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "cost_item_id"],
            ["operating_cost_item.tenant_id", "operating_cost_item.id"],
            name="fk_operating_cost_usage_tenant_id_cost_item_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "rate_id"],
            ["operating_cost_rate.tenant_id", "operating_cost_rate.id"],
            name="fk_operating_cost_usage_tenant_id_rate_id",
        ),
        Index(
            "ix_operating_cost_usage_tenant_id_period_month",
            "tenant_id",
            "period_month",
        ),
        CheckConstraint("EXTRACT(DAY FROM period_month) = 1", name="period_is_month_start"),
        CheckConstraint("usage_quantity >= 0", name="usage_non_negative"),
        CheckConstraint("unit_price_minor_snapshot >= 0", name="unit_price_non_negative"),
        CheckConstraint("estimated_amount_minor >= 0", name="estimate_non_negative"),
        CheckConstraint("status IN ('ACTIVE','SUPERSEDED')", name="status_valid"),
        CheckConstraint("currency_exponent BETWEEN 0 AND 4", name="currency_exponent_valid"),
        # AUD-6: a correction always says why.
        CheckConstraint(
            "status <> 'SUPERSEDED' OR (superseded_by_id IS NOT NULL "
            "AND correction_reason IS NOT NULL)",
            name="superseded_requires_reason",
        ),
    )


class OperatingCostActual(Base):
    """What the provider actually invoiced for one month.

    Never invented. A month with no invoice entered simply has none, and no
    variance is reported for it — a zero would read as "they charged us nothing",
    which is a different claim.

    Zero *is* accepted as an entered amount, because a bundled first-year domain
    genuinely costs nothing and the owner should be able to record that fact.
    """

    __tablename__ = "operating_cost_actual"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    cost_item_id: Mapped[uuid_fk]

    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    currency_exponent: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    invoice_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ACTIVE")
    supersedes_id: Mapped[uuid_nullable]
    superseded_by_id: Mapped[uuid_nullable]
    correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    recorded_by_user_id: Mapped[uuid_fk] = mapped_column(ForeignKey("app_user.id"))
    recorded_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_operating_cost_actual_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "cost_item_id"],
            ["operating_cost_item.tenant_id", "operating_cost_item.id"],
            name="fk_operating_cost_actual_tenant_id_cost_item_id",
        ),
        Index(
            "ix_operating_cost_actual_tenant_id_period_month",
            "tenant_id",
            "period_month",
        ),
        CheckConstraint("EXTRACT(DAY FROM period_month) = 1", name="period_is_month_start"),
        CheckConstraint("amount_minor >= 0", name="amount_non_negative"),
        CheckConstraint("status IN ('ACTIVE','SUPERSEDED')", name="status_valid"),
        CheckConstraint("currency_exponent BETWEEN 0 AND 4", name="currency_exponent_valid"),
        CheckConstraint(
            "status <> 'SUPERSEDED' OR (superseded_by_id IS NOT NULL "
            "AND correction_reason IS NOT NULL)",
            name="superseded_requires_reason",
        ),
    )
