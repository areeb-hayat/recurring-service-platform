"""Daily job entrypoints (P0 §2.1 ``jobs/``, §12).

Thin on purpose. A job is an *entrypoint*, not a place for business logic: the
reminder decision lives in :mod:`app.reminders.engine`, the run's guard and loop
live in :mod:`app.reminders.runner`, and this module only names which of them the
cron drives. That is what lets the same round be exercised by a test calling the
runner directly, without an HTTP client and without a scheduler.

It imports no adapter (A-SLOT-5): the communication provider is handed in by the
API layer, which is the only place that knows which implementation is configured.

One job exists. Statement issue is not a job — a statement is issued when its
cycle closes, deliberately, by a person — and there is no reminder-cleanup,
retention or archival job, because ``sync_operation`` is never pruned and
accepted reminder history has no delete path.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.ports.comms import CommunicationProvider
from app.reminders.runner import run_reminders_for_all_tenants

__all__ = ["run_daily"]


def run_daily(
    session: Session, clock: Clock, provider: CommunicationProvider
) -> dict[str, Any]:
    """What ``POST /internal/jobs/run-daily`` executes.

    Every active tenant, each resolving its own tenant-local business date. The
    caller chooses nothing: no tenant, no date, no stage.
    """
    return {"reminders": run_reminders_for_all_tenants(session, clock, provider)}
