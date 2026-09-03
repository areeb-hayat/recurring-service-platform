"""AuditEvent — append-only trail for financially meaningful mutations.

AUD-7: append-only. AUD-9: every row records whether it originated ONLINE, via
SYNC, from a JOB, or from the PLATFORM scope.

``before``/``after`` hold small JSON snapshots of the changed business fields.
Secrets never enter them — see ``app/audit/service.py``, which filters by an
explicit allow-list rather than trying to blacklist sensitive keys.

Module placement: P0 §2.1 does not list an ``audit/`` module because audit is
cross-cutting and belongs to no single domain. It is given its own small module
rather than being buried in ``core/`` (which P0 scopes to primitives) or
arbitrarily attached to one domain.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, uuid_nullable, uuid_pk, utc_timestamp
from app.core.ids import new_id

__all__ = ["AuditEvent", "AuditAction", "AuditSource", "ActorScope"]


class AuditAction:
    CUSTOMER_CREATED = "customer.created"
    CUSTOMER_UPDATED = "customer.updated"
    # P8. Aliases are customer identity, not money, so they are named apart from
    # everything financial. Every one of them carries the alias text before and
    # after, which is where the history of a corrected nickname lives.
    CUSTOMER_ALIAS_ADDED = "customer_alias.added"
    CUSTOMER_ALIAS_UPDATED = "customer_alias.updated"
    CUSTOMER_ALIAS_DEACTIVATED = "customer_alias.deactivated"
    CUSTOMER_ALIAS_REACTIVATED = "customer_alias.reactivated"
    SERVICE_RECORDED = "service.recorded"
    SERVICE_SKIPPED = "service.skipped"
    SERVICE_CORRECTED = "service.corrected"
    SERVICE_VOIDED = "service.voided"
    PAYMENT_RECORDED = "payment.recorded"
    PAYMENT_VOIDED = "payment.voided"
    BILLING_CYCLE_CLOSED = "billing_cycle.closed"
    COMMISSION_PLAN_CREATED = "commission_plan.created"
    COMMISSION_PLAN_CLOSED = "commission_plan.closed"
    COMMISSION_SETTLEMENT_RECORDED = "commission_settlement.recorded"
    # P6 operating costs. Tenant-scope actions on what the business pays its
    # providers -- deliberately named apart from the commission actions above,
    # because the two are different accounting concepts and an audit reader
    # must never have to guess which one a row describes.
    OPERATING_COST_ITEM_CREATED = "operating_cost_item.created"
    OPERATING_COST_RATE_CREATED = "operating_cost_rate.created"
    OPERATING_COST_RATE_CLOSED = "operating_cost_rate.closed"
    OPERATING_COST_USAGE_RECORDED = "operating_cost_usage.recorded"
    OPERATING_COST_USAGE_CORRECTED = "operating_cost_usage.corrected"
    OPERATING_COST_ACTUAL_RECORDED = "operating_cost_actual.recorded"
    OPERATING_COST_ACTUAL_CORRECTED = "operating_cost_actual.corrected"
    # P7 reminders. A reminder is a communication, not a financial mutation, so
    # these actions are deliberately named apart from anything in the ledger.
    # Only meaningful events are recorded: a run that produced nothing writes one
    # row for the run and no per-customer noise.
    REMINDER_RUN_COMPLETED = "reminder_run.completed"
    REMINDER_GENERATED = "reminder.generated"
    REMINDER_SENT = "reminder.sent"
    REMINDER_FAILED = "reminder.failed"
    REMINDER_CANCELLED = "reminder.cancelled"
    REMINDER_OWNER_ALERTED = "reminder.owner_alerted"
    REMINDER_REDISPATCHED = "reminder.redispatched"
    AUTH_LOGIN_SUCCEEDED = "auth.login_succeeded"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_TOKEN_REFRESHED = "auth.token_refreshed"
    AUTH_LOGGED_OUT = "auth.logged_out"


class AuditSource:
    ONLINE = "ONLINE"
    SYNC = "SYNC"
    JOB = "JOB"
    PLATFORM = "PLATFORM"


class ActorScope:
    TENANT = "TENANT"
    PLATFORM = "PLATFORM"
    SYSTEM = "SYSTEM"


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[uuid_pk] = mapped_column(default=new_id)
    # Nullable: platform-scope actions are not tenant-owned (P0 §6).
    tenant_id: Mapped[uuid_nullable] = mapped_column(ForeignKey("tenant.id"))
    actor_user_id: Mapped[uuid_nullable] = mapped_column(ForeignKey("app_user.id"))
    actor_scope: Mapped[str] = mapped_column(String(16), nullable=False)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid_nullable]

    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation_id: Mapped[uuid_nullable]
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ONLINE")
    occurred_at: Mapped[utc_timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index("ix_audit_event_tenant_id_occurred_at", "tenant_id", "occurred_at"),
        Index(
            "ix_audit_event_tenant_id_entity_type_entity_id",
            "tenant_id",
            "entity_type",
            "entity_id",
        ),
        CheckConstraint(
            "actor_scope IN ('TENANT','PLATFORM','SYSTEM')", name="actor_scope_valid"
        ),
        CheckConstraint(
            "source IN ('ONLINE','SYNC','JOB','PLATFORM')", name="source_valid"
        ),
    )
