"""Commission plans — COM-1, COM-8, COM-9, and the non-overlap guarantee.

The plan is the only place a rate, a basis or a currency exists. These tests are
what make "nothing about the commercial deal is hard-coded" checkable rather than
asserted.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app.commission.models import CommissionBasis
from app.commission.plans import effective_plan, list_plans
from app.core.errors import CommissionPlanOverlapError, ValidationFailed
from app.core.money import MoneyError, validate_rate_bp
from tests._commission import EARLY, make_plan, plans, platform_ctx

pytestmark = pytest.mark.postgres


@pytest.fixture
def pctx(tenant_a, platform_user, clock):
    return platform_ctx(tenant_a, platform_user, clock)


class TestCOM1NothingIsHardCoded:
    """COM-1: rate, basis and currency all come from a row."""

    def test_COM1_no_rate_or_basis_literal_lives_in_the_engine(self):
        """The engine reads terms; it never contains a commercial number."""
        import pathlib

        from tests._source import code_only

        code = code_only(pathlib.Path("app/commission/engine.py"))
        # 10000 is the basis-point scale, and it lives in core.money, not here.
        for literal in ("250", "500", "10000", "PKR"):
            assert literal not in code.split(), f"{literal!r} hard-coded in the engine"

    def test_COM1_with_no_plan_nothing_is_earned(
        self, db, tenant_a, customer_factory, pctx
    ):
        """No plan is not a default plan. Absent configuration earns nothing."""
        from tests._commission import events
        from tests._ops import do_record

        customer = customer_factory(tenant_a.ctx, code="NP", price_minor=100000)
        do_record(db, tenant_a.ctx, customer, quantity="1")
        assert events(db, pctx) == []

    def test_COM1_the_currency_comes_from_the_tenant(self, db, pctx):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        assert plans(db, pctx)[0].currency == pctx.currency

    def test_COM1_a_foreign_currency_is_refused(self, db, pctx):
        from app.commission.plans import CreatePlanInput, create_plan
        from app.core.ids import uuid7

        with pytest.raises(ValidationFailed, match="does not match the tenant currency"):
            create_plan(
                db,
                pctx,
                CreatePlanInput(
                    basis=CommissionBasis.RECORDED_VALUE,
                    rate_bp=250,
                    currency="USD",
                    effective_from=EARLY,
                ),
                operation_id=uuid7(),
            )


class TestCOM9RateIsIntegerBasisPoints:
    """COM-9: `rate_bp` is an integer 0..10000, and rounding is the shared rule."""

    @pytest.mark.parametrize("value", [0, 1, 250, 500, 10000])
    def test_COM9_valid_rates_are_accepted(self, value):
        assert validate_rate_bp(value) == value

    @pytest.mark.parametrize("value", [-1, 10001, 100000])
    def test_COM9_out_of_range_rate_is_refused(self, value):
        with pytest.raises(MoneyError):
            validate_rate_bp(value)

    def test_COM9_a_float_rate_is_refused(self):
        with pytest.raises(MoneyError):
            validate_rate_bp(2.5)

    def test_COM9_a_bool_is_not_a_rate(self):
        with pytest.raises(MoneyError):
            validate_rate_bp(True)

    def test_COM9_the_database_refuses_an_out_of_range_rate(self, db, tenant_a):
        """Not merely validated in Python: the CHECK is in the database."""
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO commission_plan
                      (id, tenant_id, basis, rate_bp, currency, effective_from,
                       created_by_user_id, created_at)
                    VALUES (gen_random_uuid(), :t, 'RECORDED_VALUE', 10001, 'PKR',
                            DATE '2026-01-01', :u, now())
                    """
                ),
                {"t": str(tenant_a.ctx.tenant_id), "u": str(tenant_a.owner.id)},
            )
        assert "rate_bp_range" in str(exc.value)
        db.rollback()

    def test_COM9_rate_bp_is_an_integer_column(self, engine):
        with engine.connect() as conn:
            dtype = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name='commission_plan' AND column_name='rate_bp'"
                )
            ).scalar()
        assert dtype == "integer"


class TestExactlyOneTermForTheBasis:
    """P0 §6: exactly one of rate_bp / fixed_amount_minor, and the right one."""

    def test_a_rated_basis_requires_a_rate(self, db, pctx):
        with pytest.raises(ValidationFailed, match="rate_bp"):
            make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE)

    def test_a_rated_basis_refuses_a_fixed_amount(self, db, pctx):
        with pytest.raises(ValidationFailed):
            make_plan(
                db,
                pctx,
                basis=CommissionBasis.BILLED_VALUE,
                rate_bp=250,
                fixed_amount_minor=100,
            )

    def test_per_event_requires_a_fixed_amount(self, db, pctx):
        with pytest.raises(ValidationFailed, match="fixed_amount_minor"):
            make_plan(db, pctx, basis=CommissionBasis.PER_EVENT)

    def test_per_event_refuses_a_rate(self, db, pctx):
        with pytest.raises(ValidationFailed):
            make_plan(
                db,
                pctx,
                basis=CommissionBasis.PER_EVENT,
                fixed_amount_minor=500,
                rate_bp=250,
            )

    def test_a_negative_fixed_amount_is_refused(self, db, pctx):
        with pytest.raises(ValidationFailed, match="negative"):
            make_plan(
                db, pctx, basis=CommissionBasis.PER_EVENT, fixed_amount_minor=-1
            )

    def test_an_unknown_basis_is_refused(self, db, pctx):
        with pytest.raises(ValidationFailed, match="unknown commission basis"):
            make_plan(db, pctx, basis="GROSS_MARGIN", rate_bp=250)

    def test_there_is_no_fifth_basis(self):
        assert set(CommissionBasis.ALL) == {
            "RECORDED_VALUE",
            "BILLED_VALUE",
            "COLLECTED_VALUE",
            "PER_EVENT",
        }

    def test_the_database_refuses_a_plan_with_neither_term(self, db, tenant_a):
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO commission_plan
                      (id, tenant_id, basis, currency, effective_from,
                       created_by_user_id, created_at)
                    VALUES (gen_random_uuid(), :t, 'RECORDED_VALUE', 'PKR',
                            DATE '2026-01-01', :u, now())
                    """
                ),
                {"t": str(tenant_a.ctx.tenant_id), "u": str(tenant_a.owner.id)},
            )
        assert "exactly_one_term_for_basis" in str(exc.value)
        db.rollback()

    def test_the_database_refuses_a_per_event_plan_carrying_a_rate(self, db, tenant_a):
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO commission_plan
                      (id, tenant_id, basis, rate_bp, fixed_amount_minor, currency,
                       effective_from, created_by_user_id, created_at)
                    VALUES (gen_random_uuid(), :t, 'PER_EVENT', 250, 500, 'PKR',
                            DATE '2026-01-01', :u, now())
                    """
                ),
                {"t": str(tenant_a.ctx.tenant_id), "u": str(tenant_a.owner.id)},
            )
        assert "exactly_one_term_for_basis" in str(exc.value)
        db.rollback()


class TestEffectiveRangesNeverOverlap:
    """P0 §6: at most one plan is effective on any date, for any tenant."""

    def test_a_second_open_ended_plan_closes_the_first(self, db, pctx):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=500,
            effective_from=date(2026, 3, 1),
        )
        first, second = plans(db, pctx)
        assert first.effective_to == date(2026, 2, 28)
        assert second.effective_to is None
        assert second.rate_bp == 500

    def test_the_closed_plan_keeps_its_own_terms(self, db, pctx):
        """Closing a plan ends its range. It never restates its rate."""
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.PER_EVENT,
            fixed_amount_minor=700,
            effective_from=date(2026, 3, 1),
        )
        first = plans(db, pctx)[0]
        assert first.rate_bp == 250
        assert first.basis == CommissionBasis.RECORDED_VALUE
        assert first.fixed_amount_minor is None

    def test_a_plan_starting_on_an_existing_start_is_refused(self, db, pctx):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        with pytest.raises(CommissionPlanOverlapError):
            make_plan(db, pctx, basis=CommissionBasis.BILLED_VALUE, rate_bp=100)

    def test_a_plan_starting_before_an_existing_one_is_refused(self, db, pctx):
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=250,
            effective_from=date(2026, 2, 1),
        )
        with pytest.raises(CommissionPlanOverlapError):
            make_plan(
                db,
                pctx,
                basis=CommissionBasis.RECORDED_VALUE,
                rate_bp=300,
                effective_from=EARLY,
            )

    def test_a_plan_overlapping_a_closed_range_is_refused(self, db, pctx):
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=250,
            effective_from=EARLY,
            effective_to=date(2026, 6, 30),
        )
        with pytest.raises(CommissionPlanOverlapError):
            make_plan(
                db,
                pctx,
                basis=CommissionBasis.RECORDED_VALUE,
                rate_bp=300,
                effective_from=date(2026, 6, 1),
            )

    def test_a_plan_after_a_closed_range_is_accepted(self, db, pctx):
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=250,
            effective_from=EARLY,
            effective_to=date(2026, 6, 30),
        )
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=300,
            effective_from=date(2026, 7, 1),
        )
        assert len(plans(db, pctx)) == 2

    def test_effective_to_may_not_precede_effective_from(self, db, pctx):
        with pytest.raises(ValidationFailed, match="effective_to"):
            make_plan(
                db,
                pctx,
                basis=CommissionBasis.RECORDED_VALUE,
                rate_bp=250,
                effective_from=date(2026, 6, 1),
                effective_to=date(2026, 5, 1),
            )

    def test_the_database_itself_refuses_an_overlap(self, db, tenant_a):
        """The EXCLUDE constraint is the guarantee, not the Python check."""
        params = {"t": str(tenant_a.ctx.tenant_id), "u": str(tenant_a.owner.id)}
        insert = text(
            """
            INSERT INTO commission_plan
              (id, tenant_id, basis, rate_bp, currency, effective_from, effective_to,
               created_by_user_id, created_at)
            VALUES (gen_random_uuid(), :t, 'RECORDED_VALUE', 250, 'PKR',
                    DATE '2026-01-01', NULL, :u, now())
            """
        )
        db.execute(insert, params)
        with pytest.raises(Exception) as exc:
            db.execute(insert, params)
        assert "ex_commission_plan_effective_range_no_overlap" in str(exc.value)
        db.rollback()

    def test_two_tenants_may_hold_the_same_effective_range(
        self, db, tenant_a, tenant_b, platform_user, clock
    ):
        """The exclusion is per tenant, not global."""
        a = platform_ctx(tenant_a, platform_user, clock)
        b = platform_ctx(tenant_b, platform_user, clock)
        make_plan(db, a, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        make_plan(db, b, basis=CommissionBasis.COLLECTED_VALUE, rate_bp=800)
        assert len(plans(db, a)) == len(plans(db, b)) == 1


class TestEffectivePlanResolution:
    def test_the_plan_in_force_is_found_by_business_date(self, db, pctx):
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=250,
            effective_from=EARLY,
        )
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=500,
            effective_from=date(2026, 3, 1),
        )
        assert effective_plan(db, pctx, date(2026, 2, 14)).rate_bp == 250
        assert effective_plan(db, pctx, date(2026, 2, 28)).rate_bp == 250
        assert effective_plan(db, pctx, date(2026, 3, 1)).rate_bp == 500
        assert effective_plan(db, pctx, date(2027, 1, 1)).rate_bp == 500

    def test_a_date_before_every_plan_resolves_to_nothing(self, db, pctx):
        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        assert effective_plan(db, pctx, date(2025, 12, 31)) is None

    def test_effective_to_is_inclusive(self, db, pctx):
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=250,
            effective_from=EARLY,
            effective_to=date(2026, 6, 30),
        )
        assert effective_plan(db, pctx, date(2026, 6, 30)) is not None
        assert effective_plan(db, pctx, date(2026, 7, 1)) is None

    def test_plan_lookup_is_tenant_scoped(
        self, db, tenant_a, tenant_b, platform_user, clock
    ):
        a = platform_ctx(tenant_a, platform_user, clock)
        b = platform_ctx(tenant_b, platform_user, clock)
        make_plan(db, a, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        assert effective_plan(db, b, date(2026, 3, 15)) is None
        assert list_plans(db, b) == []


class TestPlanCreationIsAudited:
    def test_creation_and_closure_are_both_audited(self, db, pctx):
        from sqlalchemy import select

        from app.audit.models import AuditEvent

        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=500,
            effective_from=date(2026, 3, 1),
        )
        rows = list(
            db.execute(
                select(AuditEvent).where(AuditEvent.entity_type == "commission_plan")
            )
            .scalars()
            .all()
        )
        actions = {r.action for r in rows}
        assert actions == {"commission_plan.created", "commission_plan.closed"}
        # AUD-9: every row records where it came from, and platform actions are
        # recorded as platform actions.
        assert {r.actor_scope for r in rows} == {"PLATFORM"}
        assert {r.source for r in rows} == {"PLATFORM"}

    def test_the_close_audit_records_the_before_and_after_range(self, db, pctx):
        from sqlalchemy import select

        from app.audit.models import AuditEvent

        make_plan(db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250)
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=500,
            effective_from=date(2026, 3, 1),
        )
        closed = db.execute(
            select(AuditEvent).where(AuditEvent.action == "commission_plan.closed")
        ).scalar_one()
        assert closed.before["effective_to"] is None
        assert closed.after["effective_to"] == "2026-02-28"
        assert closed.before["rate_bp"] == closed.after["rate_bp"] == 250


class TestPlanTransitionSafety:
    """The review's plan-transition audit.

    Creating a plan closes its open-ended predecessor. That is only acceptable if
    it is deterministic, audited, cannot rewrite earned history, and cannot leave
    two plans simultaneously applicable. Each of those is asserted here.
    """

    def _transition(self, db, pctx, *, on):
        make_plan(
            db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250,
            effective_from=EARLY,
        )
        make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=500,
            effective_from=on,
        )
        return plans(db, pctx)

    def test_the_transition_is_deterministic(self, db, pctx):
        """The predecessor ends the day before the successor begins. No gap, no
        overlap, and no dependence on when the call happened to be made."""
        boundary = date(2026, 3, 15)
        old, new = self._transition(db, pctx, on=boundary)
        assert old.effective_to == boundary - timedelta(days=1)
        assert new.effective_from == boundary
        assert new.effective_to is None

    def test_exactly_one_plan_applies_on_every_date_across_the_boundary(
        self, db, pctx
    ):
        boundary = date(2026, 3, 15)
        self._transition(db, pctx, on=boundary)
        for offset in range(-3, 4):
            day = boundary + timedelta(days=offset)
            covering = [
                p
                for p in plans(db, pctx)
                if p.effective_from <= day
                and (p.effective_to is None or p.effective_to >= day)
            ]
            assert len(covering) == 1, f"{len(covering)} plans cover {day}"
            assert effective_plan(db, pctx, day).id == covering[0].id
            assert covering[0].rate_bp == (500 if offset >= 0 else 250)

    def test_the_database_agrees_that_no_date_is_covered_twice(self, db, pctx):
        """Asked of PostgreSQL directly, not of the resolution helper."""
        self._transition(db, pctx, on=date(2026, 3, 15))
        worst = db.execute(
            text(
                """
                SELECT max(c) FROM (
                    SELECT count(*) AS c
                    FROM generate_series(DATE '2025-12-01', DATE '2027-01-01',
                                         INTERVAL '1 day') AS d(day)
                    JOIN commission_plan p
                      ON p.tenant_id = :t
                     AND daterange(p.effective_from, p.effective_to, '[]')
                         @> d.day::date
                    GROUP BY d.day
                ) counts
                """
            ),
            {"t": str(pctx.tenant_id)},
        ).scalar()
        assert worst == 1

    def test_the_transition_rewrites_no_earned_history(
        self, db, tenant_a, customer_factory, pctx
    ):
        """COM-3/COM-10 from the plan side: the close touches events not at all."""
        from tests._commission import adjustments, events, snapshot_rows
        from tests._ops import do_record

        make_plan(
            db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250,
            effective_from=EARLY,
        )
        customer = customer_factory(tenant_a.ctx, code="PT", price_minor=25000)
        do_record(db, tenant_a.ctx, customer, quantity="4")
        before = snapshot_rows(events(db, pctx))
        assert before

        make_plan(
            db,
            pctx,
            basis=CommissionBasis.COLLECTED_VALUE,
            rate_bp=800,
            effective_from=tenant_a.ctx.today,
        )
        db.expire_all()
        assert snapshot_rows(events(db, pctx)) == before
        assert adjustments(db, pctx) == []

    def test_a_new_plan_governs_only_dates_inside_its_own_range(
        self, db, tenant_a, customer_factory, pctx
    ):
        """A record backdated into the *old* range still earns the old terms, even
        though the new plan is the one in force today. Plan resolution follows the
        source fact's business date, not the wall clock."""
        from tests._commission import events
        from tests._ops import do_record

        boundary = tenant_a.ctx.today
        self._transition(db, pctx, on=boundary)

        backdated = customer_factory(tenant_a.ctx, code="BD", price_minor=25000)
        current = customer_factory(tenant_a.ctx, code="CU", price_minor=25000)
        do_record(
            db,
            tenant_a.ctx,
            backdated,
            quantity="4",
            service_date=boundary - timedelta(days=5),
        )
        do_record(db, tenant_a.ctx, current, quantity="4", service_date=boundary)

        by_date = {e.occurred_on: e for e in events(db, pctx)}
        assert by_date[boundary - timedelta(days=5)].rate_bp_snapshot == 250
        assert by_date[boundary].rate_bp_snapshot == 500

    def test_retrying_the_same_plan_creation_is_idempotent(self, db, pctx):
        """SYN-1/2: the existing register, not a commission-specific mechanism."""
        from app.core.ids import uuid7

        op = uuid7()
        first = make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=250,
            operation_id=op,
        )
        second = make_plan(
            db,
            pctx,
            basis=CommissionBasis.RECORDED_VALUE,
            rate_bp=250,
            operation_id=op,
        )
        assert first.status == "APPLIED"
        assert second.status == "DUPLICATE"
        assert second.result["id"] == first.result["id"]
        assert len(plans(db, pctx)) == 1

    def test_retrying_a_transition_does_not_close_the_predecessor_twice(
        self, db, pctx
    ):
        """The replay never re-runs the effect, so the predecessor keeps the one
        end date it was given."""
        from app.core.ids import uuid7

        make_plan(
            db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250,
            effective_from=EARLY,
        )
        op = uuid7()
        for _ in range(2):
            make_plan(
                db,
                pctx,
                basis=CommissionBasis.RECORDED_VALUE,
                rate_bp=500,
                effective_from=date(2026, 3, 1),
                operation_id=op,
            )
        db.expire_all()
        rows = plans(db, pctx)
        assert len(rows) == 2
        assert rows[0].effective_to == date(2026, 2, 28)
        assert rows[1].effective_to is None

    def test_a_failed_transition_leaves_the_predecessor_open(self, db, pctx):
        """The close and the insert share one transaction: if the new plan is
        refused, the old one must not be left silently ended."""
        make_plan(
            db, pctx, basis=CommissionBasis.RECORDED_VALUE, rate_bp=250,
            effective_from=EARLY,
        )
        with pytest.raises(ValidationFailed):
            make_plan(
                db,
                pctx,
                basis=CommissionBasis.PER_EVENT,  # no fixed_amount_minor
                effective_from=date(2026, 3, 1),
            )
        db.rollback()
        rows = plans(db, pctx)
        assert len(rows) == 1
        assert rows[0].effective_to is None
