"""Customer domain commands. No delete path (AUD-1 in spirit; SEC-7 for logins)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditAction
from app.audit.service import record_tenant_event, snapshot
from app.core.db import next_row_version
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.money import QuantityError, MoneyError, quantize_quantity, validate_unit_price_minor
from app.customers.aliases import alias_map_for, list_aliases
from app.customers.models import Customer
from app.search.normalize import normalize_text
from app.tenancy.context import TenantContext

__all__ = [
    "CreateCustomerInput",
    "UpdateCustomerInput",
    "create_customer",
    "update_customer",
    "get_customer",
    "list_customers",
    "serialize_customer",
    "serialize_customers",
]

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_UNSET = object()


@dataclass(frozen=True, slots=True)
class CreateCustomerInput:
    code: str
    name: str
    phone_e164: str | None = None
    whatsapp_e164: str | None = None
    address: str | None = None
    area: str | None = None
    default_quantity: Decimal | int | str = Decimal("0")
    unit_price_minor: int = 0


@dataclass(frozen=True, slots=True)
class UpdateCustomerInput:
    """Every field optional; ``_UNSET`` distinguishes "absent" from "set to null"."""

    name: Any = _UNSET
    phone_e164: Any = _UNSET
    whatsapp_e164: Any = _UNSET
    address: Any = _UNSET
    area: Any = _UNSET
    default_quantity: Any = _UNSET
    unit_price_minor: Any = _UNSET
    status: Any = _UNSET
    expected_row_version: int | None = None


def _validate_phone(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not _E164.match(value):
        raise ValidationFailed(
            f"{field} must be E.164 format, e.g. +923001234567",
            field_errors={field: "must be E.164, e.g. +923001234567"},
        )
    return value


def _validate_money_and_quantity(quantity_value: Any, price_value: Any) -> tuple[Decimal, int]:
    try:
        quantity = quantize_quantity(quantity_value)
    except (QuantityError, ValueError) as exc:
        raise ValidationFailed(str(exc), field_errors={"default_quantity": str(exc)}) from exc
    try:
        price = validate_unit_price_minor(price_value)
    except (MoneyError, ValueError) as exc:
        raise ValidationFailed(str(exc), field_errors={"unit_price_minor": str(exc)}) from exc
    return quantity, price


def serialize_customer(
    customer: Customer,
    ctx: TenantContext,
    *,
    aliases: Sequence[str] = (),
) -> dict[str, Any]:
    """The customer as every reader sees them, aliases included.

    ``aliases`` is passed in rather than looked up here, because this function is
    called once per row by the list route and by the change feed: a query inside
    it would be an N+1 over the whole customer population. :func:`serialize_customers`
    is the batching wrapper, and it is what those callers use.
    """
    return {
        "id": str(customer.id),
        "code": customer.code,
        "name": customer.name,
        # P8. The names this customer is actually called. Part of the customer
        # payload rather than a sync entity of its own, so an offline device can
        # find "Ahmed bhai" with nothing new in the feed but a version bump.
        "aliases": list(aliases),
        "phone_e164": customer.phone_e164,
        "whatsapp_e164": customer.whatsapp_e164,
        "address": customer.address,
        "area": customer.area,
        "default_quantity": str(customer.default_quantity),
        "unit_price_minor": customer.unit_price_minor,
        "status": customer.status,
        "row_version": customer.row_version,
        "unit_label": ctx.unit_label,
        "currency": ctx.currency,
        "currency_exponent": ctx.currency_exponent,
    }


def serialize_customers(
    session: Session, ctx: TenantContext, customers: Sequence[Customer]
) -> list[dict[str, Any]]:
    """Serialize a batch, loading every alias in one further statement."""
    aliases = alias_map_for(session, ctx, [c.id for c in customers])
    return [
        serialize_customer(c, ctx, aliases=aliases.get(c.id, ())) for c in customers
    ]


def get_customer(session: Session, ctx: TenantContext, customer_id: uuid.UUID) -> Customer:
    """SEC-3: tenant scoping is structural — there is no unscoped variant."""
    customer = session.execute(
        select(Customer).where(
            Customer.tenant_id == ctx.tenant_id, Customer.id == customer_id
        )
    ).scalar_one_or_none()
    if customer is None:
        raise NotFoundError("customer not found")  # SEC-4: 404, never 403
    return customer


def list_customers(
    session: Session,
    ctx: TenantContext,
    *,
    area: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Customer]:
    stmt = select(Customer).where(Customer.tenant_id == ctx.tenant_id)
    if area:
        stmt = stmt.where(Customer.area == area)
    if status:
        stmt = stmt.where(Customer.status == status)
    # Ordered by (name, id), not by name alone. `name` is not unique, and an
    # offset/limit page is only sound over a *total* order: with ties at a page
    # boundary PostgreSQL is free to return them in a different relative order
    # for each page, so a caller walking the pages could miss a customer or see
    # one twice. `id` breaks every tie deterministically. This does not change
    # the contract — customers still come back in name order — it makes the
    # pagination the contract already offers actually correct.
    stmt = stmt.order_by(Customer.name, Customer.id).limit(min(limit, 500)).offset(offset)
    return list(session.execute(stmt).scalars().all())


def create_customer(
    session: Session,
    ctx: TenantContext,
    data: CreateCustomerInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    if not data.code.strip():
        raise ValidationFailed("code is required", field_errors={"code": "required"})
    if not data.name.strip():
        raise ValidationFailed("name is required", field_errors={"name": "required"})

    quantity, price = _validate_money_and_quantity(data.default_quantity, data.unit_price_minor)

    customer = Customer(
        tenant_id=ctx.tenant_id,
        code=data.code.strip(),
        name=data.name.strip(),
        normalized_name=normalize_text(data.name),
        phone_e164=_validate_phone(data.phone_e164, "phone_e164"),
        whatsapp_e164=_validate_phone(data.whatsapp_e164, "whatsapp_e164"),
        address=data.address,
        area=data.area,
        default_quantity=quantity,
        unit_price_minor=price,
        row_version=next_row_version(session),
    )
    session.add(customer)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if "uq_customer_tenant_id_code" in str(exc.orig):
            raise ConflictError(
                f"a customer with code {data.code!r} already exists",
                code="CUSTOMER_CODE_TAKEN",
            ) from exc
        raise

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.CUSTOMER_CREATED,
        entity_type="customer",
        entity_id=customer.id,
        before=None,
        after=snapshot("customer", customer),
        operation_id=operation_id,
    )
    session.flush()
    # A customer that has just been created has no aliases, so this is the one
    # serialization that needs no lookup to say so.
    return serialize_customer(customer, ctx), "customer", customer.id


def update_customer(
    session: Session,
    ctx: TenantContext,
    customer_id: uuid.UUID,
    data: UpdateCustomerInput,
    *,
    operation_id: uuid.UUID,
) -> tuple[dict[str, Any], str, uuid.UUID]:
    """Update a customer. Price and configuration changes are audited (FIN-6)."""
    customer = get_customer(session, ctx, customer_id)

    if (
        data.expected_row_version is not None
        and customer.row_version != data.expected_row_version
    ):
        raise ConflictError(
            "customer has been modified by someone else",
            code="ROW_VERSION_CONFLICT",
            extra={"current_row_version": customer.row_version},
        )

    before = snapshot("customer", customer)

    if data.name is not _UNSET:
        if not str(data.name).strip():
            raise ValidationFailed("name is required", field_errors={"name": "required"})
        customer.name = str(data.name).strip()
        # The comparison key is never allowed to drift from the name it
        # describes: a rename that left it behind would make the customer
        # findable only under the name they no longer have.
        customer.normalized_name = normalize_text(customer.name)
    if data.phone_e164 is not _UNSET:
        customer.phone_e164 = _validate_phone(data.phone_e164, "phone_e164")
    if data.whatsapp_e164 is not _UNSET:
        customer.whatsapp_e164 = _validate_phone(data.whatsapp_e164, "whatsapp_e164")
    if data.address is not _UNSET:
        customer.address = data.address
    if data.area is not _UNSET:
        customer.area = data.area
    if data.default_quantity is not _UNSET:
        customer.default_quantity, _ = _validate_money_and_quantity(
            data.default_quantity, customer.unit_price_minor
        )
    if data.unit_price_minor is not _UNSET:
        # Changing the current price never touches an existing record's snapshot.
        _, customer.unit_price_minor = _validate_money_and_quantity(
            customer.default_quantity, data.unit_price_minor
        )
    if data.status is not _UNSET:
        if data.status not in ("ACTIVE", "INACTIVE"):
            raise ValidationFailed(
                "status must be ACTIVE or INACTIVE", field_errors={"status": "invalid"}
            )
        customer.status = data.status

    customer.row_version = next_row_version(session)
    session.flush()

    record_tenant_event(
        session,
        ctx,
        action=AuditAction.CUSTOMER_UPDATED,
        entity_type="customer",
        entity_id=customer.id,
        before=before,
        after=snapshot("customer", customer),
        operation_id=operation_id,
    )
    session.flush()
    aliases = [a.alias for a in list_aliases(session, ctx, customer.id)]
    return (
        serialize_customer(customer, ctx, aliases=aliases),
        "customer",
        customer.id,
    )
