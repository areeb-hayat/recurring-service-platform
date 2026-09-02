"""The four reporting derivations (P0 §11.1) and the derived customer status.

Covers FIN-4, FIN-5, FIN-11, FIN-14, FIN-15 and FIN-16. The centrepiece is
A-FIN-14: a 1000 charge, a 500 payment, then that payment voided must leave
outstanding at 1000, business generated at **1000 and not 1500**, and collected
at 0. Every other test here defends the same distinction from a different angle.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.billing.cycles import open_cycle
from app.billing.ledger import outstanding_minor
from app.billing.models import EntryKind, LedgerEntry, SourceType
from app.billing.reporting import (
    PaymentState,
    billed_value_minor,
    business_generated_minor,
    collected_minor,
    customer_payment_status,
    outstanding_total_minor,
    reporting_totals,
)
from app.service.models import DailyServiceRecord, RecordStatus
from tests._ops import (
    close_after_period_end,
    ctx_at,
    do_correct,
    do_pay,
    do_record,
    do_void,
    do_void_payment,
)

pytestmark = pytest.mark.postgres


def _utc(y, m, d, hour=7):
    return datetime(y, m, d, hour, tzinfo=timezone.utc)


def _active_id(db, ctx, customer):
    return db.execute(
        select(DailyServiceRecord.id).where(
            DailyServiceRecord.tenant_id == ctx.tenant_id,
            DailyServiceRecord.customer_id == customer.id,
            DailyServiceRecord.status == RecordStatus.ACTIVE,
        )
    ).scalar_one()


class TestAFIN14TheExactRegressionCase:
    """The worked example from P0 §11.1, asserted figure by figure."""

    @pytest.fixture
    def scenario(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))  # charge 1000
        payment = do_pay(db, tenant_a.ctx, customer, 500)
        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="bounced")
        return customer

    def test_outstanding_returns_to_1000(self, db, tenant_a, scenario):
        assert outstanding_minor(db, tenant_a.ctx, scenario.id) == 1000
        assert outstanding_total_minor(db, tenant_a.ctx, customer_id=scenario.id) == 1000

    def test_business_generated_is_1000_not_1500(self, db, tenant_a, scenario):
        assert business_generated_minor(db, tenant_a.ctx, customer_id=scenario.id) == 1000

    def test_collected_falls_back_to_zero(self, db, tenant_a, scenario):
        assert collected_minor(db, tenant_a.ctx, customer_id=scenario.id) == 0

    def test_the_void_produced_a_payment_origin_adjustment_of_plus_500(
        self, db, tenant_a, scenario
    ):
        adjustments = (
            db.execute(
                select(LedgerEntry).where(
                    LedgerEntry.customer_id == scenario.id,
                    LedgerEntry.entry_kind == EntryKind.ADJUSTMENT,
                )
            )
            .scalars()
            .all()
        )
        assert len(adjustments) == 1
        assert adjustments[0].amount_minor == 500
        assert adjustments[0].source_type == SourceType.PAYMENT

    def test_no_service_origin_row_changed(self, db, tenant_a, scenario):
        service_rows = (
            db.execute(
                select(LedgerEntry).where(
                    LedgerEntry.customer_id == scenario.id,
                    LedgerEntry.source_type == SourceType.DAILY_SERVICE_RECORD,
                )
            )
            .scalars()
            .all()
        )
        assert [(e.entry_kind, e.amount_minor) for e in service_rows] == [
            (EntryKind.CHARGE, 1000)
        ]

    def test_the_four_figures_are_all_distinct_and_none_derives_another(
        self, db, tenant_a, scenario
    ):
        totals = reporting_totals(db, tenant_a.ctx, customer_id=scenario.id)
        # Nothing has been billed yet: the cycle is still open.
        assert totals.billed_value_minor == 0
        assert totals.business_generated_minor == 1000
        assert totals.collected_minor == 0
        assert totals.outstanding_minor == 1000


class TestFIN16CollectionsMoveIndependently:
    def test_FIN16_collected_moves_500_then_back_to_0(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        payment = do_pay(db, tenant_a.ctx, customer, 500)
        assert collected_minor(db, tenant_a.ctx, customer_id=customer.id) == 500
        assert business_generated_minor(db, tenant_a.ctx, customer_id=customer.id) == 1000

        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="reversed")
        assert collected_minor(db, tenant_a.ctx, customer_id=customer.id) == 0
        assert business_generated_minor(db, tenant_a.ctx, customer_id=customer.id) == 1000

    def test_a_service_correction_moves_business_generated_but_not_collected(
        self, db, tenant_a, customer_factory
    ):
        """The mirror image of the rule: origin decides, in both directions."""
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("3"))  # 3000
        do_pay(db, tenant_a.ctx, customer, 1000)
        assert business_generated_minor(db, tenant_a.ctx, customer_id=customer.id) == 3000
        assert collected_minor(db, tenant_a.ctx, customer_id=customer.id) == 1000

        do_correct(
            db,
            tenant_a.ctx,
            _active_id(db, tenant_a.ctx, customer),
            quantity=Decimal("1"),
            reason="over-recorded",
        )
        assert business_generated_minor(db, tenant_a.ctx, customer_id=customer.id) == 1000
        assert collected_minor(db, tenant_a.ctx, customer_id=customer.id) == 1000

    def test_a_voided_service_record_zeroes_business_generated(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        do_void(db, tenant_a.ctx, _active_id(db, tenant_a.ctx, customer), reason="not delivered")
        assert business_generated_minor(db, tenant_a.ctx, customer_id=customer.id) == 0
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == 0


class TestFIN15BilledValue:
    def test_A_FIN_15_billed_counts_only_the_issued_statement(
        self, db, tenant_a, customer_factory
    ):
        """One closed cycle plus further service in the open cycle: the two
        figures differ, and neither is computed from the other."""
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        january = ctx_at(tenant_a, _utc(2026, 1, 20))
        do_record(db, january, customer, quantity=Decimal("4"), service_date=date(2026, 1, 5))
        close_after_period_end(db, tenant_a, open_cycle(db, january))

        february = ctx_at(tenant_a, _utc(2026, 2, 10))
        do_record(db, february, customer, quantity=Decimal("3"), service_date=date(2026, 2, 3))

        billed = billed_value_minor(db, february, customer_id=customer.id)
        generated = business_generated_minor(db, february, customer_id=customer.id)
        assert billed == 4000
        assert generated == 7000
        assert billed != generated

    def test_billed_value_excludes_payment_reversals(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        payment = do_pay(db, tenant_a.ctx, customer, 500)
        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="reversed")
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)

        assert billed_value_minor(db, tenant_a.ctx, customer_id=customer.id) == 2000

    def test_a_late_correction_is_billed_in_the_later_cycle(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        january = ctx_at(tenant_a, _utc(2026, 1, 20))
        do_record(db, january, customer, quantity=Decimal("4"), service_date=date(2026, 1, 5))
        jan_cycle = open_cycle(db, january)
        record_id = _active_id(db, january, customer)
        close_after_period_end(db, tenant_a, jan_cycle)

        february = ctx_at(tenant_a, _utc(2026, 2, 10))
        do_correct(db, february, record_id, quantity=Decimal("1"), reason="over-recorded")
        feb_cycle = open_cycle(db, february)
        close_after_period_end(db, tenant_a, feb_cycle)

        assert billed_value_minor(db, february, cycle_id=jan_cycle.id) == 4000
        assert billed_value_minor(db, february, cycle_id=feb_cycle.id) == -3000
        assert billed_value_minor(db, february, customer_id=customer.id) == 1000
        assert business_generated_minor(db, february, customer_id=customer.id) == 1000


class TestTenantScoping:
    def test_every_derivation_is_tenant_scoped(
        self, db, tenant_a, tenant_b, customer_factory
    ):
        a = customer_factory(tenant_a.ctx, price_minor=1000)
        b = customer_factory(tenant_b.ctx, price_minor=1000)
        do_record(db, tenant_a.ctx, a, quantity=Decimal("5"))
        do_pay(db, tenant_a.ctx, a, 2000)
        do_record(db, tenant_b.ctx, b, quantity=Decimal("1"))

        assert business_generated_minor(db, tenant_a.ctx) == 5000
        assert business_generated_minor(db, tenant_b.ctx) == 1000
        assert collected_minor(db, tenant_a.ctx) == 2000
        assert collected_minor(db, tenant_b.ctx) == 0
        assert outstanding_total_minor(db, tenant_b.ctx) == 1000

    def test_tenant_wide_totals_sum_the_customers(
        self, db, tenant_a, customer_factory
    ):
        first = customer_factory(tenant_a.ctx, code="R1", price_minor=1000)
        second = customer_factory(tenant_a.ctx, code="R2", price_minor=2000)
        do_record(db, tenant_a.ctx, first, quantity=Decimal("2"))
        do_record(db, tenant_a.ctx, second, quantity=Decimal("3"))
        assert business_generated_minor(db, tenant_a.ctx) == 8000
        assert (
            business_generated_minor(db, tenant_a.ctx, customer_id=first.id)
            + business_generated_minor(db, tenant_a.ctx, customer_id=second.id)
            == 8000
        )


class TestFIN11DerivedStatus:
    def test_status_is_recomputed_from_the_ledger_on_every_call(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        assert customer_payment_status(db, tenant_a.ctx, customer.id) == PaymentState.PAID
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        assert customer_payment_status(db, tenant_a.ctx, customer.id) == PaymentState.UNPAID
        do_pay(db, tenant_a.ctx, customer, 400)
        assert (
            customer_payment_status(db, tenant_a.ctx, customer.id)
            == PaymentState.PARTIALLY_PAID
        )
        do_pay(db, tenant_a.ctx, customer, 600)
        assert customer_payment_status(db, tenant_a.ctx, customer.id) == PaymentState.PAID

    def test_a_debt_carried_into_a_new_cycle_with_no_payment_reads_unpaid(
        self, db, tenant_a, customer_factory
    ):
        """A payment made last cycle does not make this cycle partially paid."""
        customer = customer_factory(tenant_a.ctx, price_minor=1000)
        january = ctx_at(tenant_a, _utc(2026, 1, 20))
        do_record(db, january, customer, quantity=Decimal("4"), service_date=date(2026, 1, 5))
        do_pay(db, january, customer, 1000, received_on=date(2026, 1, 6))
        close_after_period_end(db, tenant_a, open_cycle(db, january))

        february = ctx_at(tenant_a, _utc(2026, 2, 10))
        do_record(db, february, customer, quantity=Decimal("1"), service_date=date(2026, 2, 3))
        assert customer_payment_status(db, february, customer.id) == PaymentState.UNPAID

        do_pay(db, february, customer, 500, received_on=date(2026, 2, 4))
        assert (
            customer_payment_status(db, february, customer.id)
            == PaymentState.PARTIALLY_PAID
        )


class TestFIN4And5Properties:
    """A-FIN-4/5: a random history always reconciles, and the two report families
    stay invariant under the other's adjustments."""

    def _random_history(self, db, ctx, customer_factory, seed):
        rng = random.Random(seed)
        customer = customer_factory(ctx, code=f"P{seed}", price_minor=rng.randrange(1, 5000))
        payments: list[str] = []
        for step in range(30):
            action = rng.choice(["record", "skip", "correct", "void", "pay", "void_pay"])
            service_date = ctx.today - timedelta(days=rng.randrange(0, 14))
            record_id = db.execute(
                select(DailyServiceRecord.id).where(
                    DailyServiceRecord.tenant_id == ctx.tenant_id,
                    DailyServiceRecord.customer_id == customer.id,
                    DailyServiceRecord.status == RecordStatus.ACTIVE,
                )
            ).scalars().first()
            try:
                if action == "record":
                    do_record(
                        db,
                        ctx,
                        customer,
                        quantity=Decimal(rng.randrange(1, 9000)).scaleb(-3),
                        service_date=service_date,
                    )
                elif action == "skip":
                    do_record(db, ctx, customer, kind="SKIP", service_date=service_date)
                elif action == "correct" and record_id:
                    do_correct(
                        db,
                        ctx,
                        record_id,
                        quantity=Decimal(rng.randrange(1, 9000)).scaleb(-3),
                        reason=f"correction {step}",
                    )
                elif action == "void" and record_id:
                    do_void(db, ctx, record_id, reason=f"void {step}")
                elif action == "pay":
                    outcome = do_pay(db, ctx, customer, rng.randrange(1, 20000))
                    payments.append(outcome.result["id"])
                elif action == "void_pay" and payments:
                    do_void_payment(db, ctx, payments.pop(), reason=f"reversal {step}")
            except Exception:
                db.rollback()  # a legal refusal (duplicate day, already voided)
        return customer

    @pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
    def test_FIN5_outstanding_always_reconciles(
        self, db, tenant_a, customer_factory, seed
    ):
        customer = self._random_history(db, tenant_a.ctx, customer_factory, seed)
        rows = (
            db.execute(
                select(LedgerEntry).where(LedgerEntry.customer_id == customer.id)
            )
            .scalars()
            .all()
        )
        charges = sum(e.amount_minor for e in rows if e.entry_kind == EntryKind.CHARGE)
        adjustments = sum(
            e.amount_minor for e in rows if e.entry_kind == EntryKind.ADJUSTMENT
        )
        payments = sum(e.amount_minor for e in rows if e.entry_kind == EntryKind.PAYMENT)
        assert outstanding_minor(db, tenant_a.ctx, customer.id) == (
            charges + adjustments + payments
        )

    @pytest.mark.parametrize("seed", [6, 7, 8, 9, 10])
    def test_FIN14_16_each_figure_ignores_the_other_origin(
        self, db, tenant_a, customer_factory, seed
    ):
        customer = self._random_history(db, tenant_a.ctx, customer_factory, seed)
        rows = (
            db.execute(
                select(LedgerEntry).where(LedgerEntry.customer_id == customer.id)
            )
            .scalars()
            .all()
        )
        service_side = sum(
            e.amount_minor
            for e in rows
            if e.source_type == SourceType.DAILY_SERVICE_RECORD
        )
        payment_side = sum(
            e.amount_minor for e in rows if e.source_type == SourceType.PAYMENT
        )

        # Business generated is exactly the service-origin half of the ledger,
        # and collected is exactly the payment-origin half, negated.
        assert business_generated_minor(db, tenant_a.ctx, customer_id=customer.id) == (
            service_side
        )
        assert collected_minor(db, tenant_a.ctx, customer_id=customer.id) == -payment_side
        assert outstanding_total_minor(db, tenant_a.ctx, customer_id=customer.id) == (
            service_side + payment_side
        )
