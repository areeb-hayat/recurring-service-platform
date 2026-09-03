"""Request/response models.

Money crosses the wire as an integer count of minor units in a ``*_minor`` field
(FIN-1). Quantity crosses as a **string**, decoded to ``Decimal`` — never as a
JSON float, which cannot represent 0.1 exactly.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.schema_types import QuantityStr
from app.sync.envelope import ServiceOperationPayload

__all__ = [
    "LoginRequest",
    "RefreshRequest",
    "LogoutRequest",
    "TokenResponse",
    "CreateCustomerRequest",
    "UpdateCustomerRequest",
    "RecordServiceRequest",
    "CorrectServiceRequest",
    "VoidServiceRequest",
    "CloseCycleRequest",
    "RecordPaymentRequest",
    "VoidPaymentRequest",
    "CreateCommissionPlanRequest",
    "RecordCommissionSettlementRequest",
    "SyncOperationEnvelope",
    "SyncOperationsRequest",
    "OperationResponse",
]

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
# Format validation only (SEC §22). A pattern rather than pydantic's EmailStr so
# the backend does not take on email-validator + dnspython for one field.
EmailStr = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$",
    ),
]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields are a caller bug


# --- auth -------------------------------------------------------------------


class LoginRequest(_Base):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    device_label: str | None = Field(default=None, max_length=120)


class RefreshRequest(_Base):
    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(_Base):
    refresh_token: str = Field(min_length=1, max_length=512)


class TokenResponse(_Base):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    role: str
    scope: str
    tenant_id: str | None


# --- customers --------------------------------------------------------------


class CreateCustomerRequest(_Base):
    operation_id: uuid.UUID
    code: NonEmptyStr
    name: NonEmptyStr
    phone_e164: str | None = Field(default=None, max_length=20)
    whatsapp_e164: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=1000)
    area: str | None = Field(default=None, max_length=120)
    default_quantity: QuantityStr = "0"
    unit_price_minor: int = Field(default=0, ge=0)


class UpdateCustomerRequest(_Base):
    operation_id: uuid.UUID
    name: NonEmptyStr | None = None
    phone_e164: str | None = Field(default=None, max_length=20)
    whatsapp_e164: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=1000)
    area: str | None = Field(default=None, max_length=120)
    default_quantity: QuantityStr | None = None
    unit_price_minor: int | None = Field(default=None, ge=0)
    status: Literal["ACTIVE", "INACTIVE"] | None = None
    expected_row_version: int | None = None


# --- daily service ----------------------------------------------------------


class RecordServiceRequest(ServiceOperationPayload):
    """The online transport for the same operation a device queues offline.

    It *extends* the shared payload model rather than restating it, so SYN-8 —
    "synchronised operations pass exactly the same validation as online ones" —
    cannot drift: there is one definition of what a CONFIRM or a SKIP contains,
    and both transports validate against it. ``operation_id`` is the only field
    the HTTP body adds, because in a sync batch it belongs to the envelope.
    """

    operation_id: uuid.UUID


class CorrectServiceRequest(_Base):
    operation_id: uuid.UUID
    quantity: QuantityStr | None = None
    kind: Literal["SERVICE", "SKIP"] = "SERVICE"
    reason: NonEmptyStr  # AUD-6
    input_method: Literal["BUTTON", "VOICE"] = "BUTTON"


class VoidServiceRequest(_Base):
    operation_id: uuid.UUID
    reason: NonEmptyStr  # AUD-6


# --- billing and payments ----------------------------------------------------


class CloseCycleRequest(_Base):
    operation_id: uuid.UUID


class RecordPaymentRequest(_Base):
    operation_id: uuid.UUID
    customer_id: uuid.UUID
    # FIN-1: an integer count of minor units on the wire, never a decimal string
    # and never a JSON float. PAY-3: strictly positive; there is no upper bound
    # and no clamp to the outstanding balance, because an overpayment is a legal
    # credit (FIN-10), not an error.
    amount_minor: int = Field(gt=0)
    method: Literal["CASH", "BANK_TRANSFER", "OTHER"] = "CASH"
    # Omit for "today", resolved server-side from the tenant timezone (R4).
    received_on: date | None = None
    reference: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


class VoidPaymentRequest(_Base):
    operation_id: uuid.UUID
    reason: NonEmptyStr  # AUD-6


# --- platform commission (P0 §11; platform scope only) -----------------------
#
# ``tenant_id`` is in the body rather than derived from the token *because* these
# are platform routes: the platform principal has no tenant of its own, so the
# target tenant is an explicit choice it makes and is auditable as such. That is
# the exact opposite of a tenant route, where reading the tenant from the request
# would be the SEC-3 defect.


class CreateCommissionPlanRequest(_Base):
    operation_id: uuid.UUID
    tenant_id: uuid.UUID
    basis: Literal["RECORDED_VALUE", "BILLED_VALUE", "COLLECTED_VALUE", "PER_EVENT"]
    # COM-9: an integer count of basis points, 0..10000. Never a decimal rate and
    # never a float. Exactly one of these two is set, decided by the basis.
    rate_bp: int | None = Field(default=None, ge=0, le=10000)
    fixed_amount_minor: int | None = Field(default=None, ge=0)
    # Omit to use the tenant's own currency; it may not be anything else.
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    effective_from: date
    effective_to: date | None = None


class RecordCommissionSettlementRequest(_Base):
    operation_id: uuid.UUID
    tenant_id: uuid.UUID
    period_start: date
    period_end: date
    # FIN-1: integer minor units, and strictly positive — a settlement is money
    # that moved. A negative row would be a commission adjustment in disguise,
    # bypassing the snapshotted terms and source traceability COM-4 requires.
    # Over-settlement is unaffected: 1200 against 1000 earned is a positive row
    # that leaves outstanding at -200 (A-COM-6b).
    amount_minor: int = Field(gt=0)
    # Omit for "today", resolved server-side from the tenant timezone (R4).
    settled_on: date | None = None
    reference: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


# --- sync (P0 §7.2, §7.3) ----------------------------------------------------


class SyncOperationEnvelope(_Base):
    """One queued operation, exactly as P0 §7.2 froze the envelope.

    ``payload`` is a plain object rather than a discriminated union because the
    dispatcher validates it against the model for its ``op_type`` and returns a
    per-operation ``REJECTED`` when it does not fit. A batch of fifty entries
    must not be refused wholesale because one of them is malformed.

    ``client_created_at`` is carried because P0 §7.2 defines it, and is advisory
    only: nothing authoritative reads a device clock (R4).
    """

    operation_id: uuid.UUID
    op_type: str = Field(max_length=64)
    payload: dict[str, Any]
    client_created_at: datetime | None = None
    base_row_version: int | None = None


class SyncOperationsRequest(_Base):
    """A push batch. Bounded so one request cannot hold a transaction open all day."""

    operations: list[SyncOperationEnvelope] = Field(min_length=1, max_length=200)


class OperationResponse(_Base):
    """Every mutation reports whether it applied or replayed (SYN-2)."""

    status: Literal["APPLIED", "DUPLICATE"]
    entity: dict[str, Any]
