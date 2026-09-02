"""The commission engine — COM-2..COM-5, COM-9, COM-10 and the four bases.

The cases P0 names explicitly are here under their acceptance ids: A-COM-2 (only
central acceptance earns), A-COM-3 (a plan change never rewrites history),
A-COM-4 (an adjustment uses the original terms) and A-COM-10 (a basis switch only
changes future triggers).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.billing.models import SourceType
from app.billing.reporting import business_generated_minor, collected_minor
from app.commission.engine import commission_minor_for
from app.commission.models import CommissionBasis, CommissionSourceType
from app.core.money import apply_rate_bp
from tests._commission import (
    EARLY,
    adjustments,
    events,
    make_plan,
    platform_ctx,
)
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

PRICE = 25000  # Rs. 250.00 per unit


@pytest.fixture
def pctx(tenant_a, platform_user, clock):
    return platform_ctx(tenant_a, platform_user, clock)


@pytest.fixture
def customer(tenant_a, customer_factory):
    return customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)


class TestSourceTypesMatchTheLedger:
    def test_the_service_and_payment_source_types_have_not_drifted(self):
        """A commission row and the ledger row for the same fact agree."""
        assert (
            CommissionSourceType.DAILY_SERVICE_RECORD == SourceType.DAILY_SERVICE_RECORD
        )
        assert CommissionSourceType.PAYMENT == SourceType.PAYMENT


class TestCOM9Arithmetic:
    """COM-9: the same integer half-up rule billing uses."""

    def test_the_frozen_formula(self):
        assert apply_rate_bp(100000, 250) == 2500

    def test_half_up_at_the_boundary(self):
        # 1 * 5000 / 10000 = 0.5 -> 1, not 0.
        assert apply_rate_bp(1, 5000) == 1

    def test_half_up_is_symmetric_for_a_reversal(self):
        assert apply_rate_bp(-1, 5000) == -1

    def test_a_rated_basis_uses_the_rate(self):
        assert (
            commission_minor_for(
                basis=CommissionBasis.RECORDED_VALUE,
                rate_bp=250,
                fixed_amount_minor=None,
                base_amount_minor=100000,
            )
            == 2500
        )

    def test_per_event_ignores_the_base(self):
        for base in (0, 50000, 999999):
            assert (
                commission_minor_for(
                    basis=CommissionBasis.PER_EVENT,
                    rate_bp=None,
                    fixed_amount_minor=700,
                    base_amount_minor=base,
                )
                == 700
            )

    def test_every_result_is_an_int(self):
        for base in (0, 1, 7, 12345, -12345):
            assert isinstance(apply_rate_bp(base, 333), int)


class TestRECORDEDVALUE:
    """Commission follows accepted service value (FIN-14)."""

    def test_an_accepted_service_earns(self, db, tenant_a, customer, pctx):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        do_record(db, tenant_a.ctx, customer, quantity="4")  # 100000 minor

        (event,) = events(db, pctx)
        assert event.base_amount_minor == 100000
        assert event.commission_minor == 2500
        assert event.basis_snapshot == CommissionBasis.RECORDED_VALUE
        assert event.rate_bp_snapshot == 250
        assert event.fixed_amount_minor_snapshot is None
        assert event.source_type == CommissionSourceType.DAILY_SERVICE_RECORD

    def test_the_event_carries_the_service_date_not_the_write_date(
        self, db, tenant_a, customer, pctx
    ):
        yesterday = tenant_a.ctx.today - timedelta(days=1)
        do_record(db, tenant_a.ctx, customer, quantity="1", service_date=yesterday)
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        # The plan was created after the record, so nothing was earned; record a
        # second one to show occurred_on tracks the service date.
        other = customer
        do_record(
            db,
            tenant_a.ctx,
            other,
            quantity="1",
            service_date=tenant_a.ctx.today,
        )
        (event,) = events(db, pctx)
        assert event.occurred_on == tenant_a.ctx.today

    def test_a_SKIP_earns_nothing(self, db, tenant_a, customer, pctx):
        """P0 §11 pays per accepted *service*; a skip is not one."""
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        do_record(db, tenant_a.ctx, customer, kind="SKIP")
        assert events(db, pctx) == []

    def test_a_payment_earns_nothing_under_this_basis(
        self, db, tenant_a, customer, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        do_pay(db, tenant_a.ctx, customer, 50000)
        assert events(db, pctx) == []

    def test_a_correction_adjusts_at_the_original_terms(
        self, db, tenant_a, customer, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        do_correct(db, tenant_a.ctx, rec["id"], quantity="3", reason="miscount")

        (event,) = events(db, pctx)
        (adjustment,) = adjustments(db, pctx)
        assert event.commission_minor == 2500  # unchanged
        # base moved by -25000; 250 bp of that is -625.
        assert adjustment.amount_minor == -625
        assert adjustment.commission_event_id == event.id
        assert adjustment.source_id == event.source_id
        assert adjustment.reason == "miscount"

    def test_a_void_reverses_the_whole_base(self, db, tenant_a, customer, pctx):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        do_void(db, tenant_a.ctx, rec["id"], reason="not delivered")

        (event,) = events(db, pctx)
        (adjustment,) = adjustments(db, pctx)
        assert event.commission_minor + adjustment.amount_minor == 0

    def test_a_chain_of_corrections_earns_once_and_adjusts_each_time(
        self, db, tenant_a, customer, pctx
    ):
        """The chain mirrors the ledger: one charge, then differences."""
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=1000)
        first = do_record(db, tenant_a.ctx, customer, quantity="4").result  # 100000
        second = do_correct(
            db, tenant_a.ctx, first["id"], quantity="3", reason="a"
        ).result  # 75000
        do_correct(db, tenant_a.ctx, second["id"], quantity="2", reason="b")  # 50000

        assert len(events(db, pctx)) == 1
        rows = adjustments(db, pctx)
        assert len(rows) == 2
        assert [r.amount_minor for r in rows] == [-2500, -2500]
        total = events(db, pctx)[0].commission_minor + sum(r.amount_minor for r in rows)
        assert total == apply_rate_bp(50000, 1000)

    def test_correcting_a_SKIP_into_a_SERVICE_earns_for_the_first_time(
        self, db, tenant_a, customer, pctx
    ):
        """The skip earned nothing, so the correction is newly accepted value —
        an earning event, not an adjustment against one that never existed."""
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        rec = do_record(db, tenant_a.ctx, customer, kind="SKIP").result
        assert events(db, pctx) == []

        do_correct(db, tenant_a.ctx, rec["id"], quantity="4", reason="was delivered")
        (event,) = events(db, pctx)
        assert event.commission_minor == 2500
        assert adjustments(db, pctx) == []

    def test_correcting_a_SERVICE_into_a_SKIP_reverses_it(
        self, db, tenant_a, customer, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        do_correct(
            db, tenant_a.ctx, rec["id"], quantity=None, kind="SKIP", reason="never came"
        )

        (event,) = events(db, pctx)
        (adjustment,) = adjustments(db, pctx)
        assert event.commission_minor + adjustment.amount_minor == 0


class TestPEREVENT:
    """A fixed amount per accepted SERVICE record."""

    def test_each_accepted_service_earns_the_fixed_amount(
        self, db, tenant_a, customer_factory, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.PER_EVENT, fixed_amount_minor=700)
        for code in ("P1", "P2", "P3"):
            c = customer_factory(tenant_a.ctx, code=code, price_minor=PRICE)
            do_record(db, tenant_a.ctx, c, quantity="4")

        rows = events(db, pctx)
        assert len(rows) == 3
        assert {r.commission_minor for r in rows} == {700}
        assert {r.rate_bp_snapshot for r in rows} == {None}
        assert {r.fixed_amount_minor_snapshot for r in rows} == {700}

    def test_a_SKIP_does_not_earn_a_per_event_fee(self, db, tenant_a, customer, pctx):
        """The specific accident this basis invites: a skip billing the tenant."""
        make_plan(db, pctx, basis=CommissionBasis.PER_EVENT, fixed_amount_minor=700)
        do_record(db, tenant_a.ctx, customer, kind="SKIP")
        assert events(db, pctx) == []

    def test_the_fee_is_independent_of_quantity(
        self, db, tenant_a, customer_factory, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.PER_EVENT, fixed_amount_minor=700)
        small = customer_factory(tenant_a.ctx, code="S", price_minor=PRICE)
        large = customer_factory(tenant_a.ctx, code="L", price_minor=PRICE)
        do_record(db, tenant_a.ctx, small, quantity="1")
        do_record(db, tenant_a.ctx, large, quantity="40")
        assert {r.commission_minor for r in events(db, pctx)} == {700}

    def test_a_quantity_correction_moves_the_fee_by_nothing(
        self, db, tenant_a, customer, pctx
    ):
        """One accepted event before, one after — the fee never depended on size."""
        make_plan(db, pctx, basis=CommissionBasis.PER_EVENT, fixed_amount_minor=700)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        do_correct(db, tenant_a.ctx, rec["id"], quantity="1", reason="miscount")

        (adjustment,) = adjustments(db, pctx)
        assert adjustment.amount_minor == 0
        assert events(db, pctx)[0].commission_minor == 700

    def test_a_void_removes_the_whole_fee(self, db, tenant_a, customer, pctx):
        make_plan(db, pctx, basis=CommissionBasis.PER_EVENT, fixed_amount_minor=700)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        do_void(db, tenant_a.ctx, rec["id"], reason="not delivered")
        (adjustment,) = adjustments(db, pctx)
        assert adjustment.amount_minor == -700

    def test_correcting_to_a_SKIP_removes_the_whole_fee(
        self, db, tenant_a, customer, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.PER_EVENT, fixed_amount_minor=700)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        do_correct(
            db, tenant_a.ctx, rec["id"], quantity=None, kind="SKIP", reason="never came"
        )
        (adjustment,) = adjustments(db, pctx)
        assert adjustment.amount_minor == -700


class TestCOLLECTEDVALUE:
    """Commission follows collections (FIN-16), including reversal."""

    def test_an_accepted_payment_earns(self, db, tenant_a, customer, pctx):
        make_plan(db, pctx, basis=CommissionBasis.COLLECTED_VALUE, rate_bp=800)
        do_record(db, tenant_a.ctx, customer, quantity="4")
        do_pay(db, tenant_a.ctx, customer, 50000)

        (event,) = events(db, pctx)
        assert event.source_type == CommissionSourceType.PAYMENT
        assert event.base_amount_minor == 50000
        assert event.commission_minor == 4000

    def test_a_service_earns_nothing_under_this_basis(
        self, db, tenant_a, customer, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.COLLECTED_VALUE, rate_bp=800)
        do_record(db, tenant_a.ctx, customer, quantity="4")
        assert events(db, pctx) == []

    def test_a_service_correction_produces_no_commission_adjustment(
        self, db, tenant_a, customer, pctx
    ):
        """Nothing was earned on the service, so nothing can be reversed on it."""
        make_plan(db, pctx, basis=CommissionBasis.COLLECTED_VALUE, rate_bp=800)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        do_correct(db, tenant_a.ctx, rec["id"], quantity="1", reason="miscount")
        assert adjustments(db, pctx) == []

    def test_voiding_a_payment_reverses_the_collection_commission(
        self, db, tenant_a, customer, pctx
    ):
        """The P0 §11 worked case: a void moves COLLECTED_VALUE and nothing else."""
        make_plan(db, pctx, basis=CommissionBasis.COLLECTED_VALUE, rate_bp=800)
        do_record(db, tenant_a.ctx, customer, quantity="4")  # 100000 charged
        payment = do_pay(db, tenant_a.ctx, customer, 50000).result
        do_void_payment(db, tenant_a.ctx, payment["id"], reason="cheque bounced")

        (event,) = events(db, pctx)
        (adjustment,) = adjustments(db, pctx)
        assert event.commission_minor == 4000
        assert adjustment.amount_minor == -4000
        assert adjustment.commission_event_id == event.id
        assert adjustment.source_type == CommissionSourceType.PAYMENT

        # FIN-14 vs FIN-16, unchanged by P3: business generated stays 100000
        # while collected falls back to 0.
        assert business_generated_minor(db, tenant_a.ctx) == 100000
        assert collected_minor(db, tenant_a.ctx) == 0

    def test_business_generated_commission_is_unaffected_by_a_payment_void(
        self, db, tenant_a, customer, pctx
    ):
        """The same void under RECORDED_VALUE moves no commission at all."""
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        do_record(db, tenant_a.ctx, customer, quantity="4")
        payment = do_pay(db, tenant_a.ctx, customer, 50000).result
        do_void_payment(db, tenant_a.ctx, payment["id"], reason="cheque bounced")

        (event,) = events(db, pctx)
        assert event.commission_minor == 2500
        assert adjustments(db, pctx) == []


class TestBILLEDVALUE:
    """Commission is earned when a statement is issued (FIN-15)."""

    def test_nothing_is_earned_until_the_cycle_closes(
        self, db, tenant_a, customer, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.BILLED_VALUE, rate_bp=250)
        do_record(db, tenant_a.ctx, customer, quantity="4")
        assert events(db, pctx) == []

    def test_issuing_a_statement_earns_on_charges_plus_service_adjustments(
        self, db, tenant_a, customer, pctx, clock
    ):
        from app.billing.cycles import open_cycle

        make_plan(db, pctx, basis=CommissionBasis.BILLED_VALUE, rate_bp=250)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result  # 100000
        do_correct(db, tenant_a.ctx, rec["id"], quantity="3", reason="miscount")  # -25000

        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)

        (event,) = events(db, pctx)
        assert event.source_type == CommissionSourceType.STATEMENT
        assert event.base_amount_minor == 75000  # 100000 + (-25000)
        assert event.commission_minor == apply_rate_bp(75000, 250)
        assert event.occurred_on == cycle.period_end

    def test_a_payment_reversal_never_inflates_the_billed_base(
        self, db, tenant_a, customer, pctx
    ):
        """FIN-15 excludes payment_reversals_minor; so does the commission base."""
        from app.billing.cycles import open_cycle

        make_plan(db, pctx, basis=CommissionBasis.BILLED_VALUE, rate_bp=250)
        do_record(db, tenant_a.ctx, customer, quantity="4")  # 100000
        payment = do_pay(db, tenant_a.ctx, customer, 50000).result
        do_void_payment(db, tenant_a.ctx, payment["id"], reason="bounced")

        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)

        (event,) = events(db, pctx)
        assert event.base_amount_minor == 100000

    def test_an_issued_statement_never_needs_an_adjustment(
        self, db, tenant_a, customer, pctx
    ):
        """A statement is immutable, so the earning event can never be corrected —
        a later correction is billed on a later statement and earns its own."""
        from app.billing.cycles import open_cycle

        make_plan(db, pctx, basis=CommissionBasis.BILLED_VALUE, rate_bp=250)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        cycle = open_cycle(db, tenant_a.ctx)
        ctx_after, _ = close_after_period_end(db, tenant_a, cycle)

        do_correct(db, ctx_after, rec["id"], quantity="3", reason="late correction")
        assert adjustments(db, pctx) == []
        assert len(events(db, pctx)) == 1


class TestCOM3SnapshottingSurvivesPlanChanges:
    """A-COM-3 / A-COM-4: history is written once and never re-derived."""

    def test_A_COM_3_an_event_at_250bp_survives_a_move_to_500bp(
        self, db, tenant_a, customer, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        do_record(db, tenant_a.ctx, customer, quantity="4")
        before = events(db, pctx)[0]
        original = {
            c.name: getattr(before, c.name) for c in before.__table__.columns
        }

        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=500,
            effective_from=tenant_a.ctx.today,
        )
        db.expire_all()

        after = events(db, pctx)[0]
        assert {c.name: getattr(after, c.name) for c in after.__table__.columns} == original
        assert after.rate_bp_snapshot == 250
        assert after.commission_minor == 2500

    def test_A_COM_4_a_correction_after_a_rate_change_uses_the_old_rate(
        self, db, tenant_a, customer_factory, pctx
    ):
        """3 units at 250 bp, corrected to 2 units after the plan moved to 500 bp."""
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        customer = customer_factory(tenant_a.ctx, code="AC4", price_minor=PRICE)
        rec = do_record(db, tenant_a.ctx, customer, quantity="3").result  # 75000

        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=500,
            effective_from=tenant_a.ctx.today,
        )
        do_correct(db, tenant_a.ctx, rec["id"], quantity="2", reason="miscount")

        (event,) = events(db, pctx)
        (adjustment,) = adjustments(db, pctx)
        # -25000 at the ORIGINAL 250 bp is -625; at today's 500 bp it would be -1250.
        assert adjustment.amount_minor == -625
        assert adjustment.amount_minor != apply_rate_bp(-25000, 500)
        # Traceable to both the source and the original event (COM-4).
        assert adjustment.commission_event_id == event.id
        assert adjustment.source_id == event.source_id
        assert event.rate_bp_snapshot == 250

    def test_a_new_record_after_the_change_uses_the_new_rate(
        self, db, tenant_a, customer_factory, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        old = customer_factory(tenant_a.ctx, code="OLD", price_minor=PRICE)
        do_record(db, tenant_a.ctx, old, quantity="4")

        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=500,
            effective_from=tenant_a.ctx.today,
        )
        new = customer_factory(tenant_a.ctx, code="NEW", price_minor=PRICE)
        do_record(db, tenant_a.ctx, new, quantity="4")

        rates = sorted(e.rate_bp_snapshot for e in events(db, pctx))
        assert rates == [250, 500]
        assert sorted(e.commission_minor for e in events(db, pctx)) == [2500, 5000]


class TestCOM10BasisSwitch:
    """A-COM-10: switching the basis changes only which future triggers fire."""

    def test_A_COM_10_prior_events_are_untouched_and_only_new_triggers_differ(
        self, db, tenant_a, customer_factory, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        first = customer_factory(tenant_a.ctx, code="B1", price_minor=PRICE)
        do_record(db, tenant_a.ctx, first, quantity="4")
        do_pay(db, tenant_a.ctx, first, 50000)  # earns nothing under RECORDED_VALUE

        before = [
            {c.name: getattr(e, c.name) for c in e.__table__.columns}
            for e in events(db, pctx)
        ]
        assert len(before) == 1

        make_plan(
            db,
            pctx,
            basis=CommissionBasis.COLLECTED_VALUE,
            rate_bp=800,
            effective_from=tenant_a.ctx.today,
        )

        second = customer_factory(tenant_a.ctx, code="B2", price_minor=PRICE)
        do_record(db, tenant_a.ctx, second, quantity="4")  # now earns nothing
        do_pay(db, tenant_a.ctx, second, 50000)  # now earns

        db.expire_all()
        rows = events(db, pctx)
        after = [
            {c.name: getattr(e, c.name) for c in e.__table__.columns} for e in rows
        ]
        assert after[0] == before[0], "an existing event was altered by a basis switch"
        assert len(rows) == 2
        assert rows[1].basis_snapshot == CommissionBasis.COLLECTED_VALUE
        assert rows[1].source_type == CommissionSourceType.PAYMENT
        assert rows[1].commission_minor == 4000


class TestCOM2CentralAcceptance:
    """COM-2 / A-COM-2: only a committed central acceptance creates commission."""

    def test_A_COM_2_no_event_exists_until_the_accepting_transaction_commits(
        self, db, tenant_a, customer, pctx, session_factory
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)

        from app.service.commands import RecordServiceInput, record_service
        from app.core.ids import uuid7

        # Perform the acceptance *without* the idempotency wrapper's commit, then
        # read from a second connection: nothing is visible yet.
        record_service(
            db,
            tenant_a.ctx,
            RecordServiceInput(customer_id=customer.id, quantity=Decimal("4")),
            operation_id=uuid7(),
        )
        other = session_factory()
        try:
            visible = other.execute(
                text(
                    "SELECT count(*) FROM commission_event WHERE tenant_id = :t"
                ),
                {"t": str(tenant_a.ctx.tenant_id)},
            ).scalar_one()
        finally:
            other.close()
        assert visible == 0, "commission was visible before the source committed"

        db.commit()
        assert len(events(db, pctx)) == 1

    def test_a_synced_record_earns_exactly_like_an_online_one(
        self, db, tenant_a, customer, pctx
    ):
        """The device did not create it; the server did, on acceptance."""
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        do_record(db, tenant_a.ctx, customer, quantity="4", source="SYNC")
        (event,) = events(db, pctx)
        assert event.commission_minor == 2500

    def test_the_engine_is_not_reachable_from_any_route(self, app):
        """There is no commission-event or automatic-adjustment endpoint."""
        paths = [getattr(r, "path", "") or "" for r in app.routes]
        for fragment in ("commission/events", "commission/adjustments"):
            assert not any(fragment in p for p in paths)


class TestCOM5OneSourceFactEarnsOnce:
    """COM-5: uniqueness on both earning tables, in the database."""

    def test_a_replayed_operation_creates_no_second_event(
        self, db, tenant_a, customer, pctx
    ):
        from app.core.ids import uuid7

        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        op = uuid7()
        first = do_record(db, tenant_a.ctx, customer, quantity="4", operation_id=op)
        second = do_record(db, tenant_a.ctx, customer, quantity="4", operation_id=op)

        assert first.status == "APPLIED"
        assert second.status == "DUPLICATE"
        assert len(events(db, pctx)) == 1

    def test_a_replayed_void_creates_no_second_adjustment(
        self, db, tenant_a, customer, pctx
    ):
        from app.core.ids import uuid7

        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        op = uuid7()
        do_void(db, tenant_a.ctx, rec["id"], reason="gone", operation_id=op)
        second = do_void(db, tenant_a.ctx, rec["id"], reason="gone", operation_id=op)

        assert second.status == "DUPLICATE"
        assert len(adjustments(db, pctx)) == 1

    def test_the_database_refuses_a_second_event_for_one_source(
        self, db, tenant_a, customer, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        do_record(db, tenant_a.ctx, customer, quantity="4")
        (event,) = events(db, pctx)

        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO commission_event
                      (id, tenant_id, plan_id, basis_snapshot, rate_bp_snapshot,
                       source_type, source_id, base_amount_minor, commission_minor,
                       occurred_on, created_at)
                    VALUES (gen_random_uuid(), :t, :p, 'RECORDED_VALUE', 250,
                            :st, :sid, 1, 1, CURRENT_DATE, now())
                    """
                ),
                {
                    "t": str(event.tenant_id),
                    "p": str(event.plan_id),
                    "st": event.source_type,
                    "sid": str(event.source_id),
                },
            )
        assert "uq_commission_event_tenant_id_source_type_source_id" in str(exc.value)
        db.rollback()

    def test_the_database_refuses_a_second_adjustment_for_one_source(
        self, db, tenant_a, customer, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        rec = do_record(db, tenant_a.ctx, customer, quantity="4").result
        do_void(db, tenant_a.ctx, rec["id"], reason="gone")
        (adjustment,) = adjustments(db, pctx)

        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO commission_adjustment
                      (id, tenant_id, commission_event_id, amount_minor, reason,
                       source_type, source_id, created_at)
                    VALUES (gen_random_uuid(), :t, :e, -1, 'again', :st, :sid, now())
                    """
                ),
                {
                    "t": str(adjustment.tenant_id),
                    "e": str(adjustment.commission_event_id),
                    "st": adjustment.source_type,
                    "sid": str(adjustment.source_id),
                },
            )
        assert "uq_commission_adjustment_tenant_id_source_type_source_id" in str(exc.value)
        db.rollback()


class TestCommissionIsTenantScoped:
    def test_two_tenants_earn_independently(
        self, db, tenant_a, tenant_b, customer_factory, platform_user, clock
    ):
        a = platform_ctx(tenant_a, platform_user, clock)
        b = platform_ctx(tenant_b, platform_user, clock)
        make_plan(db, a, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)

        ca = customer_factory(tenant_a.ctx, code="TA", price_minor=PRICE)
        cb = customer_factory(tenant_b.ctx, code="TB", price_minor=PRICE)
        do_record(db, tenant_a.ctx, ca, quantity="4")
        do_record(db, tenant_b.ctx, cb, quantity="4")

        assert len(events(db, a)) == 1
        assert events(db, b) == [], "tenant B has no plan and must earn nothing"

    def test_SEC2_an_event_cannot_cite_another_tenants_plan(
        self, db, tenant_a, tenant_b, platform_user, clock
    ):
        a = platform_ctx(tenant_a, platform_user, clock)
        make_plan(db, a, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        from tests._commission import plans as list_rows

        plan = list_rows(db, a)[0]

        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO commission_event
                      (id, tenant_id, plan_id, basis_snapshot, rate_bp_snapshot,
                       source_type, source_id, base_amount_minor, commission_minor,
                       occurred_on, created_at)
                    VALUES (gen_random_uuid(), :tb, :plan_a, 'RECORDED_VALUE', 250,
                            'daily_service_record', gen_random_uuid(), 1, 1,
                            CURRENT_DATE, now())
                    """
                ),
                {"tb": str(tenant_b.ctx.tenant_id), "plan_a": str(plan.id)},
            )
        assert "fk_commission_event_tenant_id_plan_id" in str(exc.value)
        db.rollback()


class TestBasisCorrectionMatrix:
    """The review's basis-by-basis correction audit, with the exact figures.

    Each case asserts both what *does* happen and what must *not*: the recurring
    failure mode for a commission engine is a second economic effect for one
    business fact.
    """

    def test_RECORDED_VALUE_1000_corrected_to_700(
        self, db, tenant_a, customer_factory, pctx
    ):
        """Adjustment on −300 at the original terms; no event for the replacement."""
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=1000)
        customer = customer_factory(tenant_a.ctx, code="R1", price_minor=100)
        rec = do_record(db, tenant_a.ctx, customer, quantity="10").result  # 1000

        (event,) = events(db, pctx)
        assert (event.base_amount_minor, event.commission_minor) == (1000, 100)

        replacement = do_correct(
            db, tenant_a.ctx, rec["id"], quantity="7", reason="miscount"
        ).result
        assert replacement["charge_minor"] == 700

        # Exactly one earning event, still: the replacement earns nothing of its own.
        rows = events(db, pctx)
        assert len(rows) == 1
        assert rows[0].id == event.id
        assert str(rows[0].source_id) != replacement["id"]

        (adjustment,) = adjustments(db, pctx)
        assert adjustment.amount_minor == apply_rate_bp(-300, 1000) == -30
        assert adjustment.commission_event_id == event.id
        # Net commission is the rate on the corrected value, to the minor unit.
        assert event.commission_minor + adjustment.amount_minor == apply_rate_bp(700, 1000)

    def test_PER_EVENT_correction_then_void_reverses_exactly_once(
        self, db, tenant_a, customer_factory, pctx
    ):
        """One fixed fee, a quantity correction that changes nothing, then a void
        that removes the whole fee — never two fees and never a double reversal."""
        make_plan(db, pctx, basis=CommissionBasis.PER_EVENT, fixed_amount_minor=700)
        customer = customer_factory(tenant_a.ctx, code="P1", price_minor=100)
        rec = do_record(db, tenant_a.ctx, customer, quantity="10").result

        (event,) = events(db, pctx)
        assert event.commission_minor == 700

        replacement = do_correct(
            db, tenant_a.ctx, rec["id"], quantity="7", reason="miscount"
        ).result
        assert len(events(db, pctx)) == 1, "a correction earned a second fixed fee"
        assert [a.amount_minor for a in adjustments(db, pctx)] == [0]

        do_void(db, tenant_a.ctx, replacement["id"], reason="never delivered")
        assert len(events(db, pctx)) == 1
        rows = adjustments(db, pctx)
        assert [a.amount_minor for a in rows] == [0, -700]
        assert {a.commission_event_id for a in rows} == {event.id}
        assert event.commission_minor + sum(a.amount_minor for a in rows) == 0

    def test_COLLECTED_VALUE_payment_500_then_void(
        self, db, tenant_a, customer_factory, pctx
    ):
        """Earns on 500, reverses on 500, and touches no service-side commission."""
        make_plan(db, pctx, basis=CommissionBasis.COLLECTED_VALUE, rate_bp=1000)
        customer = customer_factory(tenant_a.ctx, code="CV1", price_minor=100)
        do_record(db, tenant_a.ctx, customer, quantity="10")  # 1000 of service value
        payment = do_pay(db, tenant_a.ctx, customer, 500).result

        (event,) = events(db, pctx)
        assert event.source_type == CommissionSourceType.PAYMENT
        assert (event.base_amount_minor, event.commission_minor) == (500, 50)

        do_void_payment(db, tenant_a.ctx, payment["id"], reason="bounced")
        (adjustment,) = adjustments(db, pctx)
        assert adjustment.amount_minor == -50
        assert event.commission_minor + adjustment.amount_minor == 0

        # The service side never earned and never moved.
        assert len(events(db, pctx)) == 1
        assert business_generated_minor(db, tenant_a.ctx) == 1000

    def test_BILLED_VALUE_a_late_correction_has_exactly_one_economic_effect(
        self, db, tenant_a, customer_factory, pctx
    ):
        """The double-count the review asked about, driven end to end.

        A correction after its statement was issued must not *both* reverse the
        first statement's commission *and* reduce it again when the adjustment
        appears on the next statement. It does the second only: the first
        statement is immutable and its event stands, and the correction is billed
        — and earns — on the cycle that actually carries it.
        """
        from app.billing.cycles import open_cycle

        make_plan(db, pctx, basis=CommissionBasis.BILLED_VALUE, rate_bp=1000)
        customer = customer_factory(tenant_a.ctx, code="BV1", price_minor=100)
        rec = do_record(db, tenant_a.ctx, customer, quantity="10").result  # 1000

        first_cycle = open_cycle(db, tenant_a.ctx)
        ctx_next, _ = close_after_period_end(db, tenant_a, first_cycle)

        rows = events(db, pctx)
        assert len(rows) == 1
        assert (rows[0].base_amount_minor, rows[0].commission_minor) == (1000, 100)

        # Correct in the following cycle. FIN-9: occurred_on stays, the ledger
        # adjustment posts forward.
        do_correct(db, ctx_next, rec["id"], quantity="7", reason="miscount")
        db.expire_all()

        # No immediate reversal: the issued statement is immutable and so is its event.
        assert adjustments(db, pctx) == [], "a billed-value event was reversed early"
        assert len(events(db, pctx)) == 1

        second_cycle = open_cycle(db, ctx_next)
        assert second_cycle.id != first_cycle.id
        close_after_period_end(db, tenant_a, second_cycle)
        db.expire_all()

        rows = events(db, pctx)
        assert len(rows) == 2
        assert adjustments(db, pctx) == []
        second = [e for e in rows if e.source_id != rows[0].source_id or e.id != rows[0].id][-1]
        assert second.base_amount_minor == -300, "the next statement did not carry it"
        assert second.commission_minor == apply_rate_bp(-300, 1000) == -30

        # Exactly one economic effect: the rate on the net billed value, once.
        total = sum(e.commission_minor for e in rows) + sum(
            a.amount_minor for a in adjustments(db, pctx)
        )
        assert total == apply_rate_bp(700, 1000) == 70

    def test_BILLED_VALUE_a_void_after_issue_is_also_counted_once(
        self, db, tenant_a, customer_factory, pctx
    ):
        from app.billing.cycles import open_cycle

        make_plan(db, pctx, basis=CommissionBasis.BILLED_VALUE, rate_bp=1000)
        customer = customer_factory(tenant_a.ctx, code="BV2", price_minor=100)
        rec = do_record(db, tenant_a.ctx, customer, quantity="10").result

        first_cycle = open_cycle(db, tenant_a.ctx)
        ctx_next, _ = close_after_period_end(db, tenant_a, first_cycle)

        do_void(db, ctx_next, rec["id"], reason="never delivered")
        assert adjustments(db, pctx) == []

        close_after_period_end(db, tenant_a, open_cycle(db, ctx_next))
        db.expire_all()

        rows = events(db, pctx)
        assert len(rows) == 2
        assert adjustments(db, pctx) == []
        # 1000 billed, then -1000 billed: nothing net was ever billed, so nothing
        # net was ever earned.
        assert sum(e.commission_minor for e in rows) == 0
