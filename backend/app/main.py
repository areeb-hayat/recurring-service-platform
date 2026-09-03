"""FastAPI application factory and the single error envelope.

Every error leaves through :func:`domain_error_handler` in the frozen shape
(P0 §15)::

    {"error": {"code": "...", "detail": "...", "field_errors": {...}}}
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    auth_router,
    billing_router,
    cost_router,
    customer_router,
    dashboard_router,
    internal_job_router,
    payment_router,
    platform_commission_router,
    reminder_router,
    service_router,
    statement_router,
    sync_router,
    tenant_router,
)
from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.db_models import import_all_models

__all__ = ["create_app"]

import_all_models()  # ensure every table is registered on Base.metadata


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Recurring Service, Billing & Collection Platform",
        version="0.1.0",
        description=(
            "P1 backend foundation + P2 financial engine + P3 commission engine "
            "+ P5 sync + P6 operating costs + P7 reminder engine."
        ),
    )
    app.state.settings = settings

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(DomainError)
    def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    @app.exception_handler(RequestValidationError)
    def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        field_errors = {
            ".".join(str(p) for p in err["loc"][1:]) or "body": err["msg"]
            for err in exc.errors()
        }
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION",
                    "detail": "request validation failed",
                    "field_errors": field_errors,
                }
            },
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_router)
    app.include_router(tenant_router)
    app.include_router(customer_router)
    app.include_router(service_router)
    app.include_router(billing_router)
    app.include_router(statement_router)
    app.include_router(payment_router)
    app.include_router(dashboard_router)
    app.include_router(cost_router)
    app.include_router(reminder_router)
    app.include_router(sync_router)
    app.include_router(internal_job_router)
    app.include_router(platform_commission_router)
    return app


app = create_app  # factory, not an instance: uvicorn app.main:app --factory
