"""Estimated, actual, and the difference between them.

    variance = actual - estimated

Positive means the provider charged more than the estimate expected. That is the
whole arithmetic; everything else in this module is about *not* claiming to know
things nobody has said:

* a usage-priced item with no measured usage for a month has **no estimate** —
  not zero. Zero would say the provider was free.
* an item with no invoice entered for a month has **no actual** — not zero, and
  therefore **no variance**. An invoice that has not arrived is not an invoice
  for nothing (P6 §14).
* totals are reported **per currency**. Provider prices are quoted in the
  provider's currency, the tenant bills in its own, and V1 has no FX source
  (P6 §18) — so nothing is converted and nothing is summed across currencies.

Nothing here reads the customer ledger or any commission table, and nothing here
writes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.costs.commands import serialize_rate
from app.costs.estimates import (
    RateTerms,
    effective_rate,
    estimate_minor,
    month_start,
    usage_hours_from_events,
)
from app.costs.models import (
    USAGE_QUANTITY_SCALE,
    CostRowStatus,
    OperatingCostActual,
    OperatingCostItem,
    OperatingCostUsage,
)
from app.tenancy.context import TenantContext

__all__ = [
    "month_summary",
    "month_history",
    "previous_months",
    "evaluate_scenarios",
]


def previous_months(latest: date, count: int) -> list[date]:
    """``count`` month-start dates ending at ``latest``, oldest first."""
    months: list[date] = []
    year, month = latest.year, latest.month
    for _ in range(count):
        months.append(date(year, month, 1))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(months))


def _active_rows(session: Session, ctx: TenantContext, months: list[date]):
    usage = (
        session.execute(
            select(OperatingCostUsage).where(
                OperatingCostUsage.tenant_id == ctx.tenant_id,
                OperatingCostUsage.period_month.in_(months),
                OperatingCostUsage.status == CostRowStatus.ACTIVE,
            )
        )
        .scalars()
        .all()
    )
    actuals = (
        session.execute(
            select(OperatingCostActual).where(
                OperatingCostActual.tenant_id == ctx.tenant_id,
                OperatingCostActual.period_month.in_(months),
                OperatingCostActual.status == CostRowStatus.ACTIVE,
            )
        )
        .scalars()
        .all()
    )
    return (
        {(u.cost_item_id, u.period_month): u for u in usage},
        {(a.cost_item_id, a.period_month): a for a in actuals},
    )


def _accumulate(totals: dict[str, dict[str, int | None]], currency: str, key: str, value: int):
    bucket = totals.setdefault(
        currency,
        {"estimated_minor": 0, "actual_minor": 0, "variance_minor": 0},
    )
    bucket[key] = (bucket[key] or 0) + value


def _line_for(
    session: Session,
    ctx: TenantContext,
    item: OperatingCostItem,
    period_month: date,
    usage: OperatingCostUsage | None,
    actual: OperatingCostActual | None,
) -> dict[str, Any]:
    """One cost item for one month: what we expected, what we were charged."""
    rate = effective_rate(session, ctx, cost_item_id=item.id, on_date=period_month)

    if usage is not None:
        # The estimate a recorded month carries is the one that was computed from
        # the terms in force then — read back, never re-derived. A rate added
        # afterwards therefore cannot restate a month already reviewed.
        estimated = usage.estimated_amount_minor
        currency = usage.currency
        exponent = usage.currency_exponent
    elif rate is not None:
        terms = RateTerms.of(rate)
        estimated = estimate_minor(terms, None)  # None for usage-priced with no usage
        currency = rate.currency
        exponent = rate.currency_exponent
    else:
        estimated = None
        currency = actual.currency if actual is not None else ctx.currency
        exponent = actual.currency_exponent if actual is not None else ctx.currency_exponent

    if actual is not None:
        currency = actual.currency
        exponent = actual.currency_exponent

    actual_minor = actual.amount_minor if actual is not None else None
    # Only when both halves exist, and only in one currency.
    variance = (
        actual_minor - estimated
        if actual_minor is not None
        and estimated is not None
        and (usage is None or usage.currency == actual.currency)
        else None
    )

    return {
        "cost_item_id": str(item.id),
        "code": item.code,
        "name": item.name,
        "period_month": period_month.isoformat(),
        "currency": currency,
        "currency_exponent": exponent,
        "rate": serialize_rate(rate) if rate is not None else None,
        "usage_quantity": str(usage.usage_quantity) if usage is not None else None,
        "usage_unit": usage.usage_unit if usage is not None else (rate.unit if rate else None),
        "usage_inputs": usage.inputs if usage is not None else None,
        "estimated_amount_minor": estimated,
        "actual_amount_minor": actual_minor,
        "actual_invoice_reference": actual.invoice_reference if actual else None,
        "variance_minor": variance,
        "usage_id": str(usage.id) if usage is not None else None,
        "actual_id": str(actual.id) if actual is not None else None,
    }


def month_summary(
    session: Session, ctx: TenantContext, *, period_month: date
) -> dict[str, Any]:
    """One month: a line per active cost item, plus totals per currency."""
    month = month_start(period_month)
    items = list(
        session.execute(
            select(OperatingCostItem)
            .where(OperatingCostItem.tenant_id == ctx.tenant_id)
            .order_by(OperatingCostItem.code)
        )
        .scalars()
        .all()
    )
    usage_by, actual_by = _active_rows(session, ctx, [month])

    lines: list[dict[str, Any]] = []
    totals: dict[str, dict[str, int | None]] = {}
    for item in items:
        line = _line_for(
            session,
            ctx,
            item,
            month,
            usage_by.get((item.id, month)),
            actual_by.get((item.id, month)),
        )
        # An archived item with nothing recorded for the month is not a line.
        if (
            item.status != "ACTIVE"
            and line["estimated_amount_minor"] is None
            and line["actual_amount_minor"] is None
        ):
            continue
        lines.append(line)
        if line["estimated_amount_minor"] is not None:
            _accumulate(totals, line["currency"], "estimated_minor", line["estimated_amount_minor"])
        if line["actual_amount_minor"] is not None:
            _accumulate(totals, line["currency"], "actual_minor", line["actual_amount_minor"])
        if line["variance_minor"] is not None:
            _accumulate(totals, line["currency"], "variance_minor", line["variance_minor"])

    return {
        "period_month": month.isoformat(),
        "lines": lines,
        "totals": [
            {"currency": currency, **values} for currency, values in sorted(totals.items())
        ],
    }


def month_history(
    session: Session, ctx: TenantContext, *, latest_month: date, months: int
) -> dict[str, Any]:
    """Month-by-month totals, oldest first, plus the range's own totals.

    The range total is a year-to-date figure when the caller asks for one; it is
    still per currency, for the same reason every other total here is.
    """
    period = previous_months(month_start(latest_month), max(1, min(months, 36)))
    items = {
        item.id: item
        for item in session.execute(
            select(OperatingCostItem).where(OperatingCostItem.tenant_id == ctx.tenant_id)
        )
        .scalars()
        .all()
    }
    usage_by, actual_by = _active_rows(session, ctx, period)

    rows: list[dict[str, Any]] = []
    range_totals: dict[str, dict[str, int | None]] = {}
    for month in period:
        totals: dict[str, dict[str, int | None]] = {}
        for item in items.values():
            line = _line_for(
                session,
                ctx,
                item,
                month,
                usage_by.get((item.id, month)),
                actual_by.get((item.id, month)),
            )
            for key, field in (
                ("estimated_amount_minor", "estimated_minor"),
                ("actual_amount_minor", "actual_minor"),
                ("variance_minor", "variance_minor"),
            ):
                if line[key] is not None:
                    _accumulate(totals, line["currency"], field, line[key])
                    _accumulate(range_totals, line["currency"], field, line[key])
        rows.append(
            {
                "period_month": month.isoformat(),
                "totals": [
                    {"currency": currency, **values}
                    for currency, values in sorted(totals.items())
                ],
            }
        )

    return {
        "from_month": period[0].isoformat(),
        "to_month": period[-1].isoformat(),
        "months": rows,
        "range_totals": [
            {"currency": currency, **values}
            for currency, values in sorted(range_totals.items())
        ],
    }


def evaluate_scenarios(
    session: Session,
    ctx: TenantContext,
    *,
    period_month: date,
    scenarios: list[dict[str, Any]],
) -> dict[str, Any]:
    """Price a handful of "what if we used this much?" cases. Writes nothing.

    Planning information, not an invoice, and deliberately kept apart from the
    recorded months: a scenario never creates a usage row and never appears in a
    total. The owner asks "what would 100, 500 or 1,000 a day cost?"; the answer
    is that many usage figures put through the *configured* rate.

    Each scenario supplies either a ``usage_quantity`` directly, or the
    events/seconds/days triple that :func:`usage_hours_from_events` converts to
    hours. The conversion is arithmetic; the price it is multiplied by is a rate
    row the owner can change.
    """
    month = month_start(period_month)
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        item = session.execute(
            select(OperatingCostItem).where(
                OperatingCostItem.tenant_id == ctx.tenant_id,
                OperatingCostItem.id == scenario["cost_item_id"],
            )
        ).scalar_one_or_none()
        if item is None:
            raise NotFoundError("operating cost item not found")

        rate = effective_rate(session, ctx, cost_item_id=item.id, on_date=month)
        quantity = scenario.get("usage_quantity")
        derived_from = None
        if quantity is None and scenario.get("events_per_day") is not None:
            quantity = usage_hours_from_events(
                events_per_day=scenario["events_per_day"],
                seconds_per_event=scenario["seconds_per_event"],
                days=scenario["days"],
            )
            derived_from = {
                "events_per_day": scenario["events_per_day"],
                "seconds_per_event": str(scenario["seconds_per_event"]),
                "days": scenario["days"],
            }

        terms = RateTerms.of(rate) if rate is not None else None
        estimated = estimate_minor(terms, quantity) if terms is not None else None

        results.append(
            {
                "label": scenario.get("label"),
                "cost_item_id": str(item.id),
                "code": item.code,
                "name": item.name,
                "period_month": month.isoformat(),
                # Quantised for display exactly as a stored usage figure would be,
                # so a scenario and the month it later becomes read identically.
                "usage_quantity": (
                    str(quantity.quantize(Decimal(1).scaleb(-USAGE_QUANTITY_SCALE)))
                    if quantity is not None
                    else None
                ),
                "usage_unit": (rate.unit if rate is not None else None),
                "derived_from": derived_from,
                "estimated_amount_minor": estimated,
                "currency": rate.currency if rate is not None else ctx.currency,
                "currency_exponent": (
                    rate.currency_exponent if rate is not None else ctx.currency_exponent
                ),
                "rate": serialize_rate(rate) if rate is not None else None,
            }
        )

    totals: dict[str, dict[str, int | None]] = {}
    for result in results:
        if result["estimated_amount_minor"] is not None:
            _accumulate(
                totals, result["currency"], "estimated_minor", result["estimated_amount_minor"]
            )
    return {
        "period_month": month.isoformat(),
        "results": results,
        "totals": [
            {"currency": currency, "estimated_minor": values["estimated_minor"]}
            for currency, values in sorted(totals.items())
        ],
    }
