"""Turning a rate and a usage figure into money.

**Everything here is generic arithmetic over configured data.** No provider
name, no model name and no price appears in this module or anywhere else in
``app/`` — the current planning numbers (a price per audio hour, a price per
GB-month, a price per million tokens, an annual domain fee) are *rows* in
``operating_cost_rate`` that the owner can change without a deployment. That is
the whole reason the rate is versioned data rather than a constant.

Three rules, and nothing else:

    usage priced   estimate = round_half_up(usage_quantity * unit_price_minor)
    fixed monthly  estimate = fixed_amount_minor
    fixed annual   estimate = round_half_up(fixed_amount_minor / 12)

The owner's monthly total is the sum of those over every active cost item, which
is exactly the formula in the brief — hosting, speech-to-text, intent
interpretation, backup storage, messaging hosting, messaging charges, a twelfth
of the domain, and anything else configured — expressed without naming any of
them.

**Money stays integer minor units** and quantities stay ``Decimal`` (FIN-1).
The rounding goes through :func:`app.core.money.round_half_up`, the same single
implementation the charge rule and the commission rule use.

**Which rate applies to a month.** The rate effective on the **first day** of the
period month. A month has one rate, decided once, so the figure is deterministic
and a rate introduced part-way through a month cannot silently restate a month
that has already been reviewed. Ranges cannot overlap, so this is a lookup with
at most one answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import multiply_minor, round_half_up
from app.costs.models import CostRecurrence, OperatingCostRate
from app.tenancy.context import TenantContext

__all__ = [
    "MONTHS_PER_YEAR",
    "SECONDS_PER_HOUR",
    "RateTerms",
    "effective_rate",
    "estimate_minor",
    "monthly_equivalent_minor",
    "usage_hours_from_events",
    "month_start",
]

MONTHS_PER_YEAR = 12
SECONDS_PER_HOUR = 3600


def month_start(value: date) -> date:
    """The first day of ``value``'s month — the canonical period key."""
    return value.replace(day=1)


@dataclass(frozen=True, slots=True)
class RateTerms:
    """A rate's terms, as an estimate needs them.

    Built from a stored :class:`~app.costs.models.OperatingCostRate` *or* from
    the snapshot a usage row already carries, so a historical estimate is
    recomputable from what it recorded and not from what the rate says today.
    """

    unit: str | None
    unit_price_minor: int | None
    fixed_amount_minor: int | None
    fixed_recurrence: str | None
    currency: str
    currency_exponent: int

    @property
    def is_usage_priced(self) -> bool:
        return self.unit_price_minor is not None

    @classmethod
    def of(cls, rate: OperatingCostRate) -> "RateTerms":
        return cls(
            unit=rate.unit,
            unit_price_minor=rate.unit_price_minor,
            fixed_amount_minor=rate.fixed_amount_minor,
            fixed_recurrence=rate.fixed_recurrence,
            currency=rate.currency,
            currency_exponent=rate.currency_exponent,
        )


def effective_rate(
    session: Session,
    ctx: TenantContext,
    *,
    cost_item_id,
    on_date: date,
) -> OperatingCostRate | None:
    """The rate in force for ``cost_item_id`` on ``on_date``, or ``None``.

    At most one row can match: the EXCLUDE constraint forbids overlapping ranges
    for an item, so this is a lookup and never a precedence decision.
    """
    return session.execute(
        select(OperatingCostRate).where(
            OperatingCostRate.tenant_id == ctx.tenant_id,
            OperatingCostRate.cost_item_id == cost_item_id,
            OperatingCostRate.effective_from <= on_date,
            (OperatingCostRate.effective_to.is_(None))
            | (OperatingCostRate.effective_to >= on_date),
        )
    ).scalar_one_or_none()


def monthly_equivalent_minor(amount_minor: int, recurrence: str) -> int:
    """A fixed charge expressed as one month of it.

    An annual fee divided by twelve, rounded once by the shared rule. Done here
    rather than on a screen, so "annual cost / 12" exists in exactly one place
    and no client ever divides money.
    """
    if recurrence == CostRecurrence.MONTHLY:
        return amount_minor
    if recurrence == CostRecurrence.ANNUAL:
        with localcontext() as ctx:
            ctx.prec = 50
            return round_half_up(Decimal(amount_minor) / Decimal(MONTHS_PER_YEAR))
    raise ValueError(f"unknown recurrence {recurrence!r}")


def estimate_minor(terms: RateTerms, usage_quantity: Decimal | None) -> int | None:
    """One month's estimated cost under ``terms``, or ``None`` if unknowable.

    ``None`` — rather than zero — when a usage-priced item has no measured usage
    for the month. A zero estimate would claim the provider was free; the honest
    answer is that nobody has said yet.
    """
    if terms.is_usage_priced:
        if usage_quantity is None:
            return None
        assert terms.unit_price_minor is not None
        return multiply_minor(usage_quantity, terms.unit_price_minor)

    assert terms.fixed_amount_minor is not None and terms.fixed_recurrence is not None
    return monthly_equivalent_minor(terms.fixed_amount_minor, terms.fixed_recurrence)


def usage_hours_from_events(
    *, events_per_day: int, seconds_per_event: Decimal, days: int
) -> Decimal:
    """``events_per_day * days * seconds_per_event / 3600``, exactly.

    A duration conversion, not vendor knowledge: "N things a day, each lasting S
    seconds, over D days" in hours. It is the shape the owner's planning
    scenarios use, and it stays arithmetic — the *price* per hour is a rate row.

    Exact by construction: ``Decimal`` throughout at high precision, then
    quantised to the usage scale the column stores. No float, and no ``/`` on a
    binary value.
    """
    if events_per_day < 0 or days < 0 or seconds_per_event < 0:
        raise ValueError("scenario inputs must not be negative")
    with localcontext() as ctx:
        ctx.prec = 50
        total_seconds = Decimal(events_per_day) * Decimal(days) * seconds_per_event
        return total_seconds / Decimal(SECONDS_PER_HOUR)
