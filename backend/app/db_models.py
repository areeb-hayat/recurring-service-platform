"""Single import point so Alembic and tests see every table on ``Base.metadata``.

P1 tables only. Adding a model here is a deliberate act — future packages append
their own (billing_cycle, statement, payment, reminder, commission_*, job_run).
"""

from __future__ import annotations

from app.core.db import Base

__all__ = ["Base", "import_all_models", "P1_TABLES"]

# The exact set P1 creates. The schema-assertion test compares the live database
# against this, so a stray future table cannot appear unnoticed.
P1_TABLES = frozenset(
    {
        "tenant",
        "app_user",
        "user_session",
        "customer",
        "daily_service_record",
        "ledger_entry",
        "audit_event",
        "sync_operation",
    }
)


def import_all_models() -> None:
    from app.audit import models as _audit  # noqa: F401
    from app.billing import models as _billing  # noqa: F401
    from app.customers import models as _customers  # noqa: F401
    from app.identity import models as _identity  # noqa: F401
    from app.service import models as _service  # noqa: F401
    from app.sync import models as _sync  # noqa: F401
    from app.tenancy import models as _tenancy  # noqa: F401


import_all_models()
