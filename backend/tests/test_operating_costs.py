"""Operating costs — versioned rates, estimates, real invoices, variance (P6).

The four things this file exists to prove, in order of how expensive each would
be to get wrong:

1. **A rate change never restates a month already recorded.** The terms are
   snapshotted onto the usage row; the estimate is read back, not re-derived.
2. **Nothing is invented.** No usage means no estimate; no invoice means no
   actual and no variance. Zero would be a claim, and a false one.
3. **Operating costs touch neither the customer ledger nor commission.** Three
   separate accounting concepts, and this is the one that pays providers.
4. **Money stays exact.** Integer minor units, `Decimal` usage, one half-up
   rounding rule, no float anywhere near either.

The provider prices used here are *fixtures*, chosen to match the owner's
current planning document so the arithmetic is checkable by hand. They are data
in every sense — nothing in `app/` knows any of them.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.audit.models import AuditEvent
from app.billing.models import LedgerEntry
from app.commission.models import CommissionEvent
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.ids import uuid7
from app.costs.commands import (
    CreateCostItemInput,
    CreateCostRateInput,
    RecordActualInput,
    RecordUsageInput,
    create_cost_item,
    create_cost_rate,
    record_actual,
    record_usage,
)
from app.costs.estimates import (
    RateTerms,
    effective_rate,
    estimate_minor,
    monthly_equivalent_minor,
    usage_hours_from_events,
)
from app.costs.models import (
    CostRowStatus,
    OperatingCostActual,
    OperatingCostRate,
    OperatingCostUsage,
)
from app.costs.reporting import evaluate_scenarios, month_history, month_summary

pytestmark = pytest.mark.postgres

# The tenant fixture's frozen business date is 2026-03-15 (Asia/Karachi).
MARCH = date(2026, 3, 1)
FEBRUARY = date(2026, 2, 1)
JANUARY = date(2026, 1, 1)


# --- helpers -----------------------------------------------------------------


def make_item(db, ctx, code="STT", name="Speech to text"):
    result, _, item_id = create_cost_item(
        db, ctx, CreateCostItemInput(code=code, name=name), operation_id=uuid7()
    )
    db.commit()
    return uuid.UUID(result["id"])


def usage_rate(db, ctx, item_id, *, effective_from, unit_price_minor, unit="audio_hour"):
    result, _, _ = create_cost_rate(
        db,
        ctx,
        CreateCostRateInput(
            cost_item_id=item_id,
            effective_from=effective_from,
            unit=unit,
            unit_price_minor=unit_price_minor,
            currency="USD",
            currency_exponent=2,
        ),
        operation_id=uuid7(),
    )
    db.commit()
    return uuid.UUID(result["id"])


def fixed_rate(db, ctx, item_id, *, effective_from, amount_minor, recurrence="MONTHLY"):
    result, _, _ = create_cost_rate(
        db,
        ctx,
        CreateCostRateInput(
            cost_item_id=item_id,
            effective_from=effective_from,
            fixed_amount_minor=amount_minor,
            fixed_recurrence=recurrence,
            currency="USD",
            currency_exponent=2,
        ),
        operation_id=uuid7(),
    )
    db.commit()
    return uuid.UUID(result["id"])


def add_usage(db, ctx, item_id, month, quantity, **kw):
    result, _, _ = record_usage(
        db,
        ctx,
        RecordUsageInput(
            cost_item_id=item_id,
            period_month=month,
            usage_quantity=Decimal(quantity),
            **kw,
        ),
        operation_id=uuid7(),
    )
    db.commit()
    return result


def add_actual(db, ctx, item_id, month, amount_minor, **kw):
    result, _, _ = record_actual(
        db,
        ctx,
        RecordActualInput(
            cost_item_id=item_id,
            period_month=month,
            amount_minor=amount_minor,
            **kw,
        ),
        operation_id=uuid7(),
    )
    db.commit()
    return result


def line_for(summary, code):
    return next(line for line in summary["lines"] if line["code"] == code)


# --- versioned rates ---------------------------------------------------------


class TestVersionedRates:
    """A rate is data with a lifetime, and its lifetime is unambiguous."""

    def test_a_new_rate_closes_its_predecessor_rather_than_editing_it(
        self, db, tenant_a
    ):
        item = make_item(db, tenant_a.ctx)
        first = usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        second = usage_rate(db, tenant_a.ctx, item, effective_from=MARCH, unit_price_minor=30)

        old = db.get(OperatingCostRate, first)
        new = db.get(OperatingCostRate, second)
        # The predecessor acquires an end date. Its price is untouched.
        assert old.effective_to == date(2026, 2, 28)
        assert old.unit_price_minor == 22
        assert new.effective_to is None and new.unit_price_minor == 30

    def test_overlapping_ranges_are_refused(self, db, tenant_a):
        """Two rates covering one day would make "the rate in force" a choice."""
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=MARCH, unit_price_minor=22)
        with pytest.raises(ConflictError) as exc:
            usage_rate(db, tenant_a.ctx, item, effective_from=FEBRUARY, unit_price_minor=30)
        assert exc.value.code == "COST_RATE_OVERLAP"

    def test_the_database_refuses_an_overlap_by_direct_sql(self, db, tenant_a):
        """The EXCLUDE constraint is the guarantee, not the application check."""
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO operating_cost_rate
                      (id, tenant_id, cost_item_id, effective_from, effective_to,
                       unit, unit_price_minor, currency, currency_exponent,
                       created_by_user_id, created_at)
                    VALUES (gen_random_uuid(), :tenant, :item, '2026-06-01', NULL,
                            'audio_hour', 30, 'USD', 2, :user, now())
                    """
                ),
                {
                    "tenant": str(tenant_a.ctx.tenant_id),
                    "item": str(item),
                    "user": str(tenant_a.owner.id),
                },
            )
        assert "ex_operating_cost_rate_effective_range_no_overlap" in str(exc.value)
        db.rollback()

    def test_two_items_may_hold_rates_over_the_same_dates(self, db, tenant_a):
        """The exclusion is per cost item; providers are priced independently."""
        a = make_item(db, tenant_a.ctx, code="A")
        b = make_item(db, tenant_a.ctx, code="B")
        usage_rate(db, tenant_a.ctx, a, effective_from=JANUARY, unit_price_minor=22)
        usage_rate(db, tenant_a.ctx, b, effective_from=JANUARY, unit_price_minor=99)
        assert effective_rate(
            db, tenant_a.ctx, cost_item_id=b.hex and b, on_date=MARCH
        ).unit_price_minor == 99

    def test_the_rate_for_a_month_is_the_one_in_force_on_its_first_day(
        self, db, tenant_a
    ):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        usage_rate(db, tenant_a.ctx, item, effective_from=MARCH, unit_price_minor=30)

        assert effective_rate(
            db, tenant_a.ctx, cost_item_id=item, on_date=FEBRUARY
        ).unit_price_minor == 22
        assert effective_rate(
            db, tenant_a.ctx, cost_item_id=item, on_date=MARCH
        ).unit_price_minor == 30

    def test_a_rate_change_never_restates_a_month_already_recorded(self, db, tenant_a):
        """The point of snapshotting, stated as the defect it prevents.

        February's estimate was computed at February's price. Introducing a new
        price in March must leave February exactly as the owner reviewed it.
        """
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        add_usage(db, tenant_a.ctx, item, FEBRUARY, "20.833333")
        before = line_for(
            month_summary(db, tenant_a.ctx, period_month=FEBRUARY), "STT"
        )["estimated_amount_minor"]

        usage_rate(db, tenant_a.ctx, item, effective_from=MARCH, unit_price_minor=300)

        after = line_for(
            month_summary(db, tenant_a.ctx, period_month=FEBRUARY), "STT"
        )["estimated_amount_minor"]
        assert after == before == 458

    def test_a_rate_needs_exactly_one_pricing_shape(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        for kwargs in (
            {},  # neither
            {"unit": "hour", "unit_price_minor": 5, "fixed_amount_minor": 100,
             "fixed_recurrence": "MONTHLY"},  # both
        ):
            with pytest.raises(ValidationFailed):
                create_cost_rate(
                    db,
                    tenant_a.ctx,
                    CreateCostRateInput(
                        cost_item_id=item, effective_from=MARCH, **kwargs
                    ),
                    operation_id=uuid7(),
                )
            db.rollback()

    def test_a_usage_rate_without_a_unit_is_refused(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        with pytest.raises(ValidationFailed) as exc:
            create_cost_rate(
                db,
                tenant_a.ctx,
                CreateCostRateInput(
                    cost_item_id=item, effective_from=MARCH, unit_price_minor=22
                ),
                operation_id=uuid7(),
            )
        assert "unit" in exc.value.field_errors

    def test_a_provider_currency_is_carried_not_converted(self, db, tenant_a):
        """P6 §18: the tenant bills in PKR; the provider invoices in USD.

        No FX source exists, so nothing is converted and the two never merge into
        one total.
        """
        assert tenant_a.ctx.currency == "PKR"
        item = make_item(db, tenant_a.ctx)
        rate_id = usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        assert db.get(OperatingCostRate, rate_id).currency == "USD"

        add_usage(db, tenant_a.ctx, item, MARCH, "10")
        totals = month_summary(db, tenant_a.ctx, period_month=MARCH)["totals"]
        assert [t["currency"] for t in totals] == ["USD"]


# --- estimate arithmetic ------------------------------------------------------


class TestEstimateArithmetic:
    """One rounding rule, exact decimals, and no float anywhere."""

    def test_the_owners_worked_examples(self, db, tenant_a):
        """The three planning scenarios, computed from a rate row.

        100 / 500 / 1000 commands a day at 5 seconds each over 30 days is
        4.1667 / 20.8333 / 41.6667 audio hours. At $0.22 an hour that is
        $0.92 / $4.58 / $9.17 — the figures in the owner's document, reproduced
        from data rather than from a constant.
        """
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)

        expected = {100: (Decimal("4.166667"), 92), 500: (Decimal("20.833333"), 458),
                    1000: (Decimal("41.666667"), 917)}
        for per_day, (hours, minor) in expected.items():
            measured = usage_hours_from_events(
                events_per_day=per_day, seconds_per_event=Decimal("5"), days=30
            )
            assert measured.quantize(Decimal("0.000001")) == hours
            result = add_usage(db, tenant_a.ctx, item, MARCH, str(hours),
                               correction_reason="rerun" if per_day != 100 else None)
            assert result["estimated_amount_minor"] == minor

    def test_an_annual_charge_is_shown_as_a_twelfth(self, db, tenant_a):
        """The "(annual domain cost / 12)" term of the owner's formula.

        Normalised once, on the server, by the shared half-up rule — no screen
        divides money.
        """
        assert monthly_equivalent_minor(1200, "ANNUAL") == 100
        # 1000 / 12 = 83.33...; half-up on the minor unit gives 83.
        assert monthly_equivalent_minor(1000, "ANNUAL") == 83
        assert monthly_equivalent_minor(1000, "MONTHLY") == 1000

    def test_a_fixed_item_needs_no_usage_figure(self, db, tenant_a):
        """Its monthly cost is the rate itself, so asking for usage is refused."""
        item = make_item(db, tenant_a.ctx, code="DOMAIN", name="Domain")
        fixed_rate(db, tenant_a.ctx, item, effective_from=JANUARY,
                   amount_minor=1200, recurrence="ANNUAL")

        line = line_for(month_summary(db, tenant_a.ctx, period_month=MARCH), "DOMAIN")
        assert line["estimated_amount_minor"] == 100

        with pytest.raises(ValidationFailed):
            add_usage(db, tenant_a.ctx, item, MARCH, "1")
        db.rollback()

    def test_usage_must_be_exact_and_never_a_float(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)

        with pytest.raises(ValidationFailed):
            record_usage(
                db,
                tenant_a.ctx,
                RecordUsageInput(
                    cost_item_id=item, period_month=MARCH, usage_quantity=20.8333
                ),
                operation_id=uuid7(),
            )
        db.rollback()

        with pytest.raises(ValidationFailed):
            record_usage(
                db,
                tenant_a.ctx,
                RecordUsageInput(
                    cost_item_id=item,
                    period_month=MARCH,
                    usage_quantity=Decimal("1.1234567"),  # 7 decimals
                ),
                operation_id=uuid7(),
            )
        db.rollback()

    def test_the_estimate_is_an_integer_count_of_minor_units(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        result = add_usage(db, tenant_a.ctx, item, MARCH, "20.833333")
        assert isinstance(result["estimated_amount_minor"], int)
        # And the quantity crosses the wire as a string, never a JSON number.
        assert isinstance(result["usage_quantity"], str)

    def test_no_usage_means_no_estimate_rather_than_zero(self, db, tenant_a):
        """Zero would say the provider was free. Nobody has said anything yet."""
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        line = line_for(month_summary(db, tenant_a.ctx, period_month=MARCH), "STT")
        assert line["estimated_amount_minor"] is None

    def test_usage_for_a_month_with_no_rate_is_refused(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=MARCH, unit_price_minor=22)
        with pytest.raises(ValidationFailed):
            add_usage(db, tenant_a.ctx, item, JANUARY, "5")
        db.rollback()

    def test_a_future_month_cannot_be_measured_or_invoiced(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        for call in (
            lambda: add_usage(db, tenant_a.ctx, item, date(2026, 12, 1), "5"),
            lambda: add_actual(db, tenant_a.ctx, item, date(2026, 12, 1), 500),
        ):
            with pytest.raises(ValidationFailed):
                call()
            db.rollback()


# --- actual invoices and variance --------------------------------------------


class TestActualsAndVariance:
    def test_variance_is_actual_minus_estimated(self, db, tenant_a):
        """The worked example from the brief: 20.8 hours, $4.58 vs $4.71."""
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        add_usage(db, tenant_a.ctx, item, MARCH, "20.833333")
        add_actual(db, tenant_a.ctx, item, MARCH, 471, invoice_reference="INV-1")

        line = line_for(month_summary(db, tenant_a.ctx, period_month=MARCH), "STT")
        assert line["estimated_amount_minor"] == 458
        assert line["actual_amount_minor"] == 471
        assert line["variance_minor"] == 13

    def test_no_invoice_means_no_actual_and_no_variance(self, db, tenant_a):
        """An invoice that has not arrived is not an invoice for nothing."""
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        add_usage(db, tenant_a.ctx, item, MARCH, "20.833333")

        line = line_for(month_summary(db, tenant_a.ctx, period_month=MARCH), "STT")
        assert line["actual_amount_minor"] is None
        assert line["variance_minor"] is None

    def test_an_invoice_with_no_estimate_still_shows_but_has_no_variance(
        self, db, tenant_a
    ):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        add_actual(db, tenant_a.ctx, item, MARCH, 471)

        line = line_for(month_summary(db, tenant_a.ctx, period_month=MARCH), "STT")
        assert line["actual_amount_minor"] == 471
        assert line["estimated_amount_minor"] is None
        assert line["variance_minor"] is None

    def test_a_zero_invoice_is_recordable_and_is_not_the_same_as_none(
        self, db, tenant_a
    ):
        """A bundled first-year domain really does cost nothing."""
        item = make_item(db, tenant_a.ctx, code="DOMAIN", name="Domain")
        fixed_rate(db, tenant_a.ctx, item, effective_from=JANUARY,
                   amount_minor=1200, recurrence="ANNUAL")
        add_actual(db, tenant_a.ctx, item, MARCH, 0, note="bundled with hosting")

        line = line_for(month_summary(db, tenant_a.ctx, period_month=MARCH), "DOMAIN")
        assert line["actual_amount_minor"] == 0
        assert line["variance_minor"] == -100

    def test_correcting_an_invoice_supersedes_it_and_needs_a_reason(
        self, db, tenant_a
    ):
        """AUD-1/2/3/6: the wrong figure survives, carrying why it was replaced."""
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        first = add_actual(db, tenant_a.ctx, item, MARCH, 471)

        with pytest.raises(ValidationFailed) as exc:
            add_actual(db, tenant_a.ctx, item, MARCH, 481)
        assert "correction_reason" in exc.value.field_errors
        db.rollback()

        second = add_actual(
            db, tenant_a.ctx, item, MARCH, 481,
            correction_reason="provider reissued the invoice",
        )

        old = db.get(OperatingCostActual, uuid.UUID(first["id"]))
        new = db.get(OperatingCostActual, uuid.UUID(second["id"]))
        assert old.status == CostRowStatus.SUPERSEDED
        assert old.amount_minor == 471, "the original figure is never edited"
        assert old.correction_reason == "provider reissued the invoice"
        assert old.superseded_by_id == new.id
        assert new.supersedes_id == old.id and new.status == CostRowStatus.ACTIVE

        line = line_for(month_summary(db, tenant_a.ctx, period_month=MARCH), "STT")
        assert line["actual_amount_minor"] == 481

    def test_correcting_a_usage_figure_supersedes_it_and_needs_a_reason(
        self, db, tenant_a
    ):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        first = add_usage(db, tenant_a.ctx, item, MARCH, "20.833333")

        with pytest.raises(ValidationFailed):
            add_usage(db, tenant_a.ctx, item, MARCH, "25")
        db.rollback()

        add_usage(db, tenant_a.ctx, item, MARCH, "25",
                  correction_reason="recount from the provider console")

        old = db.get(OperatingCostUsage, uuid.UUID(first["id"]))
        assert old.status == CostRowStatus.SUPERSEDED
        assert old.estimated_amount_minor == 458
        line = line_for(month_summary(db, tenant_a.ctx, period_month=MARCH), "STT")
        assert line["estimated_amount_minor"] == 550

    def test_there_is_no_hard_delete_path_for_cost_history(self):
        """AUD-1 for the P6 tables: no route, no ORM delete, no raw DML."""
        import ast

        from tests._source import APP_ROOT, python_files

        for path in python_files():
            source = path.read_text(encoding="utf-8").lower()
            for table in (
                "operating_cost_usage",
                "operating_cost_actual",
                "operating_cost_rate",
                "operating_cost_item",
            ):
                assert f"delete from {table}" not in source, path

            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "attr", None)
                    if name == "delete" and "OperatingCost" in ast.dump(node):
                        raise AssertionError(f"delete() against cost history in {path}")
        assert APP_ROOT.exists()


# --- month history ------------------------------------------------------------


class TestHistory:
    def test_month_by_month_totals_are_ordered_oldest_first(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        add_usage(db, tenant_a.ctx, item, JANUARY, "10")
        add_usage(db, tenant_a.ctx, item, FEBRUARY, "20")
        add_actual(db, tenant_a.ctx, item, FEBRUARY, 450)

        history = month_history(db, tenant_a.ctx, latest_month=MARCH, months=3)
        assert [m["period_month"] for m in history["months"]] == [
            "2026-01-01",
            "2026-02-01",
            "2026-03-01",
        ]
        jan, feb, mar = history["months"]
        assert jan["totals"][0]["estimated_minor"] == 220
        assert feb["totals"][0]["estimated_minor"] == 440
        assert feb["totals"][0]["actual_minor"] == 450
        assert feb["totals"][0]["variance_minor"] == 10
        assert mar["totals"] == []

        assert history["range_totals"][0]["currency"] == "USD"
        assert history["range_totals"][0]["estimated_minor"] == 660

    def test_totals_stay_separated_by_currency(self, db, tenant_a):
        """Two providers, two currencies, and no invented exchange rate."""
        usd = make_item(db, tenant_a.ctx, code="STT")
        usage_rate(db, tenant_a.ctx, usd, effective_from=JANUARY, unit_price_minor=22)
        add_usage(db, tenant_a.ctx, usd, MARCH, "10")

        local = make_item(db, tenant_a.ctx, code="VPS")
        create_cost_rate(
            db,
            tenant_a.ctx,
            CreateCostRateInput(
                cost_item_id=local,
                effective_from=JANUARY,
                fixed_amount_minor=500000,
                fixed_recurrence="MONTHLY",
            ),  # no currency => the tenant's own
            operation_id=uuid7(),
        )
        db.commit()

        totals = month_summary(db, tenant_a.ctx, period_month=MARCH)["totals"]
        assert {t["currency"]: t["estimated_minor"] for t in totals} == {
            "USD": 220,
            "PKR": 500000,
        }


# --- scenarios ----------------------------------------------------------------


class TestScenarioCalculator:
    def test_three_scenarios_price_from_the_configured_rate(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)

        answer = evaluate_scenarios(
            db,
            tenant_a.ctx,
            period_month=MARCH,
            scenarios=[
                {"label": label, "cost_item_id": item, "events_per_day": n,
                 "seconds_per_event": Decimal("5"), "days": 30}
                for label, n in (("Starting", 100), ("Reasonable", 500), ("Larger", 1000))
            ],
        )
        assert [r["estimated_amount_minor"] for r in answer["results"]] == [92, 458, 917]
        assert [r["usage_quantity"] for r in answer["results"]] == [
            "4.166667",
            "20.833333",
            "41.666667",
        ]
        assert answer["results"][0]["usage_unit"] == "audio_hour"

    def test_changing_the_rate_changes_the_scenario_without_a_code_change(
        self, db, tenant_a
    ):
        """The whole reason the price is a row: the calculator is generic."""
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        cheap = evaluate_scenarios(
            db, tenant_a.ctx, period_month=FEBRUARY,
            scenarios=[{"cost_item_id": item, "usage_quantity": Decimal("100")}],
        )["results"][0]["estimated_amount_minor"]

        usage_rate(db, tenant_a.ctx, item, effective_from=MARCH, unit_price_minor=44)
        dear = evaluate_scenarios(
            db, tenant_a.ctx, period_month=MARCH,
            scenarios=[{"cost_item_id": item, "usage_quantity": Decimal("100")}],
        )["results"][0]["estimated_amount_minor"]

        assert (cheap, dear) == (2200, 4400)

    def test_a_scenario_writes_nothing(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        evaluate_scenarios(
            db, tenant_a.ctx, period_month=MARCH,
            scenarios=[{"cost_item_id": item, "usage_quantity": Decimal("100")}],
        )
        assert db.execute(select(OperatingCostUsage)).scalars().all() == []
        line = line_for(month_summary(db, tenant_a.ctx, period_month=MARCH), "STT")
        assert line["estimated_amount_minor"] is None

    def test_a_scenario_for_an_unknown_item_is_not_found(self, db, tenant_a):
        with pytest.raises(NotFoundError):
            evaluate_scenarios(
                db, tenant_a.ctx, period_month=MARCH,
                scenarios=[{"cost_item_id": uuid7(), "usage_quantity": Decimal("1")}],
            )


# --- separation from the ledger and from commission ---------------------------


class TestOperatingCostsAreASeparateAccountingConcept:
    """The invariant P6 must never break, asserted from both sides."""

    def test_recording_costs_creates_no_ledger_entry_and_no_commission(
        self, db, tenant_a, customer_factory
    ):
        customer_factory(tenant_a.ctx, code="C1")
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        add_usage(db, tenant_a.ctx, item, MARCH, "20.833333")
        add_actual(db, tenant_a.ctx, item, MARCH, 471)

        assert db.execute(select(LedgerEntry)).scalars().all() == []
        assert db.execute(select(CommissionEvent)).scalars().all() == []

    def test_costs_do_not_move_any_customer_outstanding(
        self, db, tenant_a, customer_factory
    ):
        from app.billing.ledger import outstanding_minor
        from app.service.commands import RecordServiceInput, record_service

        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=25000)
        record_service(
            db, tenant_a.ctx,
            RecordServiceInput(customer_id=customer.id, quantity=Decimal("2")),
            operation_id=uuid7(),
        )
        db.commit()
        before = outstanding_minor(db, tenant_a.ctx, customer.id)

        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        add_usage(db, tenant_a.ctx, item, MARCH, "1000")
        add_actual(db, tenant_a.ctx, item, MARCH, 999999)

        assert outstanding_minor(db, tenant_a.ctx, customer.id) == before == 50000

    def test_the_cost_module_imports_neither_ledger_nor_commission(self):
        """Structural, not incidental: it cannot reach either one."""
        import ast

        from tests._source import APP_ROOT

        for name in ("commands.py", "estimates.py", "reporting.py", "models.py"):
            tree = ast.parse((APP_ROOT / "costs" / name).read_text(encoding="utf-8"))
            imports = [
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            ]
            assert not any("commission" in m for m in imports), name
            assert not any("ledger" in m for m in imports), name

    def test_no_cost_table_carries_a_row_version(self):
        """They are not sync entities; the Operating Costs screen is online-only."""
        from app.costs import models as cost_models

        for model in (
            cost_models.OperatingCostItem,
            cost_models.OperatingCostRate,
            cost_models.OperatingCostUsage,
            cost_models.OperatingCostActual,
        ):
            assert "row_version" not in model.__table__.columns.keys(), model.__name__


# --- audit --------------------------------------------------------------------


class TestAudit:
    def test_every_important_cost_mutation_is_audited(self, db, tenant_a):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        usage_rate(db, tenant_a.ctx, item, effective_from=MARCH, unit_price_minor=30)
        add_usage(db, tenant_a.ctx, item, MARCH, "10")
        add_actual(db, tenant_a.ctx, item, MARCH, 350)
        add_actual(db, tenant_a.ctx, item, MARCH, 360, correction_reason="reissued")

        actions = [
            e.action
            for e in db.execute(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == tenant_a.ctx.tenant_id)
                .order_by(AuditEvent.occurred_at)
            ).scalars()
        ]
        assert "operating_cost_item.created" in actions
        assert actions.count("operating_cost_rate.created") == 2
        assert "operating_cost_rate.closed" in actions
        assert "operating_cost_usage.recorded" in actions
        assert "operating_cost_actual.recorded" in actions
        assert "operating_cost_actual.corrected" in actions

    def test_a_correction_records_the_before_the_after_and_the_reason(
        self, db, tenant_a
    ):
        item = make_item(db, tenant_a.ctx)
        usage_rate(db, tenant_a.ctx, item, effective_from=JANUARY, unit_price_minor=22)
        add_actual(db, tenant_a.ctx, item, MARCH, 350)
        add_actual(db, tenant_a.ctx, item, MARCH, 360, correction_reason="reissued")

        event = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "operating_cost_actual.corrected"
            )
        ).scalar_one()
        assert event.before["amount_minor"] == 350
        assert event.after["amount_minor"] == 360
        assert event.reason == "reissued"
        assert event.actor_user_id == tenant_a.owner.id
        assert event.source == "ONLINE"


# --- idempotency over HTTP ----------------------------------------------------


class TestOverHttp:
    def test_a_replayed_cost_write_creates_nothing_new(self, client, tenant_a):
        item = client.post(
            "/api/v1/operating-costs/items",
            json={"operation_id": str(uuid7()), "code": "STT", "name": "Speech"},
            headers=tenant_a.auth,
        ).json()["entity"]

        client.post(
            f"/api/v1/operating-costs/items/{item['id']}/rates",
            json={
                "operation_id": str(uuid7()),
                "effective_from": "2026-01-01",
                "unit": "audio_hour",
                "unit_price_minor": 22,
                "currency": "USD",
            },
            headers=tenant_a.auth,
        )

        op = str(uuid7())
        body = {
            "operation_id": op,
            "cost_item_id": item["id"],
            "period_month": "2026-03-01",
            "usage_quantity": "20.833333",
            "inputs": {"commands_per_day": 500, "seconds_per_command": "5"},
        }
        first = client.post("/api/v1/operating-costs/usage", json=body, headers=tenant_a.auth)
        second = client.post("/api/v1/operating-costs/usage", json=body, headers=tenant_a.auth)

        assert first.json()["status"] == "APPLIED"
        assert second.json()["status"] == "DUPLICATE"
        assert first.json()["entity"]["id"] == second.json()["entity"]["id"]
        assert first.json()["entity"]["estimated_amount_minor"] == 458

        summary = client.get(
            "/api/v1/operating-costs/summary?month=2026-03-01", headers=tenant_a.auth
        ).json()
        assert len(summary["lines"]) == 1
        assert summary["lines"][0]["usage_inputs"]["commands_per_day"] == 500

    def test_the_scenario_route_prices_the_three_default_cases(self, client, tenant_a):
        item = client.post(
            "/api/v1/operating-costs/items",
            json={"operation_id": str(uuid7()), "code": "STT", "name": "Speech"},
            headers=tenant_a.auth,
        ).json()["entity"]
        client.post(
            f"/api/v1/operating-costs/items/{item['id']}/rates",
            json={
                "operation_id": str(uuid7()),
                "effective_from": "2026-01-01",
                "unit": "audio_hour",
                "unit_price_minor": 22,
                "currency": "USD",
            },
            headers=tenant_a.auth,
        )

        resp = client.post(
            "/api/v1/operating-costs/scenarios",
            json={
                "period_month": "2026-03-01",
                "scenarios": [
                    {
                        "label": label,
                        "cost_item_id": item["id"],
                        "events_per_day": n,
                        "seconds_per_event": "5",
                        "days": 30,
                    }
                    for label, n in (("Starting", 100), ("Reasonable", 500), ("Larger", 1000))
                ],
            },
            headers=tenant_a.auth,
        )
        assert resp.status_code == 200, resp.text
        assert [r["estimated_amount_minor"] for r in resp.json()["results"]] == [
            92,
            458,
            917,
        ]
