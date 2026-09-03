"""The operation envelope's vocabulary and its payload models (P0 §7.2).

**Why the payload model lives here and not in ``app/api``.** An operation is not
the property of one HTTP route. The same CONFIRM can reach the server as an
ordinary ``POST /service/records`` body or as one envelope inside a
``POST /sync/operations`` batch, and SYN-8 says the two must pass *exactly* the
same validation — "there is no privileged offline path". Defining the payload
once here, and letting the online request schema extend it with
``operation_id``, is what makes that true structurally instead of by two
copies staying accidentally identical.

**Offline write scope in V1.** P0 §7.2's ``op_type`` enumeration is the envelope's
*extensible* vocabulary; the set a device may queue for offline execution is
narrower on purpose. V1 guarantees offline CONFIRM and SKIP and nothing else —
see the dated clarification in P0 §7.2. Payments, corrections, voids and customer
edits are online-only operations today; they keep the same envelope shape, so
admitting one later is a registry entry here, not a redesign.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.core.schema_types import QuantityStr

__all__ = [
    "SyncOpType",
    "SUPPORTED_OP_TYPES",
    "ServiceOperationPayload",
    "op_type_for_kind",
]


class SyncOpType:
    """The op types a device may queue in V1."""

    SERVICE_RECORD = "service.record"
    SERVICE_SKIP = "service.skip"


SUPPORTED_OP_TYPES: frozenset[str] = frozenset(
    {SyncOpType.SERVICE_RECORD, SyncOpType.SERVICE_SKIP}
)


def op_type_for_kind(kind: str) -> str:
    """The op type a given service ``kind`` registers under.

    Both transports must agree, or the register would treat one transport's
    replay of the other's operation as an ``operation_id`` reused for a different
    request (SYN-14) and refuse a retry that is in fact identical.
    """
    return SyncOpType.SERVICE_SKIP if kind == "SKIP" else SyncOpType.SERVICE_RECORD


class ServiceOperationPayload(BaseModel):
    """The body of a CONFIRM or a SKIP, whichever transport carries it."""

    model_config = ConfigDict(extra="forbid")  # unknown fields are a caller bug

    customer_id: uuid.UUID
    kind: Literal["SERVICE", "SKIP"] = "SERVICE"
    quantity: QuantityStr | None = None
    # Omit for "today": the server resolves the tenant's business date (R4).
    #
    # A queued offline operation *does* send it, and sends the business date the
    # server itself last reported — never a date derived from the device clock.
    # That is what stops an entry made on Tuesday and synchronised on Wednesday
    # from being silently refiled under Wednesday. ``validate_service_date``
    # applies the same single rule either way: not in the future.
    service_date: date | None = None
    # Provenance only (VOI-8), never behaviour.
    input_method: Literal["BUTTON", "VOICE"] = "BUTTON"
