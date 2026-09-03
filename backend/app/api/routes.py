"""HTTP routers. Thin by design: authenticate, validate, call the domain, serialize.

No business rule lives here. Every mutation carries an ``operation_id`` and runs
through :func:`app.sync.idempotency.execute_idempotent`, so the online path and
P5's bulk sync path share one guarantee rather than two implementations.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentPrincipal,
    Db,
    TenantCtx,
    build_platform_context,
    get_app_settings,
    get_clock,
    get_communication_provider,
    require_capability,
    require_job_secret,
)
from app.api.schemas import (
    AddAliasRequest,
    CloseCycleRequest,
    CorrectServiceRequest,
    CostScenarioRequest,
    CreateCommissionPlanRequest,
    CreateCostItemRequest,
    CreateCostRateRequest,
    CreateCustomerRequest,
    DeactivateAliasRequest,
    LoginRequest,
    LogoutRequest,
    OperationResponse,
    RecordCommissionSettlementRequest,
    RecordCostActualRequest,
    RecordCostUsageRequest,
    RecordPaymentRequest,
    RecordServiceRequest,
    RefreshRequest,
    ResolveCustomerRequest,
    SendReminderRequest,
    SyncOperationsRequest,
    TokenResponse,
    UpdateAliasRequest,
    UpdateCustomerRequest,
    VoidPaymentRequest,
    VoidServiceRequest,
)
from app.billing.cycles import close_cycle, list_cycles, serialize_cycle
from app.billing.dashboard import (
    DEFAULT_RECENT_PAYMENTS,
    dashboard_summary,
    outstanding_customers,
)
from app.billing.ledger import outstanding_minor
from app.billing.reporting import customer_payment_status
from app.billing.statements import (
    list_all_statements,
    list_statements,
    load_statement,
    serialize_statement,
)
from app.commission.plans import (
    CreatePlanInput,
    create_plan,
    list_plans,
    serialize_plan,
)
from app.commission.reporting import commission_position, serialize_position
from app.commission.settlements import RecordSettlementInput, record_settlement
from app.core.clock import Clock
from app.core.config import Settings
from app.costs.commands import (
    CreateCostItemInput,
    CreateCostRateInput,
    RecordActualInput,
    RecordUsageInput,
    create_cost_item,
    create_cost_rate,
    list_cost_items,
    list_rates,
    record_actual,
    record_usage,
    serialize_cost_item,
    serialize_rate,
)
from app.costs.estimates import month_start
from app.costs.reporting import evaluate_scenarios, month_history, month_summary
from app.customers.aliases import (
    add_alias,
    deactivate_alias,
    list_aliases,
    serialize_alias,
    update_alias,
)
from app.customers.commands import (
    CreateCustomerInput,
    UpdateCustomerInput,
    create_customer,
    get_customer,
    list_customers,
    serialize_customer,
    serialize_customers,
    update_customer,
)
from app.identity import service as auth_service
from app.jobs.daily import run_daily
from app.payments.commands import (
    RecordPaymentInput,
    VoidPaymentInput,
    list_all_payments,
    list_payments,
    record_payment,
    serialize_payment,
    void_payment,
)
from app.ports.comms import CommunicationProvider
from app.search.filters import CustomerSearchFilter
from app.search.query import search_customers, serialize_match
from app.search.resolver import resolve_customer, serialize_resolution
from app.reminders.engine import dispatch_reminder, serialize_reminder
from app.reminders.reporting import (
    load_reminder_for_tenant,
    reminder_detail,
    reminder_overview,
)
from app.service.commands import (
    CorrectServiceInput,
    RecordServiceInput,
    VoidServiceInput,
    correct_service,
    list_customer_history,
    list_day,
    record_service,
    serialize_record,
    void_service,
)
from app.sync.changes import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, changes_since
from app.sync.envelope import op_type_for_kind
from app.sync.idempotency import execute_idempotent
from app.sync.operations import SyncEnvelope, apply_operation
from app.tenancy.context import TenantContext
from app.tenancy.settings import tenant_settings

auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
tenant_router = APIRouter(prefix="/api/v1/tenant", tags=["tenant"])
customer_router = APIRouter(prefix="/api/v1/customers", tags=["customers"])
service_router = APIRouter(prefix="/api/v1/service", tags=["service"])
billing_router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
statement_router = APIRouter(prefix="/api/v1/statements", tags=["billing"])
payment_router = APIRouter(prefix="/api/v1/payments", tags=["payments"])
dashboard_router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
# P6. Deliberately its own prefix and its own capabilities: what the business
# pays its providers is not what a customer owes it and not what the platform
# earns from it.
cost_router = APIRouter(prefix="/api/v1/operating-costs", tags=["operating-costs"])
# P7. Read and manual re-dispatch only: there is no "create a reminder" route and
# no way for a caller to choose a stage, an amount or a recipient. Those are the
# engine's, and the schedule's.
reminder_router = APIRouter(prefix="/api/v1/reminders", tags=["reminders"])
# P8. Read-only, tenant-scoped, and gated by the ``search:use`` capability P0
# §3.2 froze. There is no write on this router and never will be: search finds
# people, it does not change them.
search_router = APIRouter(prefix="/api/v1/search", tags=["search"])
sync_router = APIRouter(prefix="/api/v1/sync", tags=["sync"])
# P0 §12, §15: driven by the host's cron with a shared secret, never by a user
# token. Deliberately outside every tenant prefix and taking no tenant parameter.
internal_job_router = APIRouter(prefix="/api/v1/internal/jobs", tags=["internal"])
# P0 §15: the platform surface is a separate prefix, and every route on it is
# gated by a commission capability no tenant role holds (SEC-5, COM-7).
platform_commission_router = APIRouter(
    prefix="/api/v1/platform/commission", tags=["platform-commission"]
)

_UNSET = UpdateCustomerInput.__dataclass_fields__["name"].default


# --- auth -------------------------------------------------------------------


@auth_router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    session: Db,
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> TokenResponse:
    tokens = auth_service.login(
        session,
        email=body.email,
        password=body.password,
        settings=settings,
        clock=clock,
        device_label=body.device_label,
    )
    return TokenResponse(**asdict(tokens))


@auth_router.post("/refresh", response_model=TokenResponse)
def refresh(
    body: RefreshRequest,
    session: Db,
    settings: Annotated[Settings, Depends(get_app_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> TokenResponse:
    tokens = auth_service.refresh(
        session, refresh_token=body.refresh_token, settings=settings, clock=clock
    )
    return TokenResponse(**asdict(tokens))


@auth_router.post("/logout", status_code=204, response_class=Response)
def logout(
    body: LogoutRequest,
    session: Db,
    clock: Annotated[Clock, Depends(get_clock)],
) -> Response:
    auth_service.logout(session, refresh_token=body.refresh_token, clock=clock)
    return Response(status_code=204)


# --- tenant configuration ---------------------------------------------------


@tenant_router.get("/settings")
def tenant_settings_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("dashboard:read"))],
) -> dict:
    """The tenant's own configuration and business date (P0 §4, R4).

    Gated by ``dashboard:read`` — the existing P0 §3.2 capability for reading the
    business's own top-level state. No capability was added: the frozen map is
    unchanged, and only ``OWNER_ADMIN`` holds this one.
    """
    return tenant_settings(session, ctx)


# --- customers --------------------------------------------------------------


@customer_router.get("")
def list_customers_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("customer:read"))],
    area: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows = list_customers(
        session, ctx, area=area, status=status, limit=limit, offset=offset
    )
    # Batched: one alias statement for the page, never one per customer.
    return {"items": serialize_customers(session, ctx, rows)}


@customer_router.post("", response_model=OperationResponse, status_code=201)
def create_customer_route(
    body: CreateCustomerRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("customer:write"))],
) -> OperationResponse:
    payload = body.model_dump(mode="json", exclude={"operation_id"})
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="customer.create",
        payload=payload,
        perform=lambda: create_customer(
            session,
            ctx,
            CreateCustomerInput(
                code=body.code,
                name=body.name,
                phone_e164=body.phone_e164,
                whatsapp_e164=body.whatsapp_e164,
                address=body.address,
                area=body.area,
                default_quantity=body.default_quantity,
                unit_price_minor=body.unit_price_minor,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@customer_router.get("/{customer_id}")
def get_customer_route(
    customer_id: uuid.UUID,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("customer:read"))],
) -> dict:
    customer = get_customer(session, ctx, customer_id)
    data = serialize_customer(
        customer,
        ctx,
        aliases=[a.alias for a in list_aliases(session, ctx, customer.id)],
    )
    # FIN-4 / FIN-11: both derived on read, never stored and never client-computed.
    data["outstanding_minor"] = outstanding_minor(session, ctx, customer.id)
    data["payment_status"] = customer_payment_status(session, ctx, customer.id)
    return data


@customer_router.patch("/{customer_id}", response_model=OperationResponse)
def update_customer_route(
    customer_id: uuid.UUID,
    body: UpdateCustomerRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("customer:write"))],
) -> OperationResponse:
    provided = body.model_dump(mode="json", exclude_unset=True, exclude={"operation_id"})

    def _field(name: str):
        return provided[name] if name in provided else _UNSET

    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="customer.update",
        payload={"customer_id": str(customer_id), **provided},
        perform=lambda: update_customer(
            session,
            ctx,
            customer_id,
            UpdateCustomerInput(
                name=_field("name"),
                phone_e164=_field("phone_e164"),
                whatsapp_e164=_field("whatsapp_e164"),
                address=_field("address"),
                area=_field("area"),
                default_quantity=_field("default_quantity"),
                unit_price_minor=_field("unit_price_minor"),
                status=_field("status"),
                expected_row_version=body.expected_row_version,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


# --- customer aliases (P8) ---------------------------------------------------
#
# The names a customer is actually called. Three writes and one read, all under
# the existing ``customer:read`` / ``customer:write`` capabilities — an alias is
# customer data, so it needs no capability of its own and the frozen map (P0
# §3.2) is unchanged.
#
# Every write carries an ``operation_id`` and goes through ``execute_idempotent``
# like any other mutation, and its ``op_type`` is in ``FEED_WRITING_OP_TYPES``
# because an alias write bumps its customer's ``row_version``. None of them is in
# ``SUPPORTED_OP_TYPES``: aliases are an online-only edit, exactly as customer
# create and edit are, so ``POST /sync/operations`` refuses them.


@customer_router.get("/{customer_id}/aliases")
def list_aliases_route(
    customer_id: uuid.UUID,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("customer:read"))],
    include_inactive: bool = False,
) -> dict:
    get_customer(session, ctx, customer_id)  # SEC-4: 404 for another tenant's id
    rows = list_aliases(session, ctx, customer_id, include_inactive=include_inactive)
    return {"items": [serialize_alias(a) for a in rows]}


@customer_router.post("/{customer_id}/aliases", response_model=OperationResponse, status_code=201)
def add_alias_route(
    customer_id: uuid.UUID,
    body: AddAliasRequest,
    ctx: TenantCtx,
    session: Db,
    clock: Annotated[Clock, Depends(get_clock)],
    _: Annotated[object, Depends(require_capability("customer:write"))],
) -> OperationResponse:
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="customer.alias.add",
        payload={"customer_id": str(customer_id), "alias": body.alias},
        perform=lambda: add_alias(
            session,
            ctx,
            customer_id,
            body.alias,
            operation_id=body.operation_id,
            clock=clock,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@customer_router.patch(
    "/{customer_id}/aliases/{alias_id}", response_model=OperationResponse
)
def update_alias_route(
    customer_id: uuid.UUID,
    alias_id: uuid.UUID,
    body: UpdateAliasRequest,
    ctx: TenantCtx,
    session: Db,
    clock: Annotated[Clock, Depends(get_clock)],
    _: Annotated[object, Depends(require_capability("customer:write"))],
) -> OperationResponse:
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="customer.alias.update",
        payload={
            "customer_id": str(customer_id),
            "alias_id": str(alias_id),
            "alias": body.alias,
        },
        perform=lambda: update_alias(
            session,
            ctx,
            customer_id,
            alias_id,
            body.alias,
            operation_id=body.operation_id,
            clock=clock,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@customer_router.post(
    "/{customer_id}/aliases/{alias_id}/deactivate", response_model=OperationResponse
)
def deactivate_alias_route(
    customer_id: uuid.UUID,
    alias_id: uuid.UUID,
    body: DeactivateAliasRequest,
    ctx: TenantCtx,
    session: Db,
    clock: Annotated[Clock, Depends(get_clock)],
    _: Annotated[object, Depends(require_capability("customer:write"))],
) -> OperationResponse:
    """Retire an alias. There is deliberately no DELETE verb and no delete row."""
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="customer.alias.deactivate",
        payload={
            "customer_id": str(customer_id),
            "alias_id": str(alias_id),
            "reason": body.reason,
        },
        perform=lambda: deactivate_alias(
            session,
            ctx,
            customer_id,
            alias_id,
            reason=body.reason,
            operation_id=body.operation_id,
            clock=clock,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


# --- search and identification (P8) ------------------------------------------


@search_router.post("/customers")
def search_customers_route(
    body: CustomerSearchFilter,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("search:use"))],
) -> dict:
    """Structured customer search (P0 §15 ``POST /search/customers``).

    The body **is** the frozen filter object: FastAPI validates it with the same
    model the domain uses, so an unknown field, an inflated limit or an SQL
    fragment is refused before a query is built rather than after. The result is
    bounded by the filter's own cap.
    """
    matches = search_customers(session, ctx, body)
    return {
        "items": [serialize_match(m, ctx) for m in matches],
        "limit": body.limit,
        "offset": body.offset,
        # Honest about truncation: a full page means "there may be more", which
        # is not the same as "these are all of them".
        "possibly_truncated": len(matches) == body.limit,
    }


@search_router.post("/customers/resolve")
def resolve_customer_route(
    body: ResolveCustomerRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("search:use"))],
) -> dict:
    """Identify one customer from free text — RESOLVED, AMBIGUOUS or NOT_FOUND.

    The channel-independent half of P8. What a person typed here, what a
    transcript says later and what an inbound message says later still all arrive
    at the same function and get the same answer; there is no second resolution
    path for any of them to grow into.
    """
    resolution = resolve_customer(
        session,
        ctx,
        body.reference,
        limit=body.limit,
        include_inactive=body.include_inactive,
        allow_fuzzy=body.allow_fuzzy,
    )
    return serialize_resolution(resolution, ctx)


# --- daily service ----------------------------------------------------------


@service_router.post("/records", response_model=OperationResponse, status_code=201)
def record_service_route(
    body: RecordServiceRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("service:record"))],
) -> OperationResponse:
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        # The same op type a queued envelope carries for this kind, so a retry
        # that changes transport is recognised as the same request (SYN-2/14)
        # rather than refused as an operation_id reused for a different one.
        op_type=op_type_for_kind(body.kind),
        payload=body.model_dump(mode="json", exclude={"operation_id"}),
        perform=lambda: record_service(
            session,
            ctx,
            RecordServiceInput(
                customer_id=body.customer_id,
                kind=body.kind,
                quantity=body.quantity,
                service_date=body.service_date,
                input_method=body.input_method,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@service_router.post("/records/{record_id}/correct", response_model=OperationResponse)
def correct_service_route(
    record_id: uuid.UUID,
    body: CorrectServiceRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("service:correct"))],
) -> OperationResponse:
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="service.correct",
        payload={"record_id": str(record_id), **body.model_dump(mode="json", exclude={"operation_id"})},
        perform=lambda: correct_service(
            session,
            ctx,
            record_id,
            CorrectServiceInput(
                quantity=body.quantity,
                reason=body.reason,
                kind=body.kind,
                input_method=body.input_method,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@service_router.post("/records/{record_id}/void", response_model=OperationResponse)
def void_service_route(
    record_id: uuid.UUID,
    body: VoidServiceRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("service:correct"))],
) -> OperationResponse:
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="service.void",
        payload={"record_id": str(record_id), "reason": body.reason},
        perform=lambda: void_service(
            session,
            ctx,
            record_id,
            VoidServiceInput(reason=body.reason),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@service_router.get("/day/{service_date}")
def list_day_route(
    service_date: date,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("service:record"))],
    include_history: bool = False,
) -> dict:
    records = list_day(session, ctx, service_date, include_history=include_history)
    return {
        "service_date": service_date.isoformat(),
        "business_date": ctx.today.isoformat(),
        "items": [serialize_record(r, ctx) for r in records],
    }


@customer_router.get("/{customer_id}/statements")
def list_customer_statements_route(
    customer_id: uuid.UUID,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("billing:read"))],
) -> dict:
    customer = get_customer(session, ctx, customer_id)
    rows = list_statements(session, ctx, customer.id)
    return {"items": [serialize_statement(s, ctx) for s in rows]}


@customer_router.get("/{customer_id}/payments")
def list_customer_payments_route(
    customer_id: uuid.UUID,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("billing:read"))],
) -> dict:
    """One customer's payment history, voided rows included (AUD-8).

    A void is never hidden: the row stays, carrying its reason and its actor, and
    the compensating ledger entry that explains the balance is the void's whole
    point. Hiding it would leave a balance nothing on screen accounts for.
    """
    customer = get_customer(session, ctx, customer_id)
    rows = list_payments(session, ctx, customer.id)
    return {"items": [serialize_payment(p, ctx) for p in rows]}


@customer_router.get("/{customer_id}/history")
def customer_history_route(
    customer_id: uuid.UUID,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("billing:read"))],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """A customer's service records — active, superseded and voided (A-AUD-8)."""
    customer = get_customer(session, ctx, customer_id)
    rows = list_customer_history(
        session, ctx, customer.id, limit=limit, offset=offset
    )
    return {"items": [serialize_record(r, ctx) for r in rows]}


# --- billing cycles ---------------------------------------------------------


@billing_router.get("/cycles")
def list_cycles_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("billing:read"))],
) -> dict:
    return {"items": [serialize_cycle(c) for c in list_cycles(session, ctx)]}


@billing_router.post("/cycles/{cycle_id}/close", response_model=OperationResponse)
def close_cycle_route(
    cycle_id: uuid.UUID,
    body: CloseCycleRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("billing:close_cycle"))],
) -> OperationResponse:
    """Close the cycle and issue its statements in one transaction.

    P0 §15 exposes no statement-issuing route: a statement is only sound once its
    cycle can receive no further entries, so issue is part of close rather than a
    separate call anyone could make too early.
    """
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="billing.close_cycle",
        payload={"cycle_id": str(cycle_id)},
        perform=lambda: close_cycle(
            session, ctx, cycle_id, operation_id=body.operation_id
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


# --- statements -------------------------------------------------------------


@statement_router.get("")
def list_all_statements_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("billing:read"))],
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Every issued statement, newest period first.

    P0 §15 froze ``GET /statements/{id}`` and the per-customer list; the owner's
    statement screen needs the tenant-wide one to exist at all, and it is the
    read a first-time device seeds its statement snapshot from.
    """
    rows = list_all_statements(session, ctx, limit=limit, offset=offset)
    return {"items": [serialize_statement(st, ctx) for st in rows]}


@statement_router.get("/{statement_id}")
def get_statement_route(
    statement_id: uuid.UUID,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("billing:read"))],
) -> dict:
    return serialize_statement(load_statement(session, ctx, statement_id), ctx)


# --- payments ---------------------------------------------------------------


@payment_router.get("")
def list_all_payments_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("billing:read"))],
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Every payment, most recent first, voided rows included (AUD-8)."""
    rows = list_all_payments(session, ctx, limit=limit, offset=offset)
    return {"items": [serialize_payment(p, ctx) for p in rows]}


@payment_router.post("", response_model=OperationResponse, status_code=201)
def record_payment_route(
    body: RecordPaymentRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("payment:record"))],
) -> OperationResponse:
    """Record a manual payment. PAY-5: ``operation_id`` is the whole of the
    duplicate protection — the same mechanism every other write uses."""
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="payment.record",
        payload=body.model_dump(mode="json", exclude={"operation_id"}),
        perform=lambda: record_payment(
            session,
            ctx,
            RecordPaymentInput(
                customer_id=body.customer_id,
                amount_minor=body.amount_minor,
                method=body.method,
                received_on=body.received_on,
                reference=body.reference,
                note=body.note,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@payment_router.post("/{payment_id}/void", response_model=OperationResponse)
def void_payment_route(
    payment_id: uuid.UUID,
    body: VoidPaymentRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("payment:void"))],
) -> OperationResponse:
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="payment.void",
        payload={"payment_id": str(payment_id), "reason": body.reason},
        perform=lambda: void_payment(
            session,
            ctx,
            payment_id,
            VoidPaymentInput(reason=body.reason),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


# --- platform commission (P0 §11, §15) ---------------------------------------
#
# COM-7/COM-8: every route here requires a ``commission:*`` capability, and the
# tenant capability set contains none of them — so an owner-admin token is 403 on
# all of them, read included, without any per-route cleverness.


@platform_commission_router.get("/summary")
def commission_summary_route(
    session: Db,
    principal: CurrentPrincipal,
    clock: Annotated[Clock, Depends(get_clock)],
    _: Annotated[object, Depends(require_capability("commission:read"))],
    tenant_id: uuid.UUID = Query(..., description="the tenant to report on"),
) -> dict:
    """P0 §11.1 group C: earned + adjustments − settled = outstanding.

    The tenant is named explicitly by the platform caller; there is no "my
    tenant" here, because a platform principal has none.
    """
    ctx = build_platform_context(session, clock, principal, tenant_id)
    return serialize_position(commission_position(session, ctx), ctx)


@platform_commission_router.get("/plans")
def list_commission_plans_route(
    session: Db,
    principal: CurrentPrincipal,
    clock: Annotated[Clock, Depends(get_clock)],
    _: Annotated[object, Depends(require_capability("commission:read"))],
    tenant_id: uuid.UUID = Query(..., description="the tenant whose plans to list"),
) -> dict:
    ctx = build_platform_context(session, clock, principal, tenant_id)
    return {"items": [serialize_plan(p) for p in list_plans(session, ctx)]}


@platform_commission_router.post("/plans", response_model=OperationResponse, status_code=201)
def create_commission_plan_route(
    body: CreateCommissionPlanRequest,
    session: Db,
    principal: CurrentPrincipal,
    clock: Annotated[Clock, Depends(get_clock)],
    _: Annotated[object, Depends(require_capability("commission:adjust"))],
) -> OperationResponse:
    """Create a commission plan (COM-1, COM-8).

    Idempotent through the same register every other write uses: the register key
    is ``(tenant_id, operation_id)`` and the target tenant is the one named in the
    body, so a retried plan creation cannot produce two overlapping plans.
    """
    ctx = build_platform_context(session, clock, principal, body.tenant_id)
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="commission.plan.create",
        payload=body.model_dump(mode="json", exclude={"operation_id"}),
        perform=lambda: create_plan(
            session,
            ctx,
            CreatePlanInput(
                basis=body.basis,
                rate_bp=body.rate_bp,
                fixed_amount_minor=body.fixed_amount_minor,
                currency=body.currency,
                effective_from=body.effective_from,
                effective_to=body.effective_to,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@platform_commission_router.post(
    "/settlements", response_model=OperationResponse, status_code=201
)
def record_commission_settlement_route(
    body: RecordCommissionSettlementRequest,
    session: Db,
    principal: CurrentPrincipal,
    clock: Annotated[Clock, Depends(get_clock)],
    _: Annotated[object, Depends(require_capability("commission:settle"))],
) -> OperationResponse:
    """Record money settled (COM-6, COM-8).

    Strictly additive: nothing is stamped on an earning event, and a replay
    returns the same settlement rather than recording it twice.
    """
    ctx = build_platform_context(session, clock, principal, body.tenant_id)
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="commission.settlement.record",
        payload=body.model_dump(mode="json", exclude={"operation_id"}),
        perform=lambda: record_settlement(
            session,
            ctx,
            RecordSettlementInput(
                period_start=body.period_start,
                period_end=body.period_end,
                amount_minor=body.amount_minor,
                settled_on=body.settled_on,
                reference=body.reference,
                note=body.note,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


# --- owner dashboard (P0 §15) ------------------------------------------------
#
# Every number here is derived server-side from the ledger by the same functions
# statements use. The client renders them; it never adds a page of customer rows
# together to produce a total (FIN-4, FIN-11, SYN-9).
#
# ``dashboard:read`` is the existing P0 §3.2 capability for exactly this — the
# business's own top-level state. No commission figure appears on either route:
# a tenant principal holds no ``commission:*`` capability and these do not go
# looking on its behalf.


@dashboard_router.get("/summary")
def dashboard_summary_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("dashboard:read"))],
    recent_payments: int = Query(default=DEFAULT_RECENT_PAYMENTS, ge=1, le=50),
) -> dict:
    """The owner's headline figures for the open cycle and for all time."""
    return dashboard_summary(session, ctx, recent_payments=recent_payments)


@dashboard_router.get("/outstanding")
def dashboard_outstanding_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("dashboard:read"))],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Who owes money, most owed first — one grouped query, not one per customer."""
    return outstanding_customers(session, ctx, limit=limit, offset=offset)


# --- operating costs (P6) ----------------------------------------------------
#
# The owner's provider expenses. Tenant scope, gated by ``cost:read`` /
# ``cost:write`` — capabilities of their own precisely so nothing here can be
# reached with commission authority and nothing there can be reached with this
# one. These routes touch no ledger entry, no statement and no commission row,
# and the reverse is equally true.
#
# Every write carries an ``operation_id`` and goes through the same idempotency
# register as the rest of the system. None of them is an accepted sync
# operation: cost mutations are online-only (P6 §19).


@cost_router.get("/items")
def list_cost_items_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("cost:read"))],
    include_archived: bool = False,
) -> dict:
    """The configured cost items and every rate each one has ever had.

    Old rates come back alongside current ones because an old rate is the only
    explanation an old estimate has.
    """
    items = list_cost_items(session, ctx, include_archived=include_archived)
    rates = list_rates(session, ctx)
    by_item: dict[str, list[dict]] = {}
    for rate in rates:
        by_item.setdefault(str(rate.cost_item_id), []).append(serialize_rate(rate))
    return {
        "items": [
            {**serialize_cost_item(item), "rates": by_item.get(str(item.id), [])}
            for item in items
        ]
    }


@cost_router.post("/items", response_model=OperationResponse, status_code=201)
def create_cost_item_route(
    body: CreateCostItemRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("cost:write"))],
) -> OperationResponse:
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="cost.item.create",
        payload=body.model_dump(mode="json", exclude={"operation_id"}),
        perform=lambda: create_cost_item(
            session,
            ctx,
            CreateCostItemInput(
                code=body.code, name=body.name, description=body.description
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@cost_router.post(
    "/items/{cost_item_id}/rates", response_model=OperationResponse, status_code=201
)
def create_cost_rate_route(
    cost_item_id: uuid.UUID,
    body: CreateCostRateRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("cost:write"))],
) -> OperationResponse:
    """Add a versioned rate; its open-ended predecessor is closed, never edited.

    There is no rate-edit route, for the same reason there is no plan-edit route:
    the terms are snapshotted onto recorded months, so rewriting one would
    silently restate history.
    """
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="cost.rate.create",
        payload={
            "cost_item_id": str(cost_item_id),
            **body.model_dump(mode="json", exclude={"operation_id"}),
        },
        perform=lambda: create_cost_rate(
            session,
            ctx,
            CreateCostRateInput(
                cost_item_id=cost_item_id,
                effective_from=body.effective_from,
                unit=body.unit,
                unit_price_minor=body.unit_price_minor,
                fixed_amount_minor=body.fixed_amount_minor,
                fixed_recurrence=body.fixed_recurrence,
                currency=body.currency,
                currency_exponent=body.currency_exponent,
                source_note=body.source_note,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@cost_router.post("/usage", response_model=OperationResponse, status_code=201)
def record_cost_usage_route(
    body: RecordCostUsageRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("cost:write"))],
) -> OperationResponse:
    """Record a month's measured usage and freeze the estimate it produces."""
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="cost.usage.record",
        payload=body.model_dump(mode="json", exclude={"operation_id"}),
        perform=lambda: record_usage(
            session,
            ctx,
            RecordUsageInput(
                cost_item_id=body.cost_item_id,
                period_month=body.period_month,
                # A string on the wire, a Decimal in the domain — never a float.
                usage_quantity=Decimal(body.usage_quantity),
                inputs=body.inputs,
                note=body.note,
                correction_reason=body.correction_reason,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@cost_router.post("/actuals", response_model=OperationResponse, status_code=201)
def record_cost_actual_route(
    body: RecordCostActualRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("cost:write"))],
) -> OperationResponse:
    """Record what a provider actually invoiced for a month.

    Replacing an entry that already exists requires a reason and supersedes it;
    there is no edit and no delete (AUD-1, AUD-6).
    """
    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="cost.actual.record",
        payload=body.model_dump(mode="json", exclude={"operation_id"}),
        perform=lambda: record_actual(
            session,
            ctx,
            RecordActualInput(
                cost_item_id=body.cost_item_id,
                period_month=body.period_month,
                amount_minor=body.amount_minor,
                currency=body.currency,
                currency_exponent=body.currency_exponent,
                invoice_reference=body.invoice_reference,
                note=body.note,
                correction_reason=body.correction_reason,
            ),
            operation_id=body.operation_id,
        ),
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


@cost_router.get("/summary")
def cost_summary_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("cost:read"))],
    month: date | None = None,
) -> dict:
    """One month: estimated, actual and variance per cost item, plus totals.

    ``month`` defaults to the tenant's current business month. Totals are per
    currency: provider prices are quoted in the provider's currency and V1
    converts nothing.
    """
    return month_summary(session, ctx, period_month=month_start(month or ctx.today))


@cost_router.get("/history")
def cost_history_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("cost:read"))],
    month: date | None = None,
    months: int = Query(default=12, ge=1, le=36),
) -> dict:
    """Month-by-month totals ending at ``month``, oldest first."""
    return month_history(
        session, ctx, latest_month=month_start(month or ctx.today), months=months
    )


@cost_router.post("/scenarios")
def cost_scenarios_route(
    body: CostScenarioRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("cost:read"))],
) -> dict:
    """Price a few planning cases against the rates currently configured.

    A read: it writes nothing, creates no usage row and appears in no total, so
    it carries no ``operation_id``. POST rather than GET only because the input
    is a list of cases, which is a body and not a query string — the same shape
    P0 §15 gives the (read-only) structured search route.
    """
    scenarios = [
        {
            "label": entry.label,
            "cost_item_id": entry.cost_item_id,
            "usage_quantity": (
                Decimal(entry.usage_quantity) if entry.usage_quantity is not None else None
            ),
            "events_per_day": entry.events_per_day,
            "seconds_per_event": (
                Decimal(entry.seconds_per_event)
                if entry.seconds_per_event is not None
                else None
            ),
            "days": entry.days,
        }
        for entry in body.scenarios
    ]
    return evaluate_scenarios(
        session,
        ctx,
        period_month=month_start(body.period_month or ctx.today),
        scenarios=scenarios,
    )


# --- reminders (P0 §10, §15) -------------------------------------------------
#
# Two routes, and neither of them decides anything a reminder depends on. The
# owner reads the work list and may re-dispatch a delivery that failed; who is
# eligible, which stage is due and what the amount is are the engine's, computed
# from the schedule and the ledger. There is no route that creates a reminder, no
# route that sets an amount, and no unauthenticated way to send anything.
#
# ``reminder:read`` and ``reminder:trigger`` are tenant capabilities the platform
# role does not hold, so a platform token is refused here exactly as an owner
# token is refused on the commission surface (SEC-5, SEC-6).


@reminder_router.get("")
def list_reminders_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("reminder:read"))],
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Who owes what, and where each customer is in this cycle's schedule.

    Every figure — the outstanding, the stage, the derived status — is computed
    here from the ledger and the tenant's own schedule. The client filters and
    renders; it recomputes nothing (SYN-9).
    """
    return reminder_overview(session, ctx, limit=limit, offset=offset)


@reminder_router.get("/{reminder_id}")
def get_reminder_route(
    reminder_id: uuid.UUID,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("reminder:read"))],
) -> dict:
    """One reminder and every delivery attempt made for it."""
    return reminder_detail(session, ctx, reminder_id)


@reminder_router.post("/{reminder_id}/send", response_model=OperationResponse)
def send_reminder_route(
    reminder_id: uuid.UUID,
    body: SendReminderRequest,
    ctx: TenantCtx,
    session: Db,
    provider: Annotated[CommunicationProvider, Depends(get_communication_provider)],
    _: Annotated[object, Depends(require_capability("reminder:trigger"))],
) -> OperationResponse:
    """Re-dispatch a reminder whose delivery failed (P0 §15).

    Not a way to send an extra reminder. It re-attempts a stage that already
    exists, and it goes through :func:`dispatch_reminder` like the cron does — so
    it re-reads the authoritative outstanding first and cancels instead of
    sending if the customer has since paid (REM-2, REM-4). A stage that already
    succeeded, or has exhausted its bounded attempts, comes back unchanged rather
    than sending a second message.
    """

    def perform():
        reminder = load_reminder_for_tenant(session, ctx, reminder_id)
        updated = dispatch_reminder(
            session,
            ctx,
            reminder,
            provider,
            operation_id=body.operation_id,
            manual=True,
        )
        return serialize_reminder(updated), "reminder", updated.id

    outcome = execute_idempotent(
        session,
        ctx,
        operation_id=body.operation_id,
        op_type="reminder.send",
        payload={"reminder_id": str(reminder_id)},
        perform=perform,
    )
    return OperationResponse(status=outcome.status, entity=outcome.result)


# --- internal jobs (P0 §12) --------------------------------------------------


@internal_job_router.post("/run-daily")
def run_daily_job_route(
    session: Db,
    clock: Annotated[Clock, Depends(get_clock)],
    provider: Annotated[CommunicationProvider, Depends(get_communication_provider)],
    _: Annotated[None, Depends(require_job_secret)],
) -> dict:
    """The host's cron entrypoint. One call, every active tenant, once a day.

    **It takes no tenant and no date.** The runner reads the active tenants
    itself and resolves each one's business date from its own timezone, so the
    shared secret grants the ability to *run the job*, never the ability to
    reach a chosen tenant's data — there is nowhere to name one.

    Safe to call twice: ``job_run`` makes a same-business-date repeat a no-op,
    and the reminder stage index makes a genuine overlap harmless anyway.
    """
    return run_daily(session, clock, provider)


# --- sync (P0 §7.3, §7.4) ----------------------------------------------------


@sync_router.post("/operations")
def sync_operations_route(
    body: SyncOperationsRequest,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("service:record"))],
) -> dict:
    """Push a batch of queued operations and return one verdict for each.

    Every entry is applied independently, in its own transaction, by the same
    domain command the online route calls (SYN-8). A rejection or a conflict on
    one entry is that entry's answer and nothing more: the others are unaffected.

    The capability is ``service:record`` because that is what the two supported
    operations require. There is no privileged sync capability, and admitting a
    further op type means checking *its* capability, not widening this one.
    """
    results = [
        apply_operation(
            session,
            ctx,
            SyncEnvelope(
                operation_id=envelope.operation_id,
                op_type=envelope.op_type,
                payload=envelope.payload,
                client_created_at=envelope.client_created_at,
            ),
        )
        for envelope in body.operations
    ]
    return {"results": [r.to_json() for r in results]}


@sync_router.get("/changes")
def sync_changes_route(
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("customer:read"))],
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
) -> dict:
    """Tenant-scoped rows with ``row_version > since``, plus the next cursor."""
    return changes_since(session, ctx, since=since, limit=limit)
