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
    CorrectServiceRequest,
    CreateCustomerRequest,
    LoginRequest,
    LogoutRequest,
    OperationResponse,
    RecordServiceRequest,
    RefreshRequest,
    TokenResponse,
    UpdateCustomerRequest,
    VoidServiceRequest,
)
from app.billing.ledger import outstanding_minor
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
    # FIN-4: derived on read, never stored.
    data["outstanding_minor"] = outstanding_minor(session, ctx, customer.id)
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
