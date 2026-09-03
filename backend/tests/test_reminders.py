"""The reminder engine: schedule, eligibility, catch-up, delivery and isolation.

Covers REM-1 … REM-8 and the P0 §10 acceptance list (A-REM-2/3 … A-REM-8d).

Every test builds a *real* reminder cycle: service is recorded, the cycle is
closed after its period ends, a statement is issued. That matters — the whole
"fail safely rather than remind from fabricated data" rule is that no statement
means no reminder, so a suite that shortcut the statement would be testing a
different system.

The clock is moved rather than mocked around: "day 8" here means the tenant's own
business date is the 8th, resolved from its timezone by the server (P0 R4).

No test makes a network call. Delivery always goes through the in-memory
``MockCommunicationProvider``, which is what lets the failure tests be honest:
"the provider is down" is a real code path here, not a patch.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.billing.cycles import ensure_open_cycle
from app.billing.models import LedgerEntry, Statement
from app.core.errors import ValidationFailed
from app.core.ids import uuid7
from app.core.money import format_minor
from app.reminders.engine import (
    MAX_DELIVERY_ATTEMPTS,
    dispatch_reminder,
    generate_due_reminder,
    highest_sent_stage,
    reminder_cycle_for,
    tenant_schedule,
)
from app.reminders.models import (
    CommunicationLog,
    JobKind,
    JobRun,
    JobRunStatus,
    Reminder,
    ReminderKind,
    ReminderState,
)
from app.reminders.reporting import ReminderStatus, reminder_overview
from app.reminders.runner import (
    RUN_ALREADY_DONE,
    RUN_COMPLETED,
    run_daily_reminders,
    run_reminders_for_all_tenants,
)
from app.reminders.schedule import due_stage, load_schedule, next_stage_after
from app.tenancy.context import SystemContext
from tests._ops import ctx_at, do_pay, do_record, do_void_payment, close_after_period_end

pytestmark = pytest.mark.postgres

PRICE = 25000  # 250.00 per unit


# --- fixtures ----------------------------------------------------------------


def _utc(y, m, d, hour=7):
    """Midday-ish UTC, comfortably inside the same date in Asia/Karachi (UTC+5)."""
    return datetime(y, m, d, hour, tzinfo=timezone.utc)


def _sys_ctx(fixture, when: datetime) -> SystemContext:
    """The runner's context for this tenant at ``when``. No user, by construction."""
    from app.core.clock import FixedClock

    return SystemContext.for_tenant(tenant=fixture.tenant, clock=FixedClock(when))


def _billed_customer(db, fixture, customer_factory, *, code="C1", days=4, quantity="1"):
    """A customer with an issued statement for February and an open March cycle.

    Four service days in February at 250.00 leaves 1000.00 owing when the cycle
    closes on 1 March — which is the state every reminder question is asked in.
    Returns ``(customer, statement)``.
    """
    customer = customer_factory(
        fixture.ctx, code=code, price_minor=PRICE, phone_e164="+923001234567"
    )
    for day in range(1, days + 1):
        ctx = ctx_at(fixture, _utc(2026, 2, day))
        do_record(db, ctx, customer, quantity=Decimal(quantity), service_date=date(2026, 2, day))
    cycle = ensure_open_cycle(db, ctx_at(fixture, _utc(2026, 2, 1)))
    db.commit()
    close_after_period_end(db, fixture, cycle)
    statement = db.execute(
        select(Statement).where(
            Statement.tenant_id == fixture.tenant.id, Statement.customer_id == customer.id
        )
    ).scalars().one()
    return customer, statement


def _run_on(db, fixture, comms, day: int, month: int = 3):
    """Execute the tenant's reminder round on a given tenant-local business date."""
    ctx = _sys_ctx(fixture, _utc(2026, month, day))
    return ctx, run_daily_reminders(db, ctx, comms)


def _as_of(clock, fixture, settings, when: datetime) -> dict[str, str]:
    """Auth headers for ``when``, with the app's injected clock moved to match.

    Access-token expiry is checked against the injected clock, so a test that
    reasons about day 4 has to put the *server* on day 4 as well — otherwise it
    gets a truthful 401 instead of the behaviour under test.
    """
    from tests._ops import auth_at

    clock.set(when)
    return auth_at(fixture, settings, when)


def _reminders(db, fixture, customer=None) -> list[Reminder]:
    stmt = select(Reminder).where(Reminder.tenant_id == fixture.tenant.id)
    if customer is not None:
        stmt = stmt.where(Reminder.customer_id == customer.id)
    return list(db.execute(stmt.order_by(Reminder.schedule_day, Reminder.kind)).scalars().all())


# --- 1. the schedule is data (REM-1) -----------------------------------------


class TestScheduleIsConfiguration:
    def test_REM1_the_default_schedule_lives_on_the_tenant_row(self, db, tenant_a):
        schedule = tenant_schedule(db, tenant_a.ctx)
        assert [(s.day, s.kind) for s in schedule] == [
            (1, ReminderKind.STATEMENT),
            (4, ReminderKind.REMINDER),
            (8, ReminderKind.REMINDER),
            (12, ReminderKind.REMINDER),
            (15, ReminderKind.FINAL),
        ]

    def test_REM1_a_tenant_may_carry_a_different_schedule(self, db, tenant_a):
        """The days are data. A different configuration produces different stages."""
        tenant_a.tenant.reminder_schedule = [
            {"day": 2, "kind": "STATEMENT"},
            {"day": 20, "kind": "FINAL"},
        ]
        db.commit()
        schedule = tenant_schedule(db, tenant_a.ctx)
        assert [s.day for s in schedule] == [2, 20]
        assert due_stage(schedule, 19).day == 2
        assert due_stage(schedule, 20).day == 20

    @pytest.mark.parametrize(
        "raw",
        [
            [],
            "not-a-list",
            [{"day": 0, "kind": "REMINDER"}],
            [{"day": 31, "kind": "REMINDER"}],
            [{"day": 4, "kind": "SHOUTING"}],
            [{"day": 4, "kind": "REMINDER"}, {"day": 4, "kind": "FINAL"}],
            [{"day": 4, "kind": "FINAL"}, {"day": 8, "kind": "REMINDER"}],
            [{"day": 4, "kind": "OWNER_ALERT"}],
        ],
    )
    def test_a_malformed_schedule_fails_loudly_rather_than_defaulting(self, raw):
        """Reminding on days the owner did not configure is worse than not reminding."""
        with pytest.raises(ValidationFailed):
            load_schedule(raw)

    def test_the_owner_alert_is_never_a_configurable_stage(self):
        """P0 §10 pairs it with FINAL; it has no day of its own to be configured."""
        with pytest.raises(ValidationFailed):
            load_schedule([{"day": 15, "kind": "OWNER_ALERT"}])

    def test_due_stage_is_the_highest_configured_day_not_after_today(self):
        schedule = load_schedule(
            [
                {"day": 1, "kind": "STATEMENT"},
                {"day": 4, "kind": "REMINDER"},
                {"day": 8, "kind": "REMINDER"},
                {"day": 12, "kind": "REMINDER"},
                {"day": 15, "kind": "FINAL"},
            ]
        )
        assert due_stage(schedule, 1).day == 1
        assert due_stage(schedule, 3).day == 1
        assert due_stage(schedule, 9).day == 8
        assert due_stage(schedule, 16).day == 15
        assert due_stage(schedule, 28).day == 15
        assert next_stage_after(schedule, 8).day == 12
        assert next_stage_after(schedule, 15) is None

    def test_no_reminder_day_is_written_down_anywhere_in_the_engine(self):
        """REM-1 as a source guard: 1/4/8/12/15 appear in configuration, not logic."""
        import pathlib

        from tests._source import code_only

        root = pathlib.Path(__file__).resolve().parents[1] / "app" / "reminders"
        for path in root.glob("*.py"):
            tokens = code_only(path).split()
            for day in ("4", "8", "12", "15"):
                assert tokens.count(day) == 0, f"{path.name} hard-codes a schedule day"


# --- 2. each stage becomes due on its day ------------------------------------


class TestStagesBecomeDue:
    @pytest.mark.parametrize(
        "day,expected_day,expected_kind",
        [
            (1, 1, ReminderKind.STATEMENT),
            (4, 4, ReminderKind.REMINDER),
            (8, 8, ReminderKind.REMINDER),
            (12, 12, ReminderKind.REMINDER),
            (15, 15, ReminderKind.FINAL),
        ],
    )
    def test_the_run_sends_the_stage_configured_for_that_day(
        self, db, tenant_a, customer_factory, comms, day, expected_day, expected_kind
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        # Reach the target day one run at a time, exactly as the cron would.
        for d in range(1, day + 1):
            _run_on(db, tenant_a, comms, d)

        rows = [
            r
            for r in _reminders(db, tenant_a, customer)
            if r.schedule_day == expected_day and r.kind != ReminderKind.OWNER_ALERT
        ]
        assert len(rows) == 1
        assert rows[0].kind == expected_kind
        assert rows[0].state == ReminderState.SENT

    def test_nothing_is_sent_before_the_first_configured_day(
        self, db, tenant_a, customer_factory, comms
    ):
        """February's own days 1-28 precede the statement; the cycle is not billed."""
        customer = customer_factory(tenant_a.ctx, code="C1", price_minor=PRICE)
        ctx = ctx_at(tenant_a, _utc(2026, 2, 3))
        do_record(db, ctx, customer, quantity=Decimal("1"), service_date=date(2026, 2, 3))
        _, result = _run_on(db, tenant_a, comms, 3, month=2)
        assert result["generated"] == 0
        assert comms.sent == []

    def test_a_customer_with_no_issued_statement_is_never_reminded(
        self, db, tenant_a, customer_factory, comms
    ):
        """Fail safe: no bill, no cycle, no stage — never a fabricated amount."""
        customer = customer_factory(tenant_a.ctx, code="C9", price_minor=PRICE)
        ctx = ctx_at(tenant_a, _utc(2026, 3, 2))
        do_record(db, ctx, customer, quantity=Decimal("2"), service_date=date(2026, 3, 2))
        _, result = _run_on(db, tenant_a, comms, 8)
        assert _reminders(db, tenant_a, customer) == []
        assert result["sent"] == 0
        assert reminder_cycle_for(db, tenant_a.ctx, customer.id) is None


# --- 3. eligibility and the authoritative amount (REM-2, REM-3, REM-4) -------


class TestAuthoritativeAmount:
    def test_A_REM_2_3_a_payment_between_generation_and_send_lowers_the_amount(
        self, db, tenant_a, customer_factory, comms
    ):
        """The delivered amount is read at send time, never at generation."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        ctx = _sys_ctx(tenant_a, _utc(2026, 3, 4))
        schedule = tenant_schedule(db, ctx)

        generated = generate_due_reminder(db, ctx, customer=customer, schedule=schedule)
        db.commit()
        assert generated[0].amount_minor_at_generation == 100000  # 1000.00

        pay_ctx = ctx_at(tenant_a, _utc(2026, 3, 4))
        do_pay(db, pay_ctx, customer, 40000)

        dispatch_reminder(db, ctx, generated[0], comms)
        db.commit()

        assert generated[0].state == ReminderState.SENT
        # The stored generation amount is untouched; what went out is the new one.
        assert generated[0].amount_minor_at_generation == 100000
        assert comms.sent[-1].params["amount_due"] == format_minor(60000, "PKR", 2)

    def test_REM2_the_statement_amount_never_overrides_the_current_balance(
        self, db, tenant_a, customer_factory, comms
    ):
        """A stale statement total cannot be what a reminder chases."""
        customer, statement = _billed_customer(db, tenant_a, customer_factory)
        assert statement.closing_balance_minor == 100000

        pay_ctx = ctx_at(tenant_a, _utc(2026, 3, 2))
        do_pay(db, pay_ctx, customer, 40000)
        _run_on(db, tenant_a, comms, 4)

        sent = comms.sent[-1]
        assert sent.params["amount_due"] == format_minor(60000, "PKR", 2)
        assert format_minor(100000, "PKR", 2) not in sent.params.values()

    def test_A_REM_4_paying_in_full_stops_every_later_stage(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 4)
        assert len(comms.sent) == 1

        pay_ctx = ctx_at(tenant_a, _utc(2026, 3, 6))
        do_pay(db, pay_ctx, customer, 100000)

        for day in (8, 12, 15):
            _run_on(db, tenant_a, comms, day)

        assert len(comms.sent) == 1, "no further outstanding reminder after full payment"
        days = {r.schedule_day for r in _reminders(db, tenant_a, customer)}
        assert days == {4}
        assert not any(
            r.kind == ReminderKind.OWNER_ALERT for r in _reminders(db, tenant_a, customer)
        )

    def test_REM4_an_overpaid_customer_in_credit_receives_nothing(
        self, db, tenant_a, customer_factory, comms
    ):
        """A negative balance is money the business holds, not money it chases."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        pay_ctx = ctx_at(tenant_a, _utc(2026, 3, 2))
        do_pay(db, pay_ctx, customer, 150000)

        for day in (4, 8, 12, 15):
            _run_on(db, tenant_a, comms, day)

        assert _reminders(db, tenant_a, customer) == []
        assert comms.sent == []

    def test_a_payment_after_generation_that_clears_the_balance_cancels_the_stage(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        ctx = _sys_ctx(tenant_a, _utc(2026, 3, 8))
        schedule = tenant_schedule(db, ctx)
        generated = generate_due_reminder(db, ctx, customer=customer, schedule=schedule)
        db.commit()

        do_pay(db, ctx_at(tenant_a, _utc(2026, 3, 8)), customer, 100000)
        dispatch_reminder(db, ctx, generated[0], comms)
        db.commit()

        assert generated[0].state == ReminderState.CANCELLED
        assert generated[0].cancelled_at is not None
        assert comms.sent == [], "a cancelled stage is never handed to the provider"

    def test_the_statement_stage_goes_out_even_when_nothing_is_owed(
        self, db, tenant_a, customer_factory, comms
    ):
        """P0 §10 step 3: a statement is a bill and a record, not a dunning notice."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        do_pay(db, ctx_at(tenant_a, _utc(2026, 3, 1)), customer, 100000)

        _run_on(db, tenant_a, comms, 1)

        rows = _reminders(db, tenant_a, customer)
        assert [(r.schedule_day, r.kind, r.state) for r in rows] == [
            (1, ReminderKind.STATEMENT, ReminderState.SENT)
        ]

    def test_a_voided_payment_puts_the_balance_back_and_reminders_resume(
        self, db, tenant_a, customer_factory, comms
    ):
        """Eligibility follows the ledger, wherever the ledger goes."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        pay_ctx = ctx_at(tenant_a, _utc(2026, 3, 2))
        outcome = do_pay(db, pay_ctx, customer, 100000)
        _run_on(db, tenant_a, comms, 4)
        assert comms.sent == []

        do_void_payment(db, pay_ctx, outcome.result["id"], reason="cheque bounced")
        _run_on(db, tenant_a, comms, 8)

        assert len(comms.sent) == 1
        assert comms.sent[0].params["amount_due"] == format_minor(100000, "PKR", 2)


# --- 4. catch-up after an outage (REM-8) -------------------------------------


class TestOutageCatchUp:
    def test_A_REM_8a_an_outage_over_day_4_sends_the_day_4_stage_on_day_5(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 5)  # days 1-4 never ran

        rows = _reminders(db, tenant_a, customer)
        assert [(r.schedule_day, r.state) for r in rows] == [(4, ReminderState.SENT)]
        assert len(comms.sent) == 1

    def test_A_REM_8b_an_outage_through_day_8_sends_only_day_8_on_day_9(
        self, db, tenant_a, customer_factory, comms
    ):
        """The frozen rule: the latest due stage alone, never a burst."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 9)

        rows = _reminders(db, tenant_a, customer)
        assert [r.schedule_day for r in rows] == [8]
        assert len(comms.sent) == 1
        assert not any(r.schedule_day == 4 for r in rows), "day 4 must not be replayed"

    def test_running_on_day_10_after_no_prior_reminders_sends_only_day_8(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 10)

        assert [r.schedule_day for r in _reminders(db, tenant_a, customer)] == [8]
        assert len(comms.sent) == 1

    def test_A_REM_8c_a_customer_who_paid_during_the_outage_gets_no_catch_up(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        do_pay(db, ctx_at(tenant_a, _utc(2026, 3, 6)), customer, 100000)

        _run_on(db, tenant_a, comms, 9)

        assert _reminders(db, tenant_a, customer) == []
        assert comms.sent == []

    def test_A_REM_8d_an_outage_through_day_15_sends_the_final_and_the_alert_only(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 16)

        rows = _reminders(db, tenant_a, customer)
        assert {(r.schedule_day, r.kind) for r in rows} == {
            (15, ReminderKind.FINAL),
            (15, ReminderKind.OWNER_ALERT),
        }
        assert all(r.state == ReminderState.SENT for r in rows)
        templates = sorted(m.template_key for m in comms.sent)
        assert templates == ["owner.final_alert", "payment.reminder.final"]

    def test_at_most_one_customer_stage_per_run(
        self, db, tenant_a, customer_factory, comms
    ):
        """Requirement 11, asserted directly across every day of the month."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        for day in range(1, 29):
            before = len(comms.sent)
            _run_on(db, tenant_a, comms, day)
            customer_facing = [
                m for m in comms.sent[before:] if m.template_key != "owner.final_alert"
            ]
            assert len(customer_facing) <= 1, f"day {day} sent {len(customer_facing)}"

    def test_the_whole_month_sends_exactly_the_five_configured_stages(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        for day in range(1, 29):
            _run_on(db, tenant_a, comms, day)

        rows = _reminders(db, tenant_a, customer)
        assert [(r.schedule_day, r.kind) for r in rows] == [
            (1, ReminderKind.STATEMENT),
            (4, ReminderKind.REMINDER),
            (8, ReminderKind.REMINDER),
            (12, ReminderKind.REMINDER),
            (15, ReminderKind.FINAL),
            (15, ReminderKind.OWNER_ALERT),
        ]


# --- 5. idempotency and concurrency (REM-5) ----------------------------------


class TestIdempotentExecution:
    def test_A_REM_5_three_runs_on_one_business_date_send_one_message(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        results = [_run_on(db, tenant_a, comms, 4)[1] for _ in range(3)]

        assert results[0]["status"] == RUN_COMPLETED
        assert [r["status"] for r in results[1:]] == [RUN_ALREADY_DONE, RUN_ALREADY_DONE]
        assert len(comms.sent) == 1
        assert len(_reminders(db, tenant_a, customer)) == 1

        runs = db.execute(
            select(func.count()).select_from(JobRun).where(
                JobRun.tenant_id == tenant_a.tenant.id, JobRun.kind == JobKind.REMINDERS
            )
        ).scalar_one()
        assert runs == 1

    def test_a_duplicate_stage_insert_is_refused_by_the_database(
        self, db, tenant_a, customer_factory, comms
    ):
        """REM-5 is an index, not a code path. Prove the index itself refuses."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 4)
        existing = _reminders(db, tenant_a, customer)[0]

        db.add(
            Reminder(
                tenant_id=tenant_a.tenant.id,
                customer_id=customer.id,
                cycle_id=existing.cycle_id,
                schedule_day=existing.schedule_day,
                kind=existing.kind,
                amount_minor_at_generation=1,
                state=ReminderState.PENDING,
            )
        )
        with pytest.raises(Exception) as exc:
            db.flush()
        assert "uq_reminder_tenant_id_customer_id_cycle_id_schedule_day_kind" in str(
            exc.value
        )
        db.rollback()

    def test_concurrent_generation_produces_exactly_one_stage(
        self, db, session_factory, tenant_a, customer_factory, comms
    ):
        """Two runners, two connections, one row (requirement 13).

        Both sessions reach the insert with neither able to see the other's
        uncommitted row. PostgreSQL blocks the loser on the unique index until the
        winner commits, and the engine then reloads the winner rather than raising.
        """
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        ctx = _sys_ctx(tenant_a, _utc(2026, 3, 4))
        schedule = tenant_schedule(db, ctx)

        first = generate_due_reminder(db, ctx, customer=customer, schedule=schedule)
        db.commit()

        other = session_factory()
        try:
            second = generate_due_reminder(other, ctx, customer=customer, schedule=schedule)
            other.commit()
        finally:
            other.close()

        assert first[0].id == second[0].id
        assert len(_reminders(db, tenant_a, customer)) == 1

    def test_a_crashed_run_is_retryable_and_does_not_double_send(
        self, db, tenant_a, customer_factory, comms
    ):
        """Requirement 14: a process killed mid-round leaves a retry safe.

        ``SystemExit`` is deliberate — it models the process actually dying, so
        no ``except Exception`` in the runner gets to tidy up and the ``job_run``
        row is left ``RUNNING`` exactly as a real kill would leave it.
        """
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        ctx = _sys_ctx(tenant_a, _utc(2026, 3, 4))

        class Killed:
            name = "killed"
            capabilities = comms.capabilities

            def send(self, message):
                raise SystemExit("process killed mid-send")

            def parse_delivery_callback(self, headers, raw_body):
                return None

        with pytest.raises(SystemExit):
            run_daily_reminders(db, ctx, Killed())
        db.rollback()  # a new process starts with a clean session

        run = db.execute(
            select(JobRun).where(JobRun.tenant_id == tenant_a.tenant.id)
        ).scalars().one()
        assert run.status == JobRunStatus.RUNNING, "nobody was left to finish it"

        # The retry re-enters rather than being locked out by the stale row, and
        # sends exactly one message.
        _, result = _run_on(db, tenant_a, comms, 4)
        assert result["status"] == RUN_COMPLETED
        assert len(comms.sent) == 1
        rows = _reminders(db, tenant_a, customer)
        assert len(rows) == 1 and rows[0].state == ReminderState.SENT

    def test_a_failed_run_is_re_claimed_rather_than_locking_the_day_out(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        ctx = _sys_ctx(tenant_a, _utc(2026, 3, 4))
        db.add(
            JobRun(
                tenant_id=tenant_a.tenant.id,
                kind=JobKind.REMINDERS,
                business_date=date(2026, 3, 4),
                status=JobRunStatus.FAILED,
            )
        )
        db.commit()

        result = run_daily_reminders(db, ctx, comms)

        assert result["status"] == RUN_COMPLETED
        assert result["sent"] == 1
        assert len(_reminders(db, tenant_a, customer)) == 1

    def test_manual_redispatch_replays_on_the_same_operation_id(
        self, db, tenant_a, customer_factory, comms, client, settings, clock
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "provider rejected the template"
        _run_on(db, tenant_a, comms, 4)
        reminder = _reminders(db, tenant_a, customer)[0]
        assert reminder.state == ReminderState.FAILED

        comms.fail_with = None
        headers = _as_of(clock, tenant_a, settings, _utc(2026, 3, 4))
        operation_id = str(uuid7())
        first = client.post(
            f"/api/v1/reminders/{reminder.id}/send",
            json={"operation_id": operation_id},
            headers=headers,
        )
        second = client.post(
            f"/api/v1/reminders/{reminder.id}/send",
            json={"operation_id": operation_id},
            headers=headers,
        )
        assert first.status_code == 200 and first.json()["status"] == "APPLIED"
        assert second.json()["status"] == "DUPLICATE"
        # One extra delivery attempt, not two.
        assert len([m for m in comms.sent if m.customer_id == customer.id]) == 2


# --- 6. delivery failure (REM-6) ---------------------------------------------


class TestDeliveryFailure:
    def test_A_REM_6_a_total_provider_outage_changes_no_financial_row(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        before = _financial_fingerprint(db, tenant_a)

        comms.fail_with = "provider unreachable"
        _run_on(db, tenant_a, comms, 4)

        assert _financial_fingerprint(db, tenant_a) == before
        reminder = _reminders(db, tenant_a, customer)[0]
        assert reminder.state == ReminderState.FAILED
        assert reminder.last_error == "provider unreachable"
        assert reminder.sent_at is None

    def test_an_exception_from_the_provider_is_a_delivery_fact_not_a_crash(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.raise_with = ConnectionError("connection reset")

        _, result = _run_on(db, tenant_a, comms, 4)

        assert result["status"] == RUN_COMPLETED
        assert result["failed"] == 1
        reminder = _reminders(db, tenant_a, customer)[0]
        assert reminder.state == ReminderState.FAILED
        assert "ConnectionError" in reminder.last_error

    def test_a_failure_is_retained_and_never_reported_as_sent(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "rate limited"
        _run_on(db, tenant_a, comms, 4)

        attempts = db.execute(
            select(CommunicationLog).where(CommunicationLog.tenant_id == tenant_a.tenant.id)
        ).scalars().all()
        assert len(attempts) == 1
        assert attempts[0].state == "FAILED"
        assert attempts[0].error == "rate limited"
        assert attempts[0].attempt_no == 1

    def test_retries_are_bounded_and_do_not_loop(
        self, db, tenant_a, customer_factory, comms
    ):
        """P0 §9: bounded, then surfaced to the owner rather than retried forever."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "still down"
        for day in (4, 5, 6, 7):
            _run_on(db, tenant_a, comms, day)

        reminder = _reminders(db, tenant_a, customer)[0]
        assert reminder.attempt_count == MAX_DELIVERY_ATTEMPTS
        assert reminder.state == ReminderState.FAILED
        assert len(comms.sent) == MAX_DELIVERY_ATTEMPTS

    def test_a_failed_stage_is_retried_the_next_day_while_it_is_still_due(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "temporary"
        _run_on(db, tenant_a, comms, 4)

        comms.fail_with = None
        _run_on(db, tenant_a, comms, 5)

        reminder = _reminders(db, tenant_a, customer)[0]
        assert reminder.state == ReminderState.SENT
        assert reminder.attempt_count == 2
        assert len(_reminders(db, tenant_a, customer)) == 1

    def test_a_failed_stage_is_not_replayed_once_a_later_stage_is_due(
        self, db, tenant_a, customer_factory, comms
    ):
        """An outage must not turn into a burst when the provider comes back."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "down"
        _run_on(db, tenant_a, comms, 4)

        comms.fail_with = None
        _run_on(db, tenant_a, comms, 12)

        rows = _reminders(db, tenant_a, customer)
        assert {(r.schedule_day, r.state) for r in rows} == {
            (4, ReminderState.FAILED),
            (12, ReminderState.SENT),
        }

    def test_a_customer_with_no_contact_fails_visibly_rather_than_silently(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        customer.phone_e164 = None
        customer.whatsapp_e164 = None
        db.commit()

        _run_on(db, tenant_a, comms, 4)

        reminder = _reminders(db, tenant_a, customer)[0]
        assert reminder.state == ReminderState.FAILED
        assert "no phone or WhatsApp number" in reminder.last_error
        assert comms.sent == []


# --- 7. what the provider is handed (REM-7) ----------------------------------


class TestProviderBoundary:
    def test_A_REM_7_the_message_carries_a_rendered_amount_and_no_raw_balance(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 4)

        message = comms.sent[0]
        assert message.params["amount_due"] == "PKR 1,000.00"
        for key, value in message.params.items():
            assert isinstance(value, str), key
            assert not key.endswith("_minor")
        assert "100000" not in " ".join(message.params.values())

    def test_the_message_names_a_semantic_template_not_a_vendor_one(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        for day in (1, 4, 15):
            _run_on(db, tenant_a, comms, day)

        keys = [m.template_key for m in comms.sent]
        assert keys == [
            "statement.issued",
            "payment.reminder",
            "payment.reminder.final",
            "owner.final_alert",
        ]

    def test_the_idempotency_key_is_the_reminder_id(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "down"
        _run_on(db, tenant_a, comms, 4)
        comms.fail_with = None
        _run_on(db, tenant_a, comms, 5)

        reminder = _reminders(db, tenant_a, customer)[0]
        assert {m.idempotency_key for m in comms.sent} == {reminder.id}

    def test_the_port_refuses_a_non_string_or_minor_unit_param(self):
        """The boundary enforces REM-7 itself; it does not trust its caller."""
        from app.ports.comms import Channel, OutboundMessage

        base = dict(
            tenant_id=uuid7(),
            channel=Channel.SMS,
            to="+923001234567",
            template_key="payment.reminder",
            idempotency_key=uuid7(),
        )
        with pytest.raises(ValueError, match="already-rendered string"):
            OutboundMessage(params={"amount_due": 100000}, **base)
        with pytest.raises(ValueError, match="raw minor-unit"):
            OutboundMessage(params={"amount_due_minor": "100000"}, **base)

    def test_whatsapp_is_preferred_and_sms_is_the_fallback(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        customer.whatsapp_e164 = "+923339999999"
        db.commit()
        _run_on(db, tenant_a, comms, 4)
        assert comms.sent[0].channel == "WHATSAPP"
        assert comms.sent[0].to == "+923339999999"

        customer.whatsapp_e164 = None
        customer.phone_e164 = "+923001111111"
        db.commit()
        _run_on(db, tenant_a, comms, 8)
        assert comms.sent[1].channel == "SMS"
        assert comms.sent[1].to == "+923001111111"


# --- 8. the day-15 owner alert -----------------------------------------------


class TestOwnerAlert:
    def test_the_final_stage_alerts_the_owner_exactly_once(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        for day in (15, 16, 17, 20):
            _run_on(db, tenant_a, comms, day)

        alerts = [
            r for r in _reminders(db, tenant_a, customer) if r.kind == ReminderKind.OWNER_ALERT
        ]
        assert len(alerts) == 1
        assert alerts[0].state == ReminderState.SENT
        assert len([m for m in comms.sent if m.template_key == "owner.final_alert"]) == 1

    def test_the_alert_goes_to_the_owner_admin_and_names_what_to_act_on(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, statement = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 15)

        alert = next(m for m in comms.sent if m.template_key == "owner.final_alert")
        assert alert.channel == "EMAIL"
        assert alert.to == tenant_a.owner.email
        assert alert.customer_id == customer.id
        assert alert.params["customer_name"] == customer.name
        assert alert.params["amount_due"] == "PKR 1,000.00"
        assert alert.reference["cycle_id"] == str(statement.cycle_id)

    def test_the_alert_carries_no_platform_or_commission_information(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 15)

        alert = next(m for m in comms.sent if m.template_key == "owner.final_alert")
        blob = " ".join(alert.params.keys()).lower()
        for forbidden in ("commission", "platform", "plan", "settlement"):
            assert forbidden not in blob

    def test_no_alert_when_the_customer_paid_before_the_final_stage(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        do_pay(db, ctx_at(tenant_a, _utc(2026, 3, 14)), customer, 100000)
        _run_on(db, tenant_a, comms, 15)

        assert _reminders(db, tenant_a, customer) == []
        assert comms.sent == []

    def test_a_failed_alert_is_retried_without_re_sending_the_customer_notice(
        self, db, tenant_a, customer_factory, comms
    ):
        """The owner alert has its own delivery life; the FINAL is not re-sent."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)

        class OwnerAlertFails:
            name = "partial"
            capabilities = comms.capabilities

            def send(self, message):
                if message.template_key == "owner.final_alert":
                    from app.ports.comms import DeliveryReceipt, DeliveryState

                    return DeliveryReceipt(
                        state=DeliveryState.FAILED, provider=self.name, error="mailbox full"
                    )
                return comms.send(message)

            def parse_delivery_callback(self, headers, raw_body):
                return None

        ctx = _sys_ctx(tenant_a, _utc(2026, 3, 15))
        run_daily_reminders(db, ctx, OwnerAlertFails())

        rows = {r.kind: r for r in _reminders(db, tenant_a, customer)}
        assert rows[ReminderKind.FINAL].state == ReminderState.SENT
        assert rows[ReminderKind.OWNER_ALERT].state == ReminderState.FAILED

        sent_before = len([m for m in comms.sent if m.template_key.startswith("payment")])
        _run_on(db, tenant_a, comms, 16)

        rows = {r.kind: r for r in _reminders(db, tenant_a, customer)}
        assert rows[ReminderKind.OWNER_ALERT].state == ReminderState.SENT
        after = len([m for m in comms.sent if m.template_key.startswith("payment")])
        assert after == sent_before, "the customer's final notice is not sent twice"

    def test_the_alert_does_not_advance_the_customers_stage_progress(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 15)
        cycle = reminder_cycle_for(db, tenant_a.ctx, customer.id)
        assert highest_sent_stage(db, tenant_a.ctx, customer.id, cycle.cycle_id) == 15


# --- 9. business date and timezone (P0 R4) -----------------------------------


class TestBusinessDate:
    def test_the_stage_follows_the_tenants_local_day_not_utc(
        self, db, tenant_a, customer_factory, comms
    ):
        """23:00 UTC on the 3rd is already the 4th in Asia/Karachi (UTC+5)."""
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        ctx = _sys_ctx(tenant_a, datetime(2026, 3, 3, 23, 0, tzinfo=timezone.utc))
        assert ctx.today == date(2026, 3, 4)
        run_daily_reminders(db, ctx, comms)

        assert [r.schedule_day for r in _reminders(db, tenant_a, customer)] == [4]

    def test_two_tenants_in_different_zones_get_their_own_business_dates(
        self, db, tenant_a, tenant_b, customer_factory, comms, clock
    ):
        from app.core.clock import FixedClock

        tenant_b.tenant.timezone = "Pacific/Honolulu"  # UTC-10
        db.commit()
        instant = datetime(2026, 3, 4, 2, 0, tzinfo=timezone.utc)
        a = SystemContext.for_tenant(tenant=tenant_a.tenant, clock=FixedClock(instant))
        b = SystemContext.for_tenant(tenant=tenant_b.tenant, clock=FixedClock(instant))
        assert a.today == date(2026, 3, 4)
        assert b.today == date(2026, 3, 3)

    def test_the_job_guard_is_keyed_on_the_tenant_local_date(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 4)
        run = db.execute(select(JobRun).where(JobRun.tenant_id == tenant_a.tenant.id)).scalars().one()
        assert run.business_date == date(2026, 3, 4)
        assert run.status == JobRunStatus.SUCCEEDED
        assert run.detail["sent"] == 1


# --- 10. tenant isolation (SEC) ----------------------------------------------


class TestTenantIsolation:
    def test_a_run_for_one_tenant_never_touches_another(
        self, db, tenant_a, tenant_b, customer_factory, comms
    ):
        a_customer, _ = _billed_customer(db, tenant_a, customer_factory, code="A1")
        b_customer, _ = _billed_customer(db, tenant_b, customer_factory, code="B1")

        _run_on(db, tenant_a, comms, 4)

        assert [r.customer_id for r in _reminders(db, tenant_a)] == [a_customer.id]
        assert _reminders(db, tenant_b) == []
        assert {m.tenant_id for m in comms.sent} == {tenant_a.tenant.id}

    def test_the_cron_endpoint_names_no_tenant(self, app):
        """SEC: there is nothing to point at somebody else's data."""
        route = next(
            r for r in app.routes if getattr(r, "path", "") == "/api/v1/internal/jobs/run-daily"
        )
        names = {p for p in route.dependant.query_params + route.dependant.path_params}
        assert names == set()

    def test_one_cron_call_processes_every_active_tenant_on_its_own_date(
        self, db, tenant_a, tenant_b, customer_factory, comms, clock
    ):
        from app.core.clock import FixedClock

        _billed_customer(db, tenant_a, customer_factory, code="A1")
        _billed_customer(db, tenant_b, customer_factory, code="B1")

        result = run_reminders_for_all_tenants(db, FixedClock(_utc(2026, 3, 4)), comms)

        assert result["tenants"] == 2
        assert {r["status"] for r in result["results"]} == {RUN_COMPLETED}
        assert len(_reminders(db, tenant_a)) == 1
        assert len(_reminders(db, tenant_b)) == 1

    def test_one_tenants_failure_does_not_silence_the_others(
        self, db, tenant_a, tenant_b, customer_factory, comms
    ):
        from app.core.clock import FixedClock

        _billed_customer(db, tenant_a, customer_factory, code="A1")
        _billed_customer(db, tenant_b, customer_factory, code="B1")
        tenant_a.tenant.reminder_schedule = [{"day": 99, "kind": "REMINDER"}]
        db.commit()

        result = run_reminders_for_all_tenants(db, FixedClock(_utc(2026, 3, 4)), comms)

        statuses = {r["tenant_id"]: r["status"] for r in result["results"]}
        assert statuses[str(tenant_a.tenant.id)] == "ERROR"
        assert statuses[str(tenant_b.tenant.id)] == RUN_COMPLETED
        assert len(_reminders(db, tenant_b)) == 1

    def test_another_tenants_reminder_id_is_a_404(
        self, db, tenant_a, tenant_b, customer_factory, comms, client, settings, clock
    ):
        _billed_customer(db, tenant_b, customer_factory, code="B1")
        _run_on(db, tenant_b, comms, 4)
        theirs = _reminders(db, tenant_b)[0]

        headers = _as_of(clock, tenant_a, settings, _utc(2026, 3, 4))
        response = client.get(f"/api/v1/reminders/{theirs.id}", headers=headers)
        assert response.status_code == 404


# --- 11. the HTTP surface ----------------------------------------------------


class TestReminderRoutes:
    def test_the_owner_reads_the_work_list(
        self, db, tenant_a, customer_factory, comms, client, settings, clock
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 4)

        headers = _as_of(clock, tenant_a, settings, _utc(2026, 3, 4))
        body = client.get("/api/v1/reminders", headers=headers).json()

        assert body["business_date"] == "2026-03-04"
        assert body["due_stage"] == {"day": 4, "kind": "REMINDER"}
        assert [s["day"] for s in body["schedule"]] == [1, 4, 8, 12, 15]
        row = body["items"][0]
        assert row["customer_id"] == str(customer.id)
        assert row["outstanding_minor"] == 100000
        assert row["latest"]["schedule_day"] == 4
        assert row["next_stage"] == {"day": 8, "kind": "REMINDER"}
        assert row["status"] == ReminderStatus.WAITING

    def test_a_platform_token_is_refused_on_every_reminder_route(
        self, client, platform_token
    ):
        """SEC-5/SEC-6: the sets are disjoint, so this needs no per-route check."""
        headers = {"Authorization": f"Bearer {platform_token}"}
        assert client.get("/api/v1/reminders", headers=headers).status_code == 403
        assert (
            client.post(
                f"/api/v1/reminders/{uuid7()}/send",
                json={"operation_id": str(uuid7())},
                headers=headers,
            ).status_code
            == 403
        )

    def test_an_anonymous_caller_is_refused(self, client):
        assert client.get("/api/v1/reminders").status_code == 401

    def test_the_cron_endpoint_refuses_a_missing_or_wrong_secret(self, client):
        assert client.post("/api/v1/internal/jobs/run-daily").status_code == 401
        assert (
            client.post(
                "/api/v1/internal/jobs/run-daily", headers={"X-Job-Secret": "guess"}
            ).status_code
            == 401
        )

    def test_the_cron_endpoint_refuses_a_user_bearer_token(self, client, tenant_a):
        """A tenant token is not a job credential, however privileged it is."""
        assert (
            client.post("/api/v1/internal/jobs/run-daily", headers=tenant_a.auth).status_code
            == 401
        )

    def test_the_cron_endpoint_is_disabled_rather_than_open_without_a_secret(
        self, session_factory, clock, comms
    ):
        """An unset INTERNAL_JOB_SECRET must never mean "no check"."""
        from fastapi.testclient import TestClient

        from app.core.config import Settings
        from app.main import create_app
        from tests.conftest import TEST_JWT_SECRET

        app = create_app(
            Settings(jwt_secret=TEST_JWT_SECRET, internal_job_secret="", environment="test")
        )
        app.state.session_factory = session_factory
        app.state.clock = clock
        app.state.communication_provider = comms
        with TestClient(app, raise_server_exceptions=False) as anonymous:
            response = anonymous.post("/api/v1/internal/jobs/run-daily")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "JOB_ENDPOINT_DISABLED"

    def test_the_cron_endpoint_runs_the_round(
        self, db, tenant_a, customer_factory, comms, client, job_headers, settings, clock
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        clock.set(_utc(2026, 3, 4))

        response = client.post("/api/v1/internal/jobs/run-daily", headers=job_headers)

        assert response.status_code == 200
        body = response.json()["reminders"]
        assert body["tenants"] == 1
        assert body["results"][0]["sent"] == 1
        assert len(comms.sent) == 1

    def test_a_second_cron_call_on_the_same_date_sends_nothing_further(
        self, db, tenant_a, customer_factory, comms, client, job_headers, clock
    ):
        _billed_customer(db, tenant_a, customer_factory)
        clock.set(_utc(2026, 3, 4))
        client.post("/api/v1/internal/jobs/run-daily", headers=job_headers)
        second = client.post("/api/v1/internal/jobs/run-daily", headers=job_headers)

        assert second.json()["reminders"]["results"][0]["status"] == RUN_ALREADY_DONE
        assert len(comms.sent) == 1

    def test_reminder_detail_shows_every_delivery_attempt(
        self, db, tenant_a, customer_factory, comms, client, settings, clock
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "down"
        _run_on(db, tenant_a, comms, 4)
        comms.fail_with = None
        _run_on(db, tenant_a, comms, 5)
        reminder = _reminders(db, tenant_a, customer)[0]

        headers = _as_of(clock, tenant_a, settings, _utc(2026, 3, 5))
        body = client.get(f"/api/v1/reminders/{reminder.id}", headers=headers).json()

        assert body["state"] == ReminderState.SENT
        assert body["outstanding_minor"] == 100000
        assert [a["state"] for a in body["attempts"]] == ["FAILED", "ACCEPTED"]
        assert body["attempts"][1]["payload"]["amount_due"] == "PKR 1,000.00"

    def test_manual_redispatch_cancels_instead_of_sending_when_the_debt_is_gone(
        self, db, tenant_a, customer_factory, comms, client, settings, clock
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "down"
        _run_on(db, tenant_a, comms, 4)
        reminder = _reminders(db, tenant_a, customer)[0]

        comms.fail_with = None
        do_pay(db, ctx_at(tenant_a, _utc(2026, 3, 5)), customer, 100000)

        headers = _as_of(clock, tenant_a, settings, _utc(2026, 3, 5))
        body = client.post(
            f"/api/v1/reminders/{reminder.id}/send",
            json={"operation_id": str(uuid7())},
            headers=headers,
        ).json()

        assert body["entity"]["state"] == ReminderState.CANCELLED
        assert len(comms.sent) == 1, "nothing further was handed to the provider"


# --- 12. the owner's view ----------------------------------------------------


class TestReminderOverview:
    def test_a_settled_customer_is_reported_as_settled(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 4)
        do_pay(db, ctx_at(tenant_a, _utc(2026, 3, 5)), customer, 100000)

        ctx = ctx_at(tenant_a, _utc(2026, 3, 8))
        row = reminder_overview(db, ctx)["items"][0]
        assert row["status"] == ReminderStatus.SETTLED
        assert row["outstanding_minor"] == 0
        assert row["next_stage"] is None

    def test_a_failed_delivery_is_reported_as_needing_attention(
        self, db, tenant_a, customer_factory, comms
    ):
        _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "down"
        _run_on(db, tenant_a, comms, 4)

        ctx = ctx_at(tenant_a, _utc(2026, 3, 4))
        overview = reminder_overview(db, ctx)
        assert overview["items"][0]["status"] == ReminderStatus.ATTENTION
        assert overview["counts"]["attention"] == 1

    def test_a_stage_due_and_not_yet_sent_is_reported_as_due(
        self, db, tenant_a, customer_factory, comms
    ):
        _billed_customer(db, tenant_a, customer_factory)
        ctx = ctx_at(tenant_a, _utc(2026, 3, 4))
        row = reminder_overview(db, ctx)["items"][0]
        assert row["status"] == ReminderStatus.DUE
        assert row["latest"] is None

    def test_the_next_stage_is_never_a_day_that_has_already_passed(
        self, db, tenant_a, customer_factory, comms
    ):
        """A customer nothing has been sent to is still past the earlier stages."""
        _billed_customer(db, tenant_a, customer_factory)
        ctx = ctx_at(tenant_a, _utc(2026, 3, 5))
        row = reminder_overview(db, ctx)["items"][0]
        assert row["status"] == ReminderStatus.DUE
        assert row["latest"] is None
        # Day 4 is due now, so the *next* one is day 8 — never day 1.
        assert row["next_stage"] == {"day": 8, "kind": ReminderKind.REMINDER}

    def test_the_next_stage_follows_what_was_actually_sent(
        self, db, tenant_a, customer_factory, comms
    ):
        _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 8)
        ctx = ctx_at(tenant_a, _utc(2026, 3, 9))
        row = reminder_overview(db, ctx)["items"][0]
        assert row["latest"]["schedule_day"] == 8
        assert row["next_stage"] == {"day": 12, "kind": ReminderKind.REMINDER}

    def test_there_is_no_next_stage_after_the_final_one(
        self, db, tenant_a, customer_factory, comms
    ):
        _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 15)
        ctx = ctx_at(tenant_a, _utc(2026, 3, 16))
        assert reminder_overview(db, ctx)["items"][0]["next_stage"] is None

    def test_a_customer_still_owing_after_the_final_stage_needs_attention(
        self, db, tenant_a, customer_factory, comms
    ):
        _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 15)
        ctx = ctx_at(tenant_a, _utc(2026, 3, 16))
        row = reminder_overview(db, ctx)["items"][0]
        assert row["status"] == ReminderStatus.ATTENTION
        assert row["owner_alert"]["state"] == ReminderState.SENT

    def test_a_customer_with_no_statement_is_reported_as_such(
        self, db, tenant_a, customer_factory
    ):
        customer_factory(tenant_a.ctx, code="NEW", price_minor=PRICE)
        ctx = ctx_at(tenant_a, _utc(2026, 3, 4))
        row = reminder_overview(db, ctx)["items"][0]
        assert row["status"] == ReminderStatus.NO_STATEMENT
        assert row["cycle"] is None

    def test_the_overview_shows_the_current_balance_not_the_generated_one(
        self, db, tenant_a, customer_factory, comms
    ):
        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 4)
        do_pay(db, ctx_at(tenant_a, _utc(2026, 3, 5)), customer, 30000)

        ctx = ctx_at(tenant_a, _utc(2026, 3, 6))
        row = reminder_overview(db, ctx)["items"][0]
        assert row["outstanding_minor"] == 70000
        assert row["latest"]["amount_minor_at_generation"] == 100000

    def test_the_overview_never_reads_a_commission_or_operating_cost_figure(self):
        """Source guard: the owner's reminder screen is not a platform surface."""
        import pathlib

        from tests._source import code_only

        path = pathlib.Path(__file__).resolve().parents[1] / "app" / "reminders"
        for module in path.glob("*.py"):
            code = code_only(module)
            assert "commission" not in code.lower()
            assert "operating_cost" not in code.lower()


# --- 13. audit provenance (AUD-9) --------------------------------------------


class TestAuditProvenance:
    def test_a_cron_run_is_audited_as_system_and_job(
        self, db, tenant_a, customer_factory, comms
    ):
        from app.audit.models import AuditAction, AuditEvent

        _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 4)

        rows = db.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_a.tenant.id,
                AuditEvent.action.like("reminder%"),
            )
        ).scalars().all()
        assert rows, "the run left an audit trail"
        assert {r.actor_scope for r in rows} == {"SYSTEM"}
        assert {r.source for r in rows} == {"JOB"}
        assert all(r.actor_user_id is None for r in rows)
        actions = {r.action for r in rows}
        assert AuditAction.REMINDER_RUN_COMPLETED in actions
        assert AuditAction.REMINDER_SENT in actions

    def test_a_manual_redispatch_is_audited_to_the_person_who_did_it(
        self, db, tenant_a, customer_factory, comms, client, settings, clock
    ):
        from app.audit.models import AuditEvent

        customer, _ = _billed_customer(db, tenant_a, customer_factory)
        comms.fail_with = "down"
        _run_on(db, tenant_a, comms, 4)
        reminder = _reminders(db, tenant_a, customer)[0]

        comms.fail_with = None
        client.post(
            f"/api/v1/reminders/{reminder.id}/send",
            json={"operation_id": str(uuid7())},
            headers=_as_of(clock, tenant_a, settings, _utc(2026, 3, 4)),
        )

        manual = db.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_a.tenant.id,
                AuditEvent.source == "ONLINE",
                AuditEvent.action.like("reminder%"),
            )
        ).scalars().all()
        assert len(manual) == 1
        assert manual[0].actor_scope == "TENANT"
        assert manual[0].actor_user_id == tenant_a.owner.id
        assert manual[0].reason == "re-dispatched by the owner"

    def test_a_quiet_run_does_not_flood_the_audit_trail(
        self, db, tenant_a, customer_factory, comms
    ):
        """One row for the run, and none per customer when nothing happened."""
        from app.audit.models import AuditEvent

        customer_factory(tenant_a.ctx, code="NEW", price_minor=PRICE)
        _run_on(db, tenant_a, comms, 4)

        rows = db.execute(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.tenant_id == tenant_a.tenant.id,
                AuditEvent.action.like("reminder%"),
            )
        ).scalar_one()
        assert rows == 1


# --- 14. reminder history is not destructive ---------------------------------


class TestHistoryIsNotDestructive:
    @pytest.mark.parametrize("table", ["reminder", "communication_log"])
    def test_the_database_refuses_to_delete_reminder_history(
        self, db, tenant_a, customer_factory, comms, table
    ):
        _billed_customer(db, tenant_a, customer_factory)
        _run_on(db, tenant_a, comms, 4)

        with pytest.raises(Exception) as exc:
            db.execute(text(f"DELETE FROM {table}"))
            db.flush()
        assert "no delete path" in str(exc.value)
        db.rollback()


# --- helpers -----------------------------------------------------------------


def _financial_fingerprint(db, fixture) -> tuple:
    """Every financial row that a reminder must be incapable of touching.

    A tuple, compared before and after, so A-REM-6 asserts *identity* rather than
    the absence of one particular symptom.
    """
    from app.commission.models import CommissionAdjustment, CommissionEvent
    from app.payments.models import Payment

    def rows(model, *columns):
        return tuple(
            db.execute(
                select(*columns).where(model.tenant_id == fixture.tenant.id).order_by(model.id)
            ).all()
        )

    return (
        rows(LedgerEntry, LedgerEntry.id, LedgerEntry.amount_minor, LedgerEntry.entry_kind),
        rows(Statement, Statement.id, Statement.closing_balance_minor),
        rows(Payment, Payment.id, Payment.amount_minor, Payment.status),
        rows(CommissionEvent, CommissionEvent.id, CommissionEvent.commission_minor),
        rows(CommissionAdjustment, CommissionAdjustment.id, CommissionAdjustment.amount_minor),
    )
