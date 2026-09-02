"""Request/response models.

Money crosses the wire as an integer count of minor units in a ``*_minor`` field
(FIN-1). Quantity crosses as a **string**, decoded to ``Decimal`` — never as a
JSON float, which cannot represent 0.1 exactly.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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
# Quantity as a string: "2", "1.5", "0.333". Rejects JSON floats outright.
QuantityStr = Annotated[str, StringConstraints(strip_whitespace=True, max_length=24)]


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


class RecordServiceRequest(_Base):
    operation_id: uuid.UUID
    customer_id: uuid.UUID
    kind: Literal["SERVICE", "SKIP"] = "SERVICE"
    quantity: QuantityStr | None = None
    # Omit for "today". The server resolves today from the tenant timezone (R4);
    # an explicit date is validated separately, never inferred from client time.
    service_date: date | None = None
    # Provenance only (VOI-8). P1 exposes no voice route; VOICE is accepted here
    # so the column and its behaviour-neutrality are testable before P9.
    input_method: Literal["BUTTON", "VOICE"] = "BUTTON"


class CorrectServiceRequest(_Base):
    operation_id: uuid.UUID
    quantity: QuantityStr | None = None
    kind: Literal["SERVICE", "SKIP"] = "SERVICE"
    reason: NonEmptyStr  # AUD-6
    input_method: Literal["BUTTON", "VOICE"] = "BUTTON"


class VoidServiceRequest(_Base):
    operation_id: uuid.UUID
    reason: NonEmptyStr  # AUD-6


class OperationResponse(_Base):
    """Every mutation reports whether it applied or replayed (SYN-2)."""

    status: Literal["APPLIED", "DUPLICATE"]
    entity: dict[str, Any]
