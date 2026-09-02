"""Settlement and the platform commercial position — COM-6, COM-11, P0 §11.1 C.

    earned + adjustments − settled = outstanding

A-COM-6 is the case the removed ``settlement_id`` column could not represent, so
it is asserted field by field: every earning row must be byte-for-byte what it was
at creation after each settlement.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.commission.models import CommissionBasis
from app.commission.reporting import commission_position
from app.core.errors import ValidationFailed
from tests._commission import (
    EARLY,
    adjustments,
    events,
    make_plan,
    make_settlement,
    outstanding,
    platform_ctx,
    settlements,
    snapshot_rows,
)
from tests._ops import do_correct, do_pay, do_record, do_void_payment

pytestmark = pytest.mark.postgres

PRICE = 25000


@pytest.fixture
def pctx(tenant_a, platform_user, clock):
    return platform_ctx(tenant_a, platform_user, clock)


def _earn_1000(db, tenant_a, customer_factory, pctx):
    """Earn exactly 1000 minor units of commission, across two events.

    1000 bp of 100000 is 10000, so 1000 bp of 10000 is 1000: two records of
    5000 each earn 500 apiece.
    """
    make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=1000)
    for code in ("E1", "E2"):
        customer = customer_factory(tenant_a.ctx, code=code, price_minor=5000)
        do_record(db, tenant_a.ctx, customer, quantity="1")  # charge 5000 -> 500
    assert commission_position(db, pctx).earned_minor == 1000


class TestACOM6PartialSettlement:
    """A-COM-6: earn 1000, settle 400 → 600, settle 600 → 0."""

    def test_A_COM_6_the_exact_regression(
        self, db, tenant_a, customer_factory, pctx
    ):
        _earn_1000(db, tenant_a, customer_factory, pctx)
        untouched = snapshot_rows(events(db, pctx))
        assert outstanding(db, pctx) == 1000

        make_settlement(db, pctx, 400)
        db.expire_all()
        assert outstanding(db, pctx) == 600
        assert snapshot_rows(events(db, pctx)) == untouched
        assert adjustments(db, pctx) == []

        make_settlement(db, pctx, 600)
        db.expire_all()
        assert outstanding(db, pctx) == 0
        assert snapshot_rows(events(db, pctx)) == untouched
        assert adjustments(db, pctx) == []

        assert len(settlements(db, pctx)) == 2

    def test_the_identity_holds_at_every_step(
        self, db, tenant_a, customer_factory, pctx
    ):
        _earn_1000(db, tenant_a, customer_factory, pctx)
        for amount in (100, 250, 650):
            make_settlement(db, pctx, amount)
            db.expire_all()
            p = commission_position(db, pctx)
            assert (
                p.earned_minor + p.adjustments_minor - p.settled_minor
                == p.outstanding_minor
            )
        assert outstanding(db, pctx) == 0

    def test_a_settlement_stamps_nothing_on_an_earning_row(
        self, db, tenant_a, customer_factory, pctx
    ):
        """COM-6: it references no event and annotates none."""
        _earn_1000(db, tenant_a, customer_factory, pctx)
        before = snapshot_rows(events(db, pctx))
        make_settlement(db, pctx, 400)
        db.expire_all()
        assert snapshot_rows(events(db, pctx)) == before

    def test_settling_before_earning_anything_is_representable(self, db, pctx):
        make_settlement(db, pctx, 500)
        assert outstanding(db, pctx) == -500


class TestACOM6bOverSettlement:
    """A-COM-6b: additive at the edges, with no blocking logic invented."""

    def test_A_COM_6b_settling_1200_against_1000_yields_minus_200(
        self, db, tenant_a, customer_factory, pctx
    ):
        _earn_1000(db, tenant_a, customer_factory, pctx)
        make_settlement(db, pctx, 1200)
        db.expire_all()
        assert outstanding(db, pctx) == -200

    def test_A_COM_6b_a_later_adjustment_still_applies_on_top(
        self, db, tenant_a, customer_factory, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=1000)
        customer = customer_factory(tenant_a.ctx, code="OS", price_minor=5000)
        rec = do_record(db, tenant_a.ctx, customer, quantity="2").result  # 10000 -> 1000
        assert outstanding(db, pctx) == 1000

        make_settlement(db, pctx, 1200)
        db.expire_all()
        assert outstanding(db, pctx) == -200

        do_correct(db, tenant_a.ctx, rec["id"], quantity="1", reason="miscount")
        db.expire_all()
        # -5000 of base at 1000 bp is -500.
        assert commission_position(db, pctx).adjustments_minor == -500
        assert outstanding(db, pctx) == -700

    def test_the_over_settled_position_is_reached_by_a_positive_row(
        self, db, tenant_a, customer_factory, pctx
    ):
        """The sign of the row and the sign of the aggregate are separate questions:
        a negative *outstanding* is legal, a negative *settlement* is not."""
        _earn_1000(db, tenant_a, customer_factory, pctx)
        make_settlement(db, pctx, 1200)
        db.expire_all()
        assert [s.amount_minor for s in settlements(db, pctx)] == [1200]
        assert outstanding(db, pctx) == -200


class TestSettlementIsStrictlyPositive:
    """A settlement is money that moved from the tenant to the platform.

    A negative row would be a commission adjustment in disguise — moving
    outstanding with no snapshotted terms, no link to an earning event and no
    source fact. Commission moves through an adjustment or it does not move.
    """

    def test_a_zero_settlement_is_refused(self, db, pctx):
        with pytest.raises(ValidationFailed, match="greater than zero"):
            make_settlement(db, pctx, 0)

    def test_a_negative_settlement_is_refused(self, db, pctx):
        with pytest.raises(ValidationFailed, match="greater than zero"):
            make_settlement(db, pctx, -500)

    def test_a_refused_settlement_leaves_the_position_untouched(
        self, db, tenant_a, customer_factory, pctx
    ):
        _earn_1000(db, tenant_a, customer_factory, pctx)
        with pytest.raises(ValidationFailed):
            make_settlement(db, pctx, -500)
        db.rollback()
        assert settlements(db, pctx) == []

    def test_a_negative_settlement_cannot_be_used_as_a_commission_adjustment(
        self, db, tenant_a, customer_factory, pctx
    ):
        """The specific abuse the positivity rule exists to prevent: raising
        outstanding without an adjustment row explaining it."""
        _earn_1000(db, tenant_a, customer_factory, pctx)
        make_settlement(db, pctx, 400)
        db.expire_all()
        assert outstanding(db, pctx) == 600

        with pytest.raises(ValidationFailed):
            make_settlement(db, pctx, -600)
        db.rollback()
        assert outstanding(db, pctx) == 600
        assert adjustments(db, pctx) == []


class TestSettlementSignInTheDatabase:
    """The CHECK is the guarantee; the domain validation is the message."""

    def _insert(self, db, tenant_a, amount_minor):
        db.execute(
            text(
                """
                INSERT INTO commission_settlement
                  (id, tenant_id, period_start, period_end, amount_minor,
                   settled_on, created_by_user_id, created_at)
                VALUES (gen_random_uuid(), :t, DATE '2026-01-01',
                        DATE '2026-01-31', :a, DATE '2026-02-01', :u, now())
                """
            ),
            {
                "t": str(tenant_a.ctx.tenant_id),
                "u": str(tenant_a.owner.id),
                "a": amount_minor,
            },
        )

    def test_the_database_refuses_a_zero_settlement(self, db, tenant_a):
        with pytest.raises(Exception) as exc:
            self._insert(db, tenant_a, 0)
        assert "amount_positive" in str(exc.value)
        db.rollback()

    def test_the_database_refuses_a_negative_settlement(self, db, tenant_a):
        with pytest.raises(Exception) as exc:
            self._insert(db, tenant_a, -500)
        assert "amount_positive" in str(exc.value)
        db.rollback()

    def test_the_database_accepts_a_positive_settlement(self, db, tenant_a):
        self._insert(db, tenant_a, 400)
        db.flush()
        count = db.execute(
            text("SELECT count(*) FROM commission_settlement WHERE tenant_id = :t"),
            {"t": str(tenant_a.ctx.tenant_id)},
        ).scalar_one()
        assert count == 1
        db.rollback()

    def test_the_database_accepts_an_over_settlement(
        self, db, tenant_a, customer_factory, pctx
    ):
        """1200 against 1000 earned is a legal positive row (A-COM-6b)."""
        _earn_1000(db, tenant_a, customer_factory, pctx)
        self._insert(db, tenant_a, 1200)
        db.flush()
        db.expire_all()
        assert outstanding(db, pctx) == -200
        db.rollback()


class TestSettlementValidation:
    def test_a_reversed_period_is_refused(self, db, pctx):
        with pytest.raises(ValidationFailed, match="period_end"):
            make_settlement(
                db,
                pctx,
                1000,
                period_start=date(2026, 3, 1),
                period_end=date(2026, 2, 1),
            )

    def test_a_future_settlement_date_is_refused(self, db, pctx):
        with pytest.raises(ValidationFailed, match="settled_on"):
            make_settlement(db, pctx, 1000, settled_on=pctx.today + timedelta(days=1))

    def test_settled_on_defaults_to_the_tenant_business_date(self, db, pctx):
        make_settlement(db, pctx, 1000)
        assert settlements(db, pctx)[0].settled_on == pctx.today

    def test_a_float_amount_is_refused(self, db, pctx):
        with pytest.raises(ValidationFailed, match="amount_minor"):
            make_settlement(db, pctx, 100.5)

    def test_a_bool_is_not_an_amount(self, db, pctx):
        with pytest.raises(ValidationFailed, match="amount_minor"):
            make_settlement(db, pctx, True)

    def test_settlement_is_audited(self, db, pctx):
        from sqlalchemy import select

        from app.audit.models import AuditEvent

        make_settlement(db, pctx, 1000)
        row = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "commission_settlement.recorded"
            )
        ).scalar_one()
        assert row.actor_scope == "PLATFORM"
        assert row.source == "PLATFORM"
        assert row.after["amount_minor"] == 1000


class TestCOM11NoAllocation:
    """COM-11: V1 settles in aggregate. Nothing links a settlement to an event."""

    def test_no_settlement_column_exists_on_either_earning_table(self, engine):
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT table_name, column_name FROM information_schema.columns
                    WHERE table_schema='public'
                      AND table_name IN ('commission_event','commission_adjustment')
                      AND column_name LIKE '%settle%'
                    """
                )
            ).fetchall()
        assert rows == []

    def test_no_settlement_allocation_table_exists(self, engine):
        with engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                    "AND tablename LIKE '%allocation%'"
                )
            ).scalar()
        assert count == 0

    def test_a_settlement_has_no_foreign_key_to_an_earning_row(self, engine):
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT conname, confrelid::regclass::text
                    FROM pg_constraint
                    WHERE conrelid = 'commission_settlement'::regclass
                      AND contype = 'f'
                    """
                )
            ).fetchall()
        targets = {r[1] for r in rows}
        assert "commission_event" not in targets
        assert "commission_adjustment" not in targets


class TestCommissionHistoryIsImmutable:
    """COM-3 / COM-6 / AUD-1, enforced by the database, not by the absence of a route."""

    @pytest.mark.parametrize(
        "table", ["commission_event", "commission_adjustment", "commission_settlement"]
    )
    def test_update_is_rejected(self, db, tenant_a, customer_factory, pctx, table):
        _earn_1000(db, tenant_a, customer_factory, pctx)
        customer = customer_factory(tenant_a.ctx, code="IM", price_minor=5000)
        rec = do_record(db, tenant_a.ctx, customer, quantity="1").result
        do_correct(db, tenant_a.ctx, rec["id"], quantity="1", reason="no change")
        make_settlement(db, pctx, 100)

        with pytest.raises(Exception) as exc:
            db.execute(text(f"UPDATE {table} SET tenant_id = tenant_id"))
        assert "immutable" in str(exc.value).lower()
        db.rollback()

    @pytest.mark.parametrize(
        "table", ["commission_event", "commission_adjustment", "commission_settlement"]
    )
    def test_delete_is_rejected(self, db, tenant_a, customer_factory, pctx, table):
        _earn_1000(db, tenant_a, customer_factory, pctx)
        customer = customer_factory(tenant_a.ctx, code="IM2", price_minor=5000)
        rec = do_record(db, tenant_a.ctx, customer, quantity="1").result
        do_correct(db, tenant_a.ctx, rec["id"], quantity="1", reason="no change")
        make_settlement(db, pctx, 100)

        with pytest.raises(Exception) as exc:
            db.execute(text(f"DELETE FROM {table}"))
        assert "immutable" in str(exc.value).lower()
        db.rollback()


class TestPositionIsTenantScoped:
    def test_one_tenants_settlement_never_moves_anothers_position(
        self, db, tenant_a, tenant_b, customer_factory, platform_user, clock
    ):
        a = platform_ctx(tenant_a, platform_user, clock)
        b = platform_ctx(tenant_b, platform_user, clock)
        _earn_1000(db, tenant_a, customer_factory, a)
        make_settlement(db, b, 400)
        db.expire_all()

        assert outstanding(db, a) == 1000
        assert outstanding(db, b) == -400

    def test_an_empty_tenant_reports_four_zeros(self, db, tenant_b, platform_user, clock):
        b = platform_ctx(tenant_b, platform_user, clock)
        position = commission_position(db, b)
        assert (
            position.earned_minor,
            position.adjustments_minor,
            position.settled_minor,
            position.outstanding_minor,
        ) == (0, 0, 0, 0)


class TestCollectedValuePositionEndToEnd:
    """The prompt's case 3, read through the position rather than the rows."""

    def test_a_payment_void_returns_the_position_to_zero(
        self, db, tenant_a, customer_factory, pctx
    ):
        make_plan(db, pctx, basis=CommissionBasis.COLLECTED_VALUE, rate_bp=800)
        customer = customer_factory(tenant_a.ctx, code="CV", price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity="4")
        payment = do_pay(db, tenant_a.ctx, customer, 50000).result
        assert outstanding(db, pctx) == 4000

        do_void_payment(db, tenant_a.ctx, payment["id"], reason="bounced")
        db.expire_all()
        position = commission_position(db, pctx)
        assert position.earned_minor == 4000
        assert position.adjustments_minor == -4000
        assert position.outstanding_minor == 0
