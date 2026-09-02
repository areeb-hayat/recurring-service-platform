"""Billing cycles and posting-cycle resolution.

Covers P0 §5.5 and FIN-9: one OPEN cycle per tenant, contiguous periods, close,
and the late-correction rule — an adjustment keeps its true ``occurred_on`` but
posts to the currently open cycle, so an issued statement is never rewritten.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.billing.cycles import (
    close_cycle,
    ensure_open_cycle,
    list_cycles,
    load_cycle,
    open_cycle,
    period_bounds,
)
from app.billing.models import BillingCycle, CycleStatus, EntryKind, LedgerEntry
from app.core.errors import (
    CyclePeriodNotEndedError,
    CycleRolloverRequiredError,
    NotFoundError,
    ValidationFailed,
)
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
    entries,
)

pytestmark = pytest.mark.postgres

PRICE = 25000


def _utc(y, m, d, hour=7):
    return datetime(y, m, d, hour, tzinfo=timezone.utc)


class TestPeriodArithmetic:
    """Calendar-monthly is the frozen V1 default (P0 §16); the start day is config."""

    def test_calendar_month_is_the_default_period(self, tenant_a):
        assert period_bounds(tenant_a.ctx, date(2026, 3, 15)) == (
            date(2026, 3, 1),
            date(2026, 3, 31),
        )

    @pytest.mark.parametrize(
        "day,expected",
        [
            (date(2026, 1, 1), (date(2026, 1, 1), date(2026, 1, 31))),
            (date(2026, 2, 28), (date(2026, 2, 1), date(2026, 2, 28))),
            (date(2024, 2, 10), (date(2024, 2, 1), date(2024, 2, 29))),  # leap year
            (date(2026, 12, 31), (date(2026, 12, 1), date(2026, 12, 31))),
        ],
    )
    def test_month_lengths_including_february_and_year_end(self, tenant_a, day, expected):
        assert period_bounds(tenant_a.ctx, day) == expected

    def test_cycle_start_day_shifts_the_period(self, db, tenant_a):
        tenant_a.tenant.cycle_start_day = 15
        db.flush()
        ctx = ctx_at(tenant_a, _utc(2026, 3, 20))
        assert period_bounds(ctx, date(2026, 3, 20)) == (date(2026, 3, 15), date(2026, 4, 14))
        assert period_bounds(ctx, date(2026, 3, 10)) == (date(2026, 2, 15), date(2026, 3, 14))

    def test_unimplemented_cycle_type_fails_closed(self, db, tenant_a):
        """D7 is deferred behind ``tenant.cycle_type``; guessing would be worse."""
        tenant_a.tenant.cycle_type = "WEEKLY"
        db.flush()
        ctx = ctx_at(tenant_a, _utc(2026, 3, 15))
        with pytest.raises(ValidationFailed) as exc:
            period_bounds(ctx, date(2026, 3, 15))
        assert "not implemented" in str(exc.value)


class TestOneOpenCyclePerTenant:
    def test_open_cycle_is_created_on_demand(self, db, tenant_a):
        assert open_cycle(db, tenant_a.ctx) is None
        cycle = ensure_open_cycle(db, tenant_a.ctx)
        assert cycle.status == CycleStatus.OPEN
        assert (cycle.period_start, cycle.period_end) == (date(2026, 3, 1), date(2026, 3, 31))

    def test_ensure_open_cycle_is_stable(self, db, tenant_a):
        first = ensure_open_cycle(db, tenant_a.ctx)
        assert ensure_open_cycle(db, tenant_a.ctx).id == first.id
        assert len(list_cycles(db, tenant_a.ctx)) == 1

    def test_second_open_cycle_is_refused_by_the_database(self, db, tenant_a):
        """The partial unique index is the guarantee, not application care."""
        ensure_open_cycle(db, tenant_a.ctx)
        db.commit()
        with pytest.raises(Exception) as exc:
            db.execute(
                text(
                    """
                    INSERT INTO billing_cycle
                      (id, tenant_id, period_start, period_end, status, created_at)
                    VALUES (gen_random_uuid(), :t, DATE '2026-04-01', DATE '2026-04-30',
                            'OPEN', now())
                    """
                ),
                {"t": str(tenant_a.ctx.tenant_id)},
            )
        assert "uq_billing_cycle_one_open_per_tenant" in str(exc.value)
        db.rollback()

    def test_each_tenant_has_its_own_open_cycle(self, db, tenant_a, tenant_b):
        a = ensure_open_cycle(db, tenant_a.ctx)
        b = ensure_open_cycle(db, tenant_b.ctx)
        assert a.id != b.id
        assert list_cycles(db, tenant_a.ctx) == [a]
        assert list_cycles(db, tenant_b.ctx) == [b]


class TestPostingCycleResolution:
    """The P1 deferred boundary: every entry now resolves a cycle."""

    def test_charge_posts_to_the_open_cycle(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("2"))
        cycle = open_cycle(db, tenant_a.ctx)
        [entry] = entries(db, tenant_a.ctx, customer.id)
        assert entry.posting_cycle_id == cycle.id
        assert entry.occurred_on == tenant_a.ctx.today

    def test_no_entry_is_ever_left_without_a_cycle(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        do_correct(db, tenant_a.ctx, _active_id(db, tenant_a.ctx, customer),
                   quantity=Decimal("3"), reason="miscount")
        unassigned = db.execute(
            select(LedgerEntry).where(LedgerEntry.posting_cycle_id.is_(None))
        ).scalars().all()
        assert unassigned == []

    def test_backdated_record_keeps_its_date_but_posts_to_the_open_cycle(
        self, db, tenant_a, customer_factory
    ):
        """occurred_on is the truth; posting_cycle_id is where it gets billed."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        old = tenant_a.ctx.today - timedelta(days=60)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"), service_date=old)
        cycle = open_cycle(db, tenant_a.ctx)
        [entry] = entries(db, tenant_a.ctx, customer.id)
        assert entry.occurred_on == old
        assert entry.posting_cycle_id == cycle.id
        assert cycle.period_start <= tenant_a.ctx.today <= cycle.period_end


def _active_id(db, ctx, customer):
    from app.service.models import DailyServiceRecord, RecordStatus

    return db.execute(
        select(DailyServiceRecord.id).where(
            DailyServiceRecord.tenant_id == ctx.tenant_id,
            DailyServiceRecord.customer_id == customer.id,
            DailyServiceRecord.status == RecordStatus.ACTIVE,
        )
    ).scalar_one()


class TestClose:
    def test_close_marks_the_cycle_and_records_actor_and_instant(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        cycle = open_cycle(db, tenant_a.ctx)

        closing_ctx, outcome = close_after_period_end(db, tenant_a, cycle)
        db.refresh(cycle)
        assert outcome.result["status"] == CycleStatus.CLOSED
        assert cycle.status == CycleStatus.CLOSED
        assert cycle.closed_at == closing_ctx.now
        assert cycle.closed_by_user_id == tenant_a.owner.id

    def test_closing_a_closed_cycle_is_refused(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        cycle = open_cycle(db, tenant_a.ctx)
        closing_ctx, _ = close_after_period_end(db, tenant_a, cycle)
        with pytest.raises(ValidationFailed):
            close_cycle(db, closing_ctx, cycle.id, operation_id=uuid7())

    def test_unknown_cycle_is_404(self, db, tenant_a):
        with pytest.raises(NotFoundError):
            close_cycle(db, tenant_a.ctx, uuid7(), operation_id=uuid7())

    def test_another_tenants_cycle_is_404_not_403(self, db, tenant_a, tenant_b):
        cycle = ensure_open_cycle(db, tenant_a.ctx)
        db.commit()
        with pytest.raises(NotFoundError):
            close_cycle(db, tenant_b.ctx, cycle.id, operation_id=uuid7())

    def test_close_writes_an_audit_event(self, db, tenant_a, customer_factory):
        from app.audit.models import AuditAction, AuditEvent

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        cycle = open_cycle(db, tenant_a.ctx)
        close_after_period_end(db, tenant_a, cycle)

        event = db.execute(
            select(AuditEvent).where(AuditEvent.action == AuditAction.BILLING_CYCLE_CLOSED)
        ).scalar_one()
        assert event.entity_id == cycle.id
        assert event.before["status"] == "OPEN"
        assert event.after["status"] == "CLOSED"
        assert event.actor_user_id == tenant_a.owner.id

    def test_the_next_cycle_is_the_next_full_period(
        self, db, tenant_a, customer_factory
    ):
        """Periods are contiguous, never overlapping, and never shortened."""
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        first = open_cycle(db, tenant_a.ctx)
        closing_ctx, _ = close_after_period_end(db, tenant_a, first)

        do_record(
            db,
            closing_ctx,
            customer,
            quantity=Decimal("1"),
            service_date=date(2026, 4, 1),
        )
        second = open_cycle(db, closing_ctx)
        assert second.id != first.id
        assert second.period_start == first.period_end + timedelta(days=1)
        assert (second.period_start, second.period_end) == (
            date(2026, 4, 1),
            date(2026, 4, 30),
        )


class TestFIN9LateCorrection:
    """A correction to a closed period keeps its date and moves to the open cycle."""

    def test_correction_of_a_closed_cycle_posts_forward(
        self, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        january = ctx_at(tenant_a, _utc(2026, 1, 20))
        do_record(
            db, january, customer, quantity=Decimal("2"), service_date=date(2026, 1, 5)
        )
        jan_cycle = open_cycle(db, january)
        record_id = _active_id(db, january, customer)
        close_after_period_end(db, tenant_a, jan_cycle)

        february = ctx_at(tenant_a, _utc(2026, 2, 10))
        do_correct(db, february, record_id, quantity=Decimal("1"), reason="over-recorded")

        feb_cycle = open_cycle(db, february)
        assert feb_cycle.id != jan_cycle.id
        adjustment = [
            e for e in entries(db, february, customer.id)
            if e.entry_kind == EntryKind.ADJUSTMENT
        ][0]
        # The true service date survives; only the billing period moves.
        assert adjustment.occurred_on == date(2026, 1, 5)
        assert adjustment.posting_cycle_id == feb_cycle.id
        assert adjustment.amount_minor == -PRICE

    def test_the_closed_cycle_gains_no_entry(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        january = ctx_at(tenant_a, _utc(2026, 1, 20))
        do_record(
            db, january, customer, quantity=Decimal("2"), service_date=date(2026, 1, 5)
        )
        jan_cycle = open_cycle(db, january)
        record_id = _active_id(db, january, customer)
        close_after_period_end(db, tenant_a, jan_cycle)
        before = db.execute(
            select(LedgerEntry).where(LedgerEntry.posting_cycle_id == jan_cycle.id)
        ).scalars().all()

        february = ctx_at(tenant_a, _utc(2026, 2, 10))
        do_correct(db, february, record_id, quantity=Decimal("5"), reason="under-recorded")

        after = db.execute(
            select(LedgerEntry).where(LedgerEntry.posting_cycle_id == jan_cycle.id)
        ).scalars().all()
        assert {e.id for e in after} == {e.id for e in before}


class TestCloseOverHttp:
    def test_close_requires_the_capability_and_is_idempotent(
        self, client, clock, settings, db, tenant_a, customer_factory
    ):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        cycle_id = str(open_cycle(db, tenant_a.ctx).id)
        boundary = _utc(2026, 4, 1, 12)  # the day after period_end
        clock.set(boundary)
        auth = auth_at(tenant_a, settings, boundary)

        listed = client.get("/api/v1/billing/cycles", headers=auth)
        assert listed.status_code == 200
        assert [c["id"] for c in listed.json()["items"]] == [cycle_id]

        op = str(uuid7())
        first = client.post(
            f"/api/v1/billing/cycles/{cycle_id}/close",
            json={"operation_id": op},
            headers=auth,
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "APPLIED"
        assert first.json()["entity"]["statements_issued"] == 1

        replay = client.post(
            f"/api/v1/billing/cycles/{cycle_id}/close",
            json={"operation_id": op},
            headers=auth,
        )
        assert replay.json()["status"] == "DUPLICATE"
        assert replay.json()["entity"] == first.json()["entity"]

    def test_a_replay_does_not_issue_a_second_statement(
        self, client, clock, settings, db, tenant_a, customer_factory
    ):
        from app.billing.models import Statement

        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        cycle_id = str(open_cycle(db, tenant_a.ctx).id)
        boundary = _utc(2026, 4, 1, 12)
        clock.set(boundary)
        auth = auth_at(tenant_a, settings, boundary)
        op = str(uuid7())
        for _ in range(3):
            client.post(
                f"/api/v1/billing/cycles/{cycle_id}/close",
                json={"operation_id": op},
                headers=auth,
            )
        db.expire_all()
        assert len(db.execute(select(Statement)).scalars().all()) == 1


class TestEarlyCloseIsRefused:
    """A cycle runs its configured length. There is no early close and no
    override flag: shortening a period, and pushing its remaining days into the
    next bill, was never a client decision to make on their behalf."""

    @pytest.fixture
    def cycle(self, db, tenant_a, customer_factory):
        customer = customer_factory(tenant_a.ctx, price_minor=PRICE)
        do_record(db, tenant_a.ctx, customer, quantity=Decimal("1"))
        return open_cycle(db, tenant_a.ctx)

    @pytest.mark.parametrize("day", [1, 15, 30, 31])
    def test_closing_on_or_before_period_end_is_refused(self, db, tenant_a, cycle, day):
        """period_end is inclusive: the 31st is still inside the March cycle, so
        closing that day would strand any business recorded later the same day."""
        ctx = ctx_at(tenant_a, _utc(2026, 3, day, 12))
        with pytest.raises(CyclePeriodNotEndedError) as exc:
            close_cycle(db, ctx, cycle.id, operation_id=uuid7())
        assert exc.value.code == "CYCLE_PERIOD_NOT_ENDED"
        assert "2026-03-31" in str(exc.value)

    def test_a_refused_close_changes_nothing(self, db, tenant_a, cycle):
        from app.billing.models import Statement

        ctx = ctx_at(tenant_a, _utc(2026, 3, 15, 12))
        with pytest.raises(CyclePeriodNotEndedError):
            close_cycle(db, ctx, cycle.id, operation_id=uuid7())
        db.rollback()
        db.expire_all()
        reread = load_cycle(db, tenant_a.ctx, cycle.id)
        assert reread.status == CycleStatus.OPEN
        assert reread.closed_at is None
        assert db.execute(select(Statement)).scalars().all() == []

    def test_closing_the_day_after_period_end_succeeds(self, db, tenant_a, cycle):
        """The earliest valid close: business_date > period_end."""
        ctx = ctx_at(tenant_a, _utc(2026, 4, 1, 12))
        outcome = do_close_cycle(db, ctx, cycle.id)
        assert outcome.result["status"] == CycleStatus.CLOSED

    def test_closing_later_still_succeeds(self, db, tenant_a, cycle):
        ctx = ctx_at(tenant_a, _utc(2026, 4, 9, 12))
        outcome = do_close_cycle(db, ctx, cycle.id)
        assert outcome.result["status"] == CycleStatus.CLOSED

    def test_no_shortened_or_synthetic_cycle_is_ever_created(
        self, db, tenant_a, cycle, customer_factory
    ):
        """Every cycle spans a full configured period, start to end."""
        ctx = ctx_at(tenant_a, _utc(2026, 4, 9, 12))
        do_close_cycle(db, ctx, cycle.id)
        customer = customer_factory(ctx, code="NEXT", price_minor=PRICE)
        do_record(db, ctx, customer, quantity=Decimal("1"))

        for existing in list_cycles(db, ctx):
            start, end = period_bounds(ctx, existing.period_start)
            assert (existing.period_start, existing.period_end) == (start, end)

    def test_the_api_reports_the_refusal_with_its_code(
        self, client, db, tenant_a, cycle
    ):
        resp = client.post(
            f"/api/v1/billing/cycles/{cycle.id}/close",
            json={"operation_id": str(uuid7())},
            headers=tenant_a.auth,
        )
        assert resp.status_code == 422
        body = resp.json()["error"]
        assert body["code"] == "CYCLE_PERIOD_NOT_ENDED"
        assert body["period_end"] == "2026-03-31"
        assert body["business_date"] == "2026-03-15"

    def test_the_api_still_refuses_on_the_final_day_of_the_period(
        self, client, clock, settings, db, tenant_a, cycle
    ):
        from tests._ops import auth_at

        final_day = _utc(2026, 3, 31, 12)
        clock.set(final_day)
        resp = client.post(
            f"/api/v1/billing/cycles/{cycle.id}/close",
            json={"operation_id": str(uuid7())},
            headers=auth_at(tenant_a, settings, final_day),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "CYCLE_PERIOD_NOT_ENDED"

    def test_a_refused_close_consumes_no_operation_id(
        self, client, clock, settings, db, tenant_a, cycle
    ):
        """The register records applied effects only, so the same intent can be
        retried once the period has actually ended (P1 §7)."""
        cycle_id = str(cycle.id)
        op = str(uuid7())
        first = client.post(
            f"/api/v1/billing/cycles/{cycle_id}/close",
            json={"operation_id": op},
            headers=tenant_a.auth,
        )
        assert first.status_code == 422

        db.expire_all()
        boundary = _utc(2026, 4, 1, 12)
        clock.set(boundary)
        retry = client.post(
            f"/api/v1/billing/cycles/{cycle_id}/close",
            json={"operation_id": op},
            headers=auth_at(tenant_a, settings, boundary),
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["status"] == "APPLIED"


class TestPeriodBoundaryAndRollover:
    """The August/September walkthrough, end to end (A..H).

    ``period_end`` is inclusive, so the cycle is still running all through
    31 August; it may not be closed until 1 September; and an August cycle left
    open into September must not swallow September's business.
    """

    AUG_START = date(2026, 8, 1)
    AUG_END = date(2026, 8, 31)
    SEP_START = date(2026, 9, 1)

    @pytest.fixture
    def august(self, db, tenant_a, customer_factory):
        """A customer and an open August cycle, seen from 31 August."""
        ctx = ctx_at(tenant_a, _utc(2026, 8, 31, 12))
        customer = customer_factory(ctx, code="AUG", price_minor=PRICE)
        cycle = ensure_open_cycle(db, ctx)
        assert (cycle.period_start, cycle.period_end) == (self.AUG_START, self.AUG_END)
        return ctx, customer, cycle

    # --- A ---------------------------------------------------------------
    def test_A_a_new_service_on_the_final_day_posts_to_august(self, db, august):
        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("2"))
        [entry] = entries(db, ctx, customer.id)
        assert entry.occurred_on == self.AUG_END
        assert entry.posting_cycle_id == cycle.id

    # --- B ---------------------------------------------------------------
    def test_B_a_new_payment_on_the_final_day_posts_to_august(self, db, august):
        ctx, customer, cycle = august
        do_pay(db, ctx, customer, 4000)
        [entry] = entries(db, ctx, customer.id)
        assert entry.entry_kind == EntryKind.PAYMENT
        assert entry.occurred_on == self.AUG_END
        assert entry.posting_cycle_id == cycle.id

    # --- C ---------------------------------------------------------------
    def test_C_closing_on_the_final_day_is_rejected(self, db, august):
        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("1"))
        with pytest.raises(CyclePeriodNotEndedError) as exc:
            close_cycle(db, ctx, cycle.id, operation_id=uuid7())
        assert exc.value.extra["period_end"] == "2026-08-31"
        assert exc.value.extra["business_date"] == "2026-08-31"

    def test_C_and_the_cycle_is_untouched_afterwards(self, db, tenant_a, august):
        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("1"))
        with pytest.raises(CyclePeriodNotEndedError):
            close_cycle(db, ctx, cycle.id, operation_id=uuid7())
        db.rollback()
        db.expire_all()
        assert load_cycle(db, ctx, cycle.id).status == CycleStatus.OPEN

    # --- D ---------------------------------------------------------------
    def test_D_closing_on_the_first_of_september_is_allowed(self, db, tenant_a, august):
        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("1"))
        september = ctx_at(tenant_a, _utc(2026, 9, 1, 12))
        outcome = do_close_cycle(db, september, cycle.id)
        assert outcome.result["status"] == CycleStatus.CLOSED
        assert outcome.result["statements_issued"] == 1

    # --- E ---------------------------------------------------------------
    def test_E_a_september_service_never_falls_into_an_expired_august(
        self, db, tenant_a, august
    ):
        """The defect this audit exists to prevent: August still OPEN on
        1 September must not silently absorb September's service."""
        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("1"))
        september = ctx_at(tenant_a, _utc(2026, 9, 1, 12))

        with pytest.raises(CycleRolloverRequiredError) as exc:
            do_record(db, september, customer, quantity=Decimal("3"))
        assert exc.value.code == "CYCLE_ROLLOVER_REQUIRED"
        assert exc.value.extra["period_end"] == "2026-08-31"
        db.rollback()
        db.expire_all()

        # Nothing landed in August, and no cycle was invented or auto-closed.
        posted = entries(db, ctx, customer.id)
        assert [e.amount_minor for e in posted] == [PRICE]
        assert load_cycle(db, ctx, cycle.id).status == CycleStatus.OPEN
        assert len(list_cycles(db, ctx)) == 1

    # --- F ---------------------------------------------------------------
    def test_F_a_september_payment_fails_closed_the_same_way(
        self, db, tenant_a, august
    ):
        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("1"))
        september = ctx_at(tenant_a, _utc(2026, 9, 1, 12))

        with pytest.raises(CycleRolloverRequiredError):
            do_pay(db, september, customer, 5000)
        db.rollback()
        db.expire_all()

        assert [e.entry_kind for e in entries(db, ctx, customer.id)] == [EntryKind.CHARGE]

    def test_F_the_api_reports_the_rollover_as_a_conflict(
        self, client, clock, settings, db, tenant_a, august
    ):
        from tests._ops import auth_at

        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("1"))
        db.commit()

        first_of_september = _utc(2026, 9, 1, 12)
        clock.set(first_of_september)
        resp = client.post(
            "/api/v1/payments",
            json={
                "operation_id": str(uuid7()),
                "customer_id": str(customer.id),
                "amount_minor": 5000,
            },
            headers=auth_at(tenant_a, settings, first_of_september),
        )
        assert resp.status_code == 409
        body = resp.json()["error"]
        assert body["code"] == "CYCLE_ROLLOVER_REQUIRED"
        assert body["period_end"] == "2026-08-31"
        assert body["business_date"] == "2026-09-01"

    # --- G ---------------------------------------------------------------
    def test_G_after_a_proper_rollover_september_events_post_to_september(
        self, db, tenant_a, august
    ):
        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("1"))
        september = ctx_at(tenant_a, _utc(2026, 9, 1, 12))

        do_close_cycle(db, september, cycle.id)  # the proper close operation
        do_record(db, september, customer, quantity=Decimal("2"))
        do_pay(db, september, customer, 1500)

        sep_cycle = open_cycle(db, september)
        assert sep_cycle.id != cycle.id
        assert (sep_cycle.period_start, sep_cycle.period_end) == (
            self.SEP_START,
            date(2026, 9, 30),
        )
        posted = entries(db, september, customer.id)
        by_cycle = {e.posting_cycle_id for e in posted}
        assert by_cycle == {cycle.id, sep_cycle.id}
        september_entries = [e for e in posted if e.posting_cycle_id == sep_cycle.id]
        assert sorted(e.amount_minor for e in september_entries) == [-1500, 2 * PRICE]

    # --- H ---------------------------------------------------------------
    def test_H_a_late_correction_still_keeps_its_date_and_posts_forward(
        self, db, tenant_a, august
    ):
        """The frozen late-correction rule is untouched by the rollover guard:
        historical `occurred_on`, adjustment into the current valid open cycle."""
        ctx, customer, cycle = august
        do_record(
            db, ctx, customer, quantity=Decimal("4"), service_date=date(2026, 8, 10)
        )
        record_id = db.execute(
            select(DailyServiceRecord.id).where(
                DailyServiceRecord.tenant_id == ctx.tenant_id,
                DailyServiceRecord.customer_id == customer.id,
                DailyServiceRecord.status == RecordStatus.ACTIVE,
            )
        ).scalar_one()

        september = ctx_at(tenant_a, _utc(2026, 9, 5, 12))
        do_close_cycle(db, ctx_at(tenant_a, _utc(2026, 9, 1, 12)), cycle.id)
        do_correct(db, september, record_id, quantity=Decimal("1"), reason="over-recorded")

        sep_cycle = open_cycle(db, september)
        adjustment = [
            e for e in entries(db, september, customer.id)
            if e.entry_kind == EntryKind.ADJUSTMENT
        ][0]
        assert adjustment.occurred_on == date(2026, 8, 10)  # the truth survives
        assert adjustment.posting_cycle_id == sep_cycle.id  # billed next cycle
        assert adjustment.amount_minor == -3 * PRICE

    # --- the backdating direction stays open ------------------------------
    def test_a_backdated_record_into_a_closed_period_still_posts_forward(
        self, db, tenant_a, august
    ):
        """The guard is one-directional. An entry may post to a cycle that began
        before it; only a cycle that *ended* before it is refused."""
        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("1"))
        september = ctx_at(tenant_a, _utc(2026, 9, 5, 12))
        do_close_cycle(db, ctx_at(tenant_a, _utc(2026, 9, 1, 12)), cycle.id)

        do_record(
            db, september, customer, quantity=Decimal("2"), service_date=date(2026, 8, 4)
        )
        sep_cycle = open_cycle(db, september)
        backdated = [
            e for e in entries(db, september, customer.id) if e.occurred_on == date(2026, 8, 4)
        ][0]
        assert backdated.posting_cycle_id == sep_cycle.id

    def test_the_new_cycle_is_the_full_period_containing_today(
        self, db, tenant_a, august
    ):
        """No shortened, extended or synthetic period, and no gap-filling."""
        ctx, customer, cycle = august
        do_record(db, ctx, customer, quantity=Decimal("1"))
        do_close_cycle(db, ctx_at(tenant_a, _utc(2026, 9, 1, 12)), cycle.id)

        november = ctx_at(tenant_a, _utc(2026, 11, 20, 12))
        do_record(db, november, customer, quantity=Decimal("1"))
        opened = open_cycle(db, november)
        assert (opened.period_start, opened.period_end) == (
            date(2026, 11, 1),
            date(2026, 11, 30),
        )
        assert opened.period_start == period_bounds(november, november.today)[0]
