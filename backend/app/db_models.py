"""Single import point so Alembic and tests see every table on ``Base.metadata``.

Adding a model here is a deliberate act. The per-package sets below are what the
schema-assertion test compares the live database against, so a stray future table
cannot appear unnoticed — reminder, communication_log, commission_* and job_run
still belong to later packages and must stay absent.
"""

from __future__ import annotations

from app.core.db import Base

__all__ = ["Base", "import_all_models", "P1_TABLES", "P2_TABLES", "ALL_TABLES"]

# The exact set P1 creates.
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

# The exact set P2 adds (P0 §5.5, §6).
P2_TABLES = frozenset({"billing_cycle", "statement", "payment"})

ALL_TABLES = P1_TABLES | P2_TABLES


def import_all_models() -> None:
    from app.audit import models as _audit  # noqa: F401
    from app.billing import models as _billing  # noqa: F401
    from app.customers import models as _customers  # noqa: F401
    from app.identity import models as _identity  # noqa: F401
    from app.payments import models as _payments  # noqa: F401
    from app.service import models as _service  # noqa: F401
    from app.sync import models as _sync  # noqa: F401
    from app.tenancy import models as _tenancy  # noqa: F401


import_all_models()
