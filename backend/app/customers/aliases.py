"""Alias commands — the names a customer is actually called.

Three writes and two reads. Every write is audited with the text before and
after, bumps the owning customer's ``row_version`` so the change feed carries the
new alias set, and leaves the row in place: an alias that is retired goes
``INACTIVE``, and there is no delete path (the database refuses one).

**Why a write here touches ``customer.row_version``.** An alias is not a sync
entity of its own — it travels inside the customer's payload. If an alias write
did not advance the customer's version, an offline device would keep the alias
set it happened to have when the customer last changed, and the operator would
search for a nickname the server knows and this device does not. Bumping the
parent is the smallest thing that keeps the feed and the snapshot honest.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from sqlalchemy import func as sql_func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditAction
from app.audit.service import record_tenant_event, snapshot
from app.core.clock import Clock
from app.core.db import next_row_version
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.customers.models import AliasStatus, Customer, CustomerAlias
from app.search.normalize import normalize_text
from app.tenancy.context import TenantContext

__all__ = [
    "MAX_ALIASES_PER_CUSTOMER",
    "add_alias",
    "alias_map_for",
    "deactivate_alias",
    "list_aliases",
    "serialize_alias",
    "update_alias",
]

#: A bound, not a product rule. Somebody is called by three or four names, not by
#: forty; an unbounded list would be an unbounded slice of the search index and
#: an unbounded customer payload on every device.
MAX_ALIASES_PER_CUSTOMER = 20


def serialize_alias(alias: CustomerAlias) -> dict[str, Any]:
    return {
        "id": str(alias.id),
        "customer_id": str(alias.customer_id),
        "alias": alias.alias,
        "status": alias.status,
        "created_at": alias.created_at.isoformat() if alias.created_at else None,
        "updated_at": alias.updated_at.isoformat() if alias.updated_at else None,
    }


def _validated(value: str | None) -> tuple[str, str]:
    """The display text and its comparison key, or a validation failure."""
    text = (value or "").strip()
    if not text:
        raise ValidationFailed("alias is required", field_errors={"alias": "required"})
    if len(text) > 200:
        raise ValidationFailed(
            "alias is too long", field_errors={"alias": "at most 200 characters"}
        )
    normalized = normalize_text(text)
    if not normalized:
        # "!!!" normalizes to nothing, and an alias nobody can search for is not
        # an alias. Refused rather than stored as an empty comparison key that
        # would then match every empty query.
        raise ValidationFailed(
            "alias must contain a letter or a digit",
            field_errors={"alias": "must contain a letter or a digit"},
        )
    return text, normalized


def _customer(session: Session, ctx: TenantContext, customer_id: uuid.UUID) -> Customer:
    customer = session.execute(
        select(Customer).where(
            Customer.tenant_id == ctx.tenant_id, Customer.id == customer_id
        )
    ).scalar_one_or_none()
    if customer is None:
        raise NotFoundError("customer not found")  # SEC-4: 404, never 403
    return customer


def _alias_row(
    session: Session, ctx: TenantContext, customer_id: uuid.UUID, alias_id: uuid.UUID
) -> CustomerAlias:
    row = session.execute(
        select(CustomerAlias).where(
            CustomerAlias.tenant_id == ctx.tenant_id,
            CustomerAlias.customer_id == customer_id,
            CustomerAlias.id == alias_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("alias not found")
    return row


def list_aliases(
    session: Session,
    ctx: TenantContext,
    customer_id: uuid.UUID,
    *,
    include_inactive: bool = False,
) -> list[CustomerAlias]:
    """One customer's aliases, active first, then alphabetically."""
    stmt = select(CustomerAlias).where(
        CustomerAlias.tenant_id == ctx.tenant_id,
        CustomerAlias.customer_id == customer_id,
    )
    if not include_inactive:
        stmt = stmt.where(CustomerAlias.status == AliasStatus.ACTIVE)
    stmt = stmt.order_by(
        CustomerAlias.status, CustomerAlias.normalized, CustomerAlias.id
    )
    return list(session.execute(stmt).scalars().all())


def alias_map_for(
    session: Session, ctx: TenantContext, customer_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """Active aliases for a batch of customers, in **one** statement.

    This is the answer to "no N+1 alias queries": every caller that serializes
    more than one customer — the list route, the search results, the change feed
    — loads the whole batch's aliases here and hands the map to the serializer.
    An empty batch does not touch the database at all.
    """
    ids = list(customer_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(CustomerAlias.customer_id, CustomerAlias.alias)
        .where(
            CustomerAlias.tenant_id == ctx.tenant_id,
            CustomerAlias.customer_id.in_(ids),
            CustomerAlias.status == AliasStatus.ACTIVE,
        )
        .order_by(CustomerAlias.normalized, CustomerAlias.id)
    ).all()
    out: dict[uuid.UUID, list[str]] = {}
    for customer_id, alias in rows:
        out.setdefault(customer_id, []).append(alias)
    return out


def _touch_customer(session: Session, customer: Customer) -> None:
    """Advance the parent's version so the alias reaches every device."""
    customer.row_version = next_row_version(session)


def _now(session: Session, clock: Clock | None):
    return clock.now_utc() if clock is not None else session.execute(
        select(sql_func.now())
    ).scalar_one()


def add_alias(
    session: Session,
    ctx: TenantContext,
    customer_id: uuid.UUID,
    alias: str,
    *,
    operation_id: uuid.UUID,
    clock: Clock | None = None,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Record another name this customer is called by.

    Re-adding a spelling that was previously retired **reactivates that row**
    rather than inserting a second one, so one nickname has one history and the
    partial unique index keeps meaning what it says.
    """
    customer = _customer(session, ctx, customer_id)
    text, normalized = _validated(alias)

    existing = (
        session.execute(
            select(CustomerAlias).where(
                CustomerAlias.tenant_id == ctx.tenant_id,
                CustomerAlias.customer_id == customer_id,
                CustomerAlias.normalized == normalized,
            )
        )
        .scalars()
        .first()
    )

    if existing is not None and existing.status == AliasStatus.ACTIVE:
        raise ConflictError(
            f"{customer.name} is already known as {existing.alias!r}",
            code="ALIAS_ALREADY_EXISTS",
            extra={"alias_id": str(existing.id)},
        )

    if len(list_aliases(session, ctx, customer_id)) >= MAX_ALIASES_PER_CUSTOMER:
        raise ValidationFailed(
            f"a customer may have at most {MAX_ALIASES_PER_CUSTOMER} aliases",
            field_errors={"alias": "too many aliases"},
        )

    if existing is not None:
        before = snapshot("customer_alias", existing)
        existing.alias = text
        existing.status = AliasStatus.ACTIVE
        existing.deactivated_at = None
        existing.updated_at = _now(session, clock)
        row = existing
        action = AuditAction.CUSTOMER_ALIAS_REACTIVATED
    else:
        before = None
        row = CustomerAlias(
            tenant_id=ctx.tenant_id,
            customer_id=customer_id,
            alias=text,
            normalized=normalized,
        )
        session.add(row)
        action = AuditAction.CUSTOMER_ALIAS_ADDED

    _touch_customer(session, customer)
    try:
        session.flush()
    except IntegrityError as exc:  # pragma: no cover - guarded above, kept honest
        session.rollback()
        if "uq_customer_alias_active_normalized" in str(exc.orig):
            raise ConflictError(
                "that alias already exists for this customer",
                code="ALIAS_ALREADY_EXISTS",
            ) from exc
        raise

    record_tenant_event(
        session,
        ctx,
        action=action,
        entity_type="customer_alias",
        entity_id=row.id,
        before=before,
        after=snapshot("customer_alias", row),
        operation_id=operation_id,
    )
    session.flush()
    return serialize_alias(row), "customer_alias", row.id


def update_alias(
    session: Session,
    ctx: TenantContext,
    customer_id: uuid.UUID,
    alias_id: uuid.UUID,
    alias: str,
    *,
    operation_id: uuid.UUID,
    clock: Clock | None = None,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Correct the spelling of an alias, keeping its identity and its history.

    A typo is corrected in place — the audit event carries the old text and the
    new — rather than by removing a row and adding another, which would lose the
    fact that the two are the same nickname.
    """
    customer = _customer(session, ctx, customer_id)
    row = _alias_row(session, ctx, customer_id, alias_id)
    text, normalized = _validated(alias)

    clash = (
        session.execute(
            select(CustomerAlias).where(
                CustomerAlias.tenant_id == ctx.tenant_id,
                CustomerAlias.customer_id == customer_id,
                CustomerAlias.normalized == normalized,
                CustomerAlias.status == AliasStatus.ACTIVE,
                CustomerAlias.id != alias_id,
            )
        )
        .scalars()
        .first()
    )
    if clash is not None:
        raise ConflictError(
            f"{customer.name} is already known as {clash.alias!r}",
            code="ALIAS_ALREADY_EXISTS",
            extra={"alias_id": str(clash.id)},
        )

    before = snapshot("customer_alias", row)
    row.alias = text
    row.normalized = normalized
    row.updated_at = _now(session, clock)
    _touch_customer(session, customer)
    session.flush()

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.CUSTOMER_ALIAS_UPDATED,
        entity_type="customer_alias",
        entity_id=row.id,
        before=before,
        after=snapshot("customer_alias", row),
        operation_id=operation_id,
    )
    session.flush()
    return serialize_alias(row), "customer_alias", row.id


def deactivate_alias(
    session: Session,
    ctx: TenantContext,
    customer_id: uuid.UUID,
    alias_id: uuid.UUID,
    *,
    reason: str | None = None,
    operation_id: uuid.UUID,
    clock: Clock | None = None,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Retire an alias. The row stays; it stops matching."""
    customer = _customer(session, ctx, customer_id)
    row = _alias_row(session, ctx, customer_id, alias_id)

    if row.status == AliasStatus.INACTIVE:
        # Asking twice is not an error — the state is already what was asked for,
        # and inventing a second audit row for a change that did not happen would
        # make the history say something untrue.
        return serialize_alias(row), "customer_alias", row.id

    before = snapshot("customer_alias", row)
    row.status = AliasStatus.INACTIVE
    row.deactivated_at = _now(session, clock)
    row.updated_at = row.deactivated_at
    _touch_customer(session, customer)
    session.flush()

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.CUSTOMER_ALIAS_DEACTIVATED,
        entity_type="customer_alias",
        entity_id=row.id,
        before=before,
        after=snapshot("customer_alias", row),
        reason=reason,
        operation_id=operation_id,
    )
    session.flush()
    return serialize_alias(row), "customer_alias", row.id
