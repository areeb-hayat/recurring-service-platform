"""The scheduled reminder run: the ``job_run`` guard, and the loop (P0 §10, §12).

**One process, one HTTP call, no broker.** The frozen stack has no Celery, no
Redis and no queue worker, and this needs none: the host's cron calls one
authenticated endpoint, which calls :func:`run_daily_reminders` once per active
tenant. Everything that would otherwise need a scheduler — the same-day guard,
the catch-up rule, the retry bound — is a database row or a pure function.

**Two independent guarantees, and it matters which is which.**

* ``job_run (tenant_id, kind, business_date)`` is a *short-circuit*. A cron that
  fires twice on one business date does the work once (A-REM-5). It is cheap and
  it is not load-bearing.
* The ``reminder`` stage index is the *correctness guarantee*. Even if two
  runners somehow proceed at once — a stale RUNNING row, two hosts, a retry
  overlapping a slow run — PostgreSQL admits exactly one row per
  ``(tenant, customer, cycle, stage, kind)``, so at most one message per stage
  exists no matter how the processes interleave.

**Commit granularity is per customer.** A crash halfway through a round leaves
the customers already processed durably processed and the rest untouched; the
next run resumes rather than restarting, and could not double-send even if it
did. That is requirement "failure before commit → retry safe", made structural.

**The tenant is never a caller's choice.** There is no tenant parameter on the
job route and none on :func:`run_reminders_for_all_tenants`. The runner reads the
active tenants itself and builds one
:class:`~app.tenancy.context.SystemContext` per tenant, each resolving *its own*
business date from *its own* timezone (P0 R4). A cron cannot be pointed at
somebody else's data because there is nowhere to point it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditAction
from app.audit.service import record_system_event, snapshot
from app.core.clock import Clock
from app.customers.models import Customer
from app.ports.comms import CommunicationProvider
from app.reminders.engine import process_customer, tenant_schedule
from app.reminders.models import (
    JobKind,
    JobRun,
    JobRunStatus,
    JobTrigger,
    ReminderKind,
    ReminderState,
)
from app.reminders.schedule import due_stage, serialize_schedule
from app.tenancy.context import SystemContext
from app.tenancy.models import Tenant

__all__ = [
    "RUN_ALREADY_DONE",
    "RUN_COMPLETED",
    "run_daily_reminders",
    "run_reminders_for_all_tenants",
]

RUN_COMPLETED = "COMPLETED"
RUN_ALREADY_DONE = "ALREADY_RUN"

_JOB_RUN_UNIQUE = "uq_job_run_tenant_id_kind_business_date"


def _claim_run(session: Session, ctx, *, triggered_by: str) -> tuple[JobRun | None, str]:
    """Claim today's run for this tenant, or report why we are not running it.

    Committed on its own before any work starts, so a concurrent invocation sees
    the claim immediately rather than blocking behind an entire round.

    Only a ``SUCCEEDED`` row short-circuits. A ``FAILED`` or still-``RUNNING`` one
    is re-claimed, and both cases are deliberate:

    * A crashed run must be retryable. A process killed mid-round leaves its row
      ``RUNNING`` forever, and refusing to re-enter would silence that tenant's
      reminders for the rest of the day over a row nobody will ever finish.
    * Re-entering is safe *because the guard is not the guarantee*. The stage
      unique index admits one row per (tenant, customer, cycle, stage, kind)
      however the processes interleave, and a stage already ``SENT`` is skipped
      by :func:`~app.reminders.engine.dispatch_reminder`. So the worst outcome of
      two overlapping runners is wasted work, never a second message.

    Which is why there is no lease, no heartbeat and no stale-run sweeper here:
    they would be machinery protecting something the index already protects.
    """
    run = JobRun(
        tenant_id=ctx.tenant_id,
        kind=JobKind.REMINDERS,
        business_date=ctx.today,
        status=JobRunStatus.RUNNING,
        triggered_by=triggered_by,
        started_at=ctx.now,
    )
    session.add(run)
    try:
        session.flush()
    except IntegrityError as exc:
        if _JOB_RUN_UNIQUE not in str(getattr(exc, "orig", exc)):
            raise
        session.rollback()
        existing = session.execute(
            select(JobRun).where(
                JobRun.tenant_id == ctx.tenant_id,
                JobRun.kind == JobKind.REMINDERS,
                JobRun.business_date == ctx.today,
            )
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - only if the winner rolled back
            raise
        if existing.status == JobRunStatus.SUCCEEDED:
            return None, RUN_ALREADY_DONE
        existing.status = JobRunStatus.RUNNING
        existing.triggered_by = triggered_by
        existing.started_at = ctx.now
        existing.finished_at = None
        session.commit()
        return existing, RUN_COMPLETED
    session.commit()
    return run, RUN_COMPLETED


def _active_customers(session: Session, ctx) -> list[Customer]:
    return list(
        session.execute(
            select(Customer)
            .where(Customer.tenant_id == ctx.tenant_id, Customer.status == "ACTIVE")
            .order_by(Customer.code, Customer.id)
        )
        .scalars()
        .all()
    )


def run_daily_reminders(
    session: Session,
    ctx: SystemContext,
    provider: CommunicationProvider,
    *,
    triggered_by: str = JobTrigger.CRON,
) -> dict[str, Any]:
    """One tenant's reminder round for its own business date.

    Returns a summary rather than raising on a delivery problem: a provider
    outage is an outcome to report, not an error to propagate. A genuine
    programming failure does propagate, after the run is marked ``FAILED`` so a
    later invocation may retry it.
    """
    schedule = tenant_schedule(session, ctx)
    stage = due_stage(schedule, ctx.today.day)

    run, outcome = _claim_run(session, ctx, triggered_by=triggered_by)
    if run is None:
        return {
            "status": outcome,
            "tenant_id": str(ctx.tenant_id),
            "business_date": ctx.today.isoformat(),
            "due_stage": stage.as_dict() if stage else None,
            "generated": 0,
            "sent": 0,
            "failed": 0,
            "cancelled": 0,
            "owner_alerts": 0,
        }

    counts = {"generated": 0, "sent": 0, "failed": 0, "cancelled": 0, "owner_alerts": 0}

    try:
        if stage is not None:
            for customer in _active_customers(session, ctx):
                reminders = process_customer(
                    session, ctx, provider, customer=customer, schedule=schedule
                )
                for reminder in reminders:
                    counts["generated"] += 1
                    if reminder.kind == ReminderKind.OWNER_ALERT:
                        counts["owner_alerts"] += 1
                    if reminder.state == ReminderState.SENT:
                        counts["sent"] += 1
                    elif reminder.state == ReminderState.FAILED:
                        counts["failed"] += 1
                    elif reminder.state == ReminderState.CANCELLED:
                        counts["cancelled"] += 1
                # One customer, one commit: a crash costs the rest of the round,
                # never the part already done.
                session.commit()
    except Exception:
        session.rollback()
        run.status = JobRunStatus.FAILED
        run.finished_at = ctx.now
        run.detail = dict(counts)
        session.commit()
        raise

    run.status = JobRunStatus.SUCCEEDED
    run.finished_at = ctx.now
    run.detail = dict(counts)
    # AUD-9: one audit row per run, with SYSTEM/JOB provenance. Per-customer
    # events are already recorded by the engine; a second row per customer here
    # would be the polling noise this trail exists to stay free of.
    record_system_event(
        session,
        ctx,
        action=AuditAction.REMINDER_RUN_COMPLETED,
        entity_type="job_run",
        entity_id=run.id,
        after=snapshot("job_run", run),
    )
    session.commit()

    return {
        "status": RUN_COMPLETED,
        "tenant_id": str(ctx.tenant_id),
        "business_date": ctx.today.isoformat(),
        "due_stage": stage.as_dict() if stage else None,
        "schedule": serialize_schedule(schedule),
        **counts,
    }


def run_reminders_for_all_tenants(
    session: Session, clock: Clock, provider: CommunicationProvider
) -> dict[str, Any]:
    """Every active tenant, each on its own business date.

    The cron's entire surface. It names no tenant and accepts no date; a tenant
    in Karachi and one in Dubai driven by the same 02:00 UTC trigger each get
    their own local day, and a run for one can never touch the other's data
    because each gets its own :class:`SystemContext`.

    A tenant that raises does not stop the round: its failure is reported and the
    next tenant is processed, because one misconfigured tenant must not silence
    everybody else's reminders.
    """
    tenants = list(
        session.execute(
            select(Tenant).where(Tenant.status == "ACTIVE").order_by(Tenant.slug)
        )
        .scalars()
        .all()
    )

    results: list[dict[str, Any]] = []
    for tenant in tenants:
        ctx = SystemContext.for_tenant(tenant=tenant, clock=clock)
        try:
            results.append(run_daily_reminders(session, ctx, provider))
        except Exception as exc:
            session.rollback()
            results.append(
                {
                    "status": "ERROR",
                    "tenant_id": str(tenant.id),
                    "business_date": ctx.today.isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {"tenants": len(results), "results": results}
