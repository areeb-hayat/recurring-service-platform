"""HTTP routers. Thin by design: authenticate, validate, call the domain, serialize.

No business rule lives here. Every mutation carries an ``operation_id`` and runs
through :func:`app.sync.idempotency.execute_idempotent`, so the online path and
P5's bulk sync path share one guarantee rather than two implementations.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    Db,
    TenantCtx,
    get_app_settings,
    get_clock,
    require_capability,
)
from app.api.schemas import (
    CloseCycleRequest,
    CorrectServiceRequest,
    CreateCustomerRequest,
    LoginRequest,
    LogoutRequest,
    OperationResponse,
    RecordPaymentRequest,
    RecordServiceRequest,
    RefreshRequest,
    TokenResponse,
    UpdateCustomerRequest,
    VoidPaymentRequest,
    VoidServiceRequest,
)
from app.billing.cycles import close_cycle, list_cycles, serialize_cycle
from app.billing.ledger import outstanding_minor
from app.billing.reporting import customer_payment_status
from app.billing.statements import (
    list_statements,
    load_statement,
    serialize_statement,
)
from app.core.clock import Clock
from app.core.config import Settings
from app.customers.commands import (
    CreateCustomerInput,
    UpdateCustomerInput,
    create_customer,
    get_customer,
    list_customers,
    serialize_customer,
    update_customer,
)
from app.identity import service as auth_service
from app.payments.commands import (
    RecordPaymentInput,
    VoidPaymentInput,
    record_payment,
    void_payment,
)
from app.service.commands import (
    CorrectServiceInput,
    RecordServiceInput,
    VoidServiceInput,
    correct_service,
    list_day,
    record_service,
    serialize_record,
    void_service,
)
from app.sync.idempotency import execute_idempotent
from app.tenancy.context import TenantContext

auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
customer_router = APIRouter(prefix="/api/v1/customers", tags=["customers"])
service_router = APIRouter(prefix="/api/v1/service", tags=["service"])
billing_router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
statement_router = APIRouter(prefix="/api/v1/statements", tags=["billing"])
payment_router = APIRouter(prefix="/api/v1/payments", tags=["payments"])

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
    return {"items": [serialize_customer(c, ctx) for c in rows]}


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
    data = serialize_customer(customer, ctx)
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
        op_type="service.record",
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


@statement_router.get("/{statement_id}")
def get_statement_route(
    statement_id: uuid.UUID,
    ctx: TenantCtx,
    session: Db,
    _: Annotated[object, Depends(require_capability("billing:read"))],
) -> dict:
    return serialize_statement(load_statement(session, ctx, statement_id), ctx)


# --- payments ---------------------------------------------------------------


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
