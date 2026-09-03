"""The ``CommunicationProvider`` port (P0 §2.1, §9).

**What this boundary is for.** The application decides *who* is reminded, *which*
stage is due, *what* the amount is, and *whether* a reminder must be suppressed.
A delivery provider decides exactly one thing: whether a message left the
building. Nothing behind this port may compute a balance, resolve a stage, read
a payment, or judge eligibility — REM-7, and the reason the contract carries a
finished string rather than a number and a rule.

**Provider-neutral on purpose.** There is no WhatsApp field, no template body, no
vendor message shape and no channel-specific option anywhere in these types. A
message names a *semantic* template key and a bag of already-rendered
parameters; which vendor, which transport and which template body those become
is an adapter's problem and P10's decision. SMS is expected to arrive as another
:class:`Channel` value against this same contract, not as a second port.

**Delivery outcome is not a business outcome.** :class:`DeliveryReceipt` is the
only thing a provider returns, and P0 §9's failure isolation is what the caller
does with it: an outage writes to ``communication_log`` and can never move a
balance, a statement, a payment or a commission row (REM-6).

``idempotency_key`` is the reminder's own id. A retry of an uncertain delivery
carries the same key, so a provider that supports deduplication has the identity
it needs and one that does not is no worse off than before.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol, runtime_checkable

__all__ = [
    "Channel",
    "DeliveryState",
    "CommsCapabilities",
    "OutboundMessage",
    "DeliveryReceipt",
    "DeliveryUpdate",
    "CommunicationProvider",
    "TEMPLATE_KEYS",
]


class Channel:
    """Transports a message may travel over.

    ``SMS`` is listed because P10 is expected to want it and a channel value
    costs nothing; no SMS gateway, modem or relay exists anywhere in this
    codebase and none is implied by this name.
    """

    WHATSAPP = "WHATSAPP"
    SMS = "SMS"
    EMAIL = "EMAIL"

    ALL = frozenset({WHATSAPP, SMS, EMAIL})


class DeliveryState:
    """The lifecycle of one delivery attempt.

    ``ACCEPTED`` means the provider took responsibility for the message.
    ``DELIVERED`` means it confirmed arrival, which only a provider with
    delivery receipts can ever report. Both count as sent; ``FAILED`` never does
    and is never quietly upgraded.
    """

    QUEUED = "QUEUED"
    ACCEPTED = "ACCEPTED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"

    ALL = frozenset({QUEUED, ACCEPTED, DELIVERED, FAILED})
    SUCCESSFUL = frozenset({ACCEPTED, DELIVERED})


#: The semantic templates the reminder engine can ask for. A key names an
#: *intent*, never a body: the wording, the language and the vendor template id
#: belong to an adapter and to the business, not to this contract.
TEMPLATE_KEYS: frozenset[str] = frozenset(
    {
        "statement.issued",
        "payment.reminder",
        "payment.reminder.final",
        "owner.final_alert",
    }
)


@dataclass(frozen=True, slots=True)
class CommsCapabilities:
    """What a provider can actually do, so the caller never assumes."""

    channels: frozenset[str]
    supports_templates: bool = True
    supports_delivery_receipts: bool = False


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """One semantic message, ready to deliver.

    ``params`` must be **strings only**, and that is enforced here rather than
    left to a reviewer: an int would be a number for a provider to format, and a
    formatted amount is the whole of REM-7. A key ending in ``_minor`` is
    rejected for the same reason — a raw minor-unit balance must never cross
    this boundary.
    """

    tenant_id: uuid.UUID
    channel: str
    to: str
    template_key: str
    params: Mapping[str, str]
    idempotency_key: uuid.UUID
    #: The customer the message is *about*. Present on an owner alert too, where
    #: the recipient is the owner but the subject is still a customer.
    customer_id: uuid.UUID | None = None
    #: Traceability back into reminder history; never used to decide anything.
    reference: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.channel not in Channel.ALL:
            raise ValueError(f"unknown channel: {self.channel!r}")
        if self.template_key not in TEMPLATE_KEYS:
            raise ValueError(f"unknown template key: {self.template_key!r}")
        if not self.to:
            raise ValueError("outbound message has no destination")
        for key, value in self.params.items():
            if not isinstance(value, str):
                raise ValueError(
                    f"message param {key!r} must be an already-rendered string "
                    f"(got {type(value).__name__}); providers never format money"
                )
            if key.endswith("_minor"):
                raise ValueError(
                    f"message param {key!r} would send a raw minor-unit amount; "
                    "send the rendered string instead (REM-7)"
                )


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """A provider's answer about one attempt, and nothing more."""

    state: str
    provider: str
    provider_message_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in DeliveryState.ALL:
            raise ValueError(f"unknown delivery state: {self.state!r}")

    @property
    def succeeded(self) -> bool:
        return self.state in DeliveryState.SUCCESSFUL


@dataclass(frozen=True, slots=True)
class DeliveryUpdate:
    """An asynchronous status change a provider reports later.

    Declared because the port is the shape P10 must satisfy, and a provider with
    delivery receipts has nowhere else to put one. Nothing in P7 receives a
    callback: there is no route, no adapter and no parser for one yet.
    """

    provider_message_id: str
    state: str
    occurred_at: datetime | None = None
    error: str | None = None


@runtime_checkable
class CommunicationProvider(Protocol):
    """P0 §9. Two methods, and neither returns a business decision."""

    name: str
    capabilities: CommsCapabilities

    def send(self, message: OutboundMessage) -> DeliveryReceipt: ...

    def parse_delivery_callback(
        self, headers: Mapping[str, str], raw_body: bytes
    ) -> DeliveryUpdate | None: ...
