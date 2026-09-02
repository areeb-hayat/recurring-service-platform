"""SyncOperation — the permanent server-side idempotency register.

SYN-2 / SYN-3 / SYN-13. ``(tenant_id, operation_id)`` is unique, and the register
row is written **in the same transaction** as the effect it records, so "effect
without register" and "register without effect" are both impossible.

**Never pruned.** A retention horizon would silently become a duplication
horizon: an operation retried after the cut-off would be accepted a second time.
There is deliberately no TTL, no archival job and no cleanup task anywhere in
this codebase.

P1 registers ``APPLIED`` operations only — those that committed an effect worth
replaying. ``REJECTED`` and ``CONFLICT`` are returned to the caller but not
persisted, so a transient validation failure can never permanently poison an
``operation_id``. The status column carries the full P0 vocabulary so P5 can
extend this without a migration.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, uuid_fk, uuid_nullable, uuid_pk, utc_timestamp
from app.core.ids import new_id

__all__ = ["SyncOperation", "OperationStatus"]


class OperationStatus:
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"  # a response status, never a stored row
    REJECTED = "REJECTED"
    CONFLICT = "CONFLICT"


class SyncOperation(Base):
    __tablename__ = "sync_operation"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    tenant_id: Mapped[uuid_fk] = mapped_column(ForeignKey("tenant.id"))
    operation_id: Mapped[uuid_fk]
    user_id: Mapped[uuid_nullable] = mapped_column(ForeignKey("app_user.id"))

    op_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # SYN-14: binds an operation_id to the request that created it.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # The logical result replayed on DUPLICATE. JSONB, so semantic equality is
    # the contract — not byte-identical serialization (SYN-2).
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[uuid_nullable]
    received_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "operation_id", name="uq_sync_operation_tenant_id_operation_id"
        ),
        Index("ix_sync_operation_tenant_id_received_at", "tenant_id", "received_at"),
        CheckConstraint(
            "status IN ('APPLIED','REJECTED','CONFLICT')", name="status_valid"
        ),
    )
