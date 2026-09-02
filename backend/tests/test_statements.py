"""Issued statements: the movement identity, carry-forward and immutability.

Covers FIN-3 (no drift), FIN-6 (price snapshots survive into the bill), FIN-8 and
FIN-9. The origin split is asserted directly: a voided payment must land in
``payment_reversals_minor`` and never in ``service_adjustments_minor``, because
billed value (FIN-15) is read from those two columns.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.billing.cycles import ensure_open_cycle, open_cycle
from app.billing.models import Statement
from app.billing.statements import list_statements, load_statement
from app.core.errors import NotFoundError, ValidationFailed
from app.core.ids import uuid7
from app.service.models import DailyServiceRecord, RecordStatus
from tests._ops import (
    auth_at,
    close_after_period_end,
    ctx_at,
    do_close_cycle,
    do_correct,
    do_pay,
    do_record,
    do_void_payment,
    entries,
)

pytestmark = pytest.mark.postgres

PRICE = 25000


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


def _statement(db, ctx, customer, cycle_id=None) -> Statement:
    stmt = select(Statement).where(
        Statement.tenant_id == ctx.tenant_id, Statement.customer_id == customer.id
    )
    if cycle_id is not None:
        stmt = stmt.where(Statement.cycle_id == cycle_id)
    return db.execute(stmt).scalars().one()


def _identity_holds(s: Statement) -> bool:
    return s.closing_balance_minor == (
        s.opening_balance_minor
        + s.charges_minor
        + s.service_adjustments_minor
        - s.payments_minor
        + s.payment_reversals_minor
    )


class TestFIN8Identity:
    def test_FIN8_the_movement_identity_holds(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("2"))  # 50000
        do_correct(
            db,
            tenant_a.ctx,
            _active_id(db, tenant_a.ctx, customer),
            quantity=Decimal("1"),
            reason="over-recorded",
        )  # -25000
        payment = do_pay(db, tenant_a.ctx, customer, 10000)
        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="bounced")
        do_pay(db, tenant_a.ctx, customer, 5000)

        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)

        s = _statement(db, tenant_a.ctx, customer, cycle.id)
        assert s.opening_balance_minor == 0
        assert s.charges_minor == 50000
        assert s.service_adjustments_minor == -25000
        assert s.payments_minor == 15000
        assert s.payment_reversals_minor == 10000
        assert s.closing_balance_minor == 20000
        assert _identity_holds(s)

    def test_the_closing_balance_equals_the_ledger_sum(
        self, db, tenant_a, customer_factory
    ):
        from app.billing.ledger import outstanding_minor

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("3"))
        do_pay(db, tenant_a.ctx, customer, 20000)
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)

        s = _statement(db, tenant_a.ctx, customer, cycle.id)
        assert s.closing_balance_minor == outstanding_minor(db, tenant_a.ctx, customer.id)

    def test_the_origin_split_is_never_merged(self, db, tenant_a, customer_factory):
        """A voided payment must not appear as a service adjustment — that is the
        contamination FIN-15 exists to prevent."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        payment = do_pay(db, tenant_a.ctx, customer, 30000)
        do_void_payment(db, tenant_a.ctx, payment.result["id"], reason="reversed")

        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)
        s = _statement(db, tenant_a.ctx, customer, cycle.id)
        assert s.service_adjustments_minor == 0
        assert s.payment_reversals_minor == 30000

    def test_service_days_and_total_quantity(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        for offset in range(3):
            do_record(
                db,
                tenant_a.ctx,
                customer,
                quantity=Decimal("1.5"),
                service_date=tenant_a.ctx.today - timedelta(days=offset),
            )
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)
        s = _statement(db, tenant_a.ctx, customer, cycle.id)
        assert s.service_days == 3
        assert s.total_quantity == Decimal("4.500")
        assert s.unit_label == "bottle"
        assert s.currency == "PKR"

    def test_FIN7_a_skip_adds_no_charge_and_no_service_day(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        do_record(
            db,
            tenant_a.ctx,
            customer,
            kind="SKIP",
            service_date=tenant_a.ctx.today - timedelta(days=1),
        )
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)
        s = _statement(db, tenant_a.ctx, customer, cycle.id)
        assert s.service_days == 1
        assert s.charges_minor == PRICE

    def test_FIN6_a_later_price_change_does_not_restate_the_bill(
        self, db, tenant_a, customer_factory
    ):
        """A-FIN-6's statement clause, testable for the first time in P2."""
        customer = customer_factory(tenant_a.ctx, price_minor=25000)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("3"))  # 75000
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)

        customer.unit_price_minor = 30000
        db.flush()

        s = _statement(db, tenant_a.ctx, customer, cycle.id)
        assert s.charges_minor == 75000


class TestFIN3NoDrift:
    def test_FIN3_the_statement_total_is_the_sum_of_its_lines(
        self, db, tenant_a, customer_factory
    ):
        """A-FIN-3: 1000 random records, no drift between lines and the total.

        Spread across customers because one customer may hold only one ACTIVE
        record per service date (SYN-4).
        """
        rng = random.Random(20260315)
        # Late in the month, so all 25 service dates are in the past (R4).
        ctx = ctx_at(tenant_a, _utc(2026, 3, 31, 12))
        expected: dict = {}
        for index in range(40):
            customer = customer_factory(
                ctx, code=f"D{index:03d}", price_minor=rng.randrange(1, 500_000)
            )
            total = 0
            for day in range(25):
                quantity = Decimal(rng.randrange(1, 100_000)).scaleb(-3)
                outcome = do_record(
                    db,
                    ctx,
                    customer,
                    quantity=quantity,
                    service_date=date(2026, 3, day + 1),
                )
                total += outcome.result["charge_minor"]
            expected[customer.id] = total

        cycle = open_cycle(db, ctx)
        # period_end is inclusive, so the close happens the day after it.
        close_after_period_end(db, tenant_a, cycle)

        rows = db.execute(select(Statement).where(Statement.cycle_id == cycle.id))
        issued = {s.customer_id: s.charges_minor for s in rows.scalars().all()}
        assert issued == expected


class TestCarryForward:
    def test_FIN8_opening_equals_the_previous_closing_over_three_cycles(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        closings = []
        for month in (1, 2, 3):
            ctx = ctx_at(tenant_a, _utc(2026, month, 10))
            do_record(
                db, ctx, customer, quantity=Decimal("2"), service_date=date(2026, month, 5)
            )
            do_pay(db, ctx, customer, 10000, received_on=date(2026, month, 6))
            cycle = open_cycle(db, ctx)
            close_after_period_end(db, tenant_a, cycle)
            closings.append(_statement(db, ctx, customer, cycle.id))

        assert [s.opening_balance_minor for s in closings] == [
            0,
            closings[0].closing_balance_minor,
            closings[1].closing_balance_minor,
        ]
        assert all(_identity_holds(s) for s in closings)
        # Carry-forward needs no transfer entry: the ledger is continuous.
        assert closings[-1].closing_balance_minor == 3 * (50000 - 10000)

    def test_a_customer_with_no_movement_still_carries_the_balance_forward(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        january = ctx_at(tenant_a, _utc(2026, 1, 10))
        do_record(
            db, january, customer, quantity=Decimal("2"), service_date=date(2026, 1, 5)
        )
        jan_cycle = open_cycle(db, january)
        close_after_period_end(db, tenant_a, jan_cycle)

        february = ctx_at(tenant_a, _utc(2026, 2, 10))
        ensure_open_cycle(db, february)
        feb_cycle = open_cycle(db, february)
        close_after_period_end(db, tenant_a, feb_cycle)

        feb = _statement(db, february, customer, feb_cycle.id)
        assert feb.charges_minor == 0
        assert feb.opening_balance_minor == 50000
        assert feb.closing_balance_minor == 50000

    def test_a_customer_who_has_never_transacted_gets_no_statement(
        self, db, tenant_a, customer_factory
    ):
        active = customer_factory(tenant_a.ctx, code="ACT", price_minor=PRICE)
        idle = customer_factory(tenant_a.ctx, code="IDLE", price_minor=PRICE)
        do_record(db, tenant_a.ctx, active, quantity=Decimal("1"))
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)

        assert list_statements(db, tenant_a.ctx, idle.id) == []
        assert len(list_statements(db, tenant_a.ctx, active.id)) == 1


class TestFIN9IssuedStatementsAreNeverRewritten:
    def test_A_FIN_9_january_is_unchanged_and_february_carries_the_adjustment(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        january = ctx_at(tenant_a, _utc(2026, 1, 20))
        do_record(
            db, january, customer, quantity=Decimal("4"), service_date=date(2026, 1, 5)
        )
        jan_cycle = open_cycle(db, january)
        record_id = _active_id(db, january, customer)
        close_after_period_end(db, tenant_a, jan_cycle)

        jan_statement = _statement(db, january, customer, jan_cycle.id)
        issued_form = {
            column.name: getattr(jan_statement, column.name)
            for column in Statement.__table__.columns
        }

        february = ctx_at(tenant_a, _utc(2026, 2, 10))
        do_correct(db, february, record_id, quantity=Decimal("2"), reason="over-recorded")
        feb_cycle = open_cycle(db, february)
        close_after_period_end(db, tenant_a, feb_cycle)

        db.expire_all()
        reread = load_statement(db, february, jan_statement.id)
        assert {
            column.name: getattr(reread, column.name)
            for column in Statement.__table__.columns
        } == issued_form

        feb_statement = _statement(db, february, customer, feb_cycle.id)
        assert feb_statement.opening_balance_minor == 100000
        assert feb_statement.charges_minor == 0
        assert feb_statement.service_adjustments_minor == -50000
        assert feb_statement.closing_balance_minor == 50000

        adjustment = [
            e for e in entries(db, february, customer.id) if e.entry_kind == "ADJUSTMENT"
        ][0]
        assert adjustment.occurred_on == date(2026, 1, 5)


class TestFIN8Immutability:
    def test_an_update_is_rejected_by_the_database(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)
        statement_id = _statement(db, tenant_a.ctx, customer, cycle.id).id

        with pytest.raises(Exception) as exc:
            db.execute(
                text("UPDATE statement SET charges_minor = 1 WHERE id = :i"),
                {"i": str(statement_id)},
            )
        assert "immutable" in str(exc.value)
        db.rollback()

    def test_a_delete_is_rejected_by_the_database(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)
        statement_id = _statement(db, tenant_a.ctx, customer, cycle.id).id

        with pytest.raises(Exception) as exc:
            db.execute(
                text("DELETE FROM statement WHERE id = :i"), {"i": str(statement_id)}
            )
        assert "immutable" in str(exc.value)
        db.rollback()

    def test_the_api_exposes_no_mutating_statement_route(self, app):
        from tests.test_tenant_isolation import tenant_scoped_routes

        for method, path in tenant_scoped_routes(app):
            if "statement" in path:
                assert method == "GET", f"{method} {path} would mutate a statement"

    def test_a_second_statement_for_the_same_cycle_is_refused(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)
        existing = _statement(db, tenant_a.ctx, customer, cycle.id)

        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO statement
                      (id, tenant_id, customer_id, cycle_id, issued_at,
                       opening_balance_minor, charges_minor, service_adjustments_minor,
                       payments_minor, payment_reversals_minor, closing_balance_minor,
                       service_days, total_quantity, unit_label, currency)
                    VALUES (gen_random_uuid(), :t, :c, :cy, now(),
                            0, 0, 0, 0, 0, 0, 0, 0, 'bottle', 'PKR')
                    """
                ),
                {
                    "t": str(tenant_a.ctx.tenant_id),
                    "c": str(existing.customer_id),
                    "cy": str(cycle.id),
                },
            )
        assert "uq_statement_tenant_id_customer_id_cycle_id" in str(exc.value)
        db.rollback()

    def test_the_balance_identity_is_enforced_by_a_check(
        self, db, tenant_a, customer_factory
    ):
        """FIN-8 survives a direct INSERT that bypasses the application."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        cycle = ensure_open_cycle(db, tenant_a.ctx)
        db.flush()
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO statement
                      (id, tenant_id, customer_id, cycle_id, issued_at,
                       opening_balance_minor, charges_minor, service_adjustments_minor,
                       payments_minor, payment_reversals_minor, closing_balance_minor,
                       service_days, total_quantity, unit_label, currency)
                    VALUES (gen_random_uuid(), :t, :c, :cy, now(),
                            0, 1000, 0, 0, 0, 999, 1, 1, 'bottle', 'PKR')
                    """
                ),
                {
                    "t": str(tenant_a.ctx.tenant_id),
                    "c": str(customer.id),
                    "cy": str(cycle.id),
                },
            )
        assert "ck_statement_balance_identity" in str(exc.value)
        db.rollback()


class TestOrphanedEntriesFailClosed:
    def test_issuing_refuses_while_an_entry_has_no_posting_cycle(
        self, db, tenant_a, customer_factory
    ):
        """A P1-era row would otherwise be silently omitted from the bill."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        cycle = open_cycle(db, tenant_a.ctx)
        db.execute(
            text(
                """
                INSERT INTO ledger_entry
                  (id, tenant_id, customer_id, entry_kind, amount_minor, occurred_on,
                   posting_cycle_id, source_type, source_id, created_at)
                VALUES (gen_random_uuid(), :t, :c, 'CHARGE', 500, CURRENT_DATE,
                        NULL, 'daily_service_record', gen_random_uuid(), now())
                """
            ),
            {"t": str(tenant_a.ctx.tenant_id), "c": str(customer.id)},
        )
        db.flush()
        # At the period boundary, so the refusal under test is the orphaned entry
        # and not the early-close rule.
        boundary = ctx_at(tenant_a, _utc(2026, 4, 1, 12))
        with pytest.raises(ValidationFailed) as exc:
            from app.billing.cycles import close_cycle

            close_cycle(db, boundary, cycle.id, operation_id=uuid7())
        assert "posting cycle" in str(exc.value)
        db.rollback()


class TestStatementsOverHttp:
    def test_read_one_statement_and_a_customers_list(
        self, client, clock, settings, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        cycle_id = str(open_cycle(db, tenant_a.ctx).id)
        db.commit()
        boundary = _utc(2026, 4, 1, 12)  # the day after period_end
        clock.set(boundary)
        auth = auth_at(tenant_a, settings, boundary)
        closed = client.post(
            f"/api/v1/billing/cycles/{cycle_id}/close",
            json={"operation_id": str(uuid7())},
            headers=auth,
        )
        assert closed.status_code == 200, closed.text

        listed = client.get(
            f"/api/v1/customers/{customer.id}/statements", headers=auth
        )
        assert listed.status_code == 200
        [item] = listed.json()["items"]
        assert item["charges_minor"] == 50000
        assert item["currency_exponent"] == 2
        assert item["row_version"] > 0

        one = client.get(f"/api/v1/statements/{item['id']}", headers=auth)
        assert one.status_code == 200
        assert one.json() == item

    def test_an_unknown_statement_is_404(self, client, tenant_a):
        resp = client.get(f"/api/v1/statements/{uuid7()}", headers=tenant_a.auth)
        assert resp.status_code == 404
