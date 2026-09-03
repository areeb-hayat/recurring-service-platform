"""The in-memory communication provider (P0 §9).

The development and test default, and the only implementation P7 ships. It makes
no network call — ``tests/test_architecture.py`` still asserts no HTTP client is
imported anywhere in ``app/`` — and it records every message so a test can assert
on exactly what would have left the building.

It can be told to fail, because the interesting reminder tests are the failure
ones: a provider outage must leave every financial row untouched (REM-6), must
never mark a reminder ``SENT``, and must leave a durable ``FAILED`` record rather
than losing the attempt.

Deliberately deterministic. ``provider_message_id`` is derived from the message's
own idempotency key, so a retry of an uncertain delivery is visibly the same
logical delivery rather than a new one.
"""

from __future__ import annotations

from typing import Mapping

from app.ports.comms import (
    Channel,
    CommsCapabilities,
    DeliveryReceipt,
    DeliveryState,
    DeliveryUpdate,
    OutboundMessage,
)

__all__ = ["MockCommunicationProvider"]


class MockCommunicationProvider:
    """Records messages in memory; never sends one anywhere."""

    name = "mock"
    capabilities = CommsCapabilities(
        channels=frozenset({Channel.WHATSAPP, Channel.SMS, Channel.EMAIL}),
        supports_templates=True,
        supports_delivery_receipts=False,
    )

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        #: Set to make every send fail, or raise, for outage tests.
        self.fail_with: str | None = None
        self.raise_with: Exception | None = None

    def send(self, message: OutboundMessage) -> DeliveryReceipt:
        self.sent.append(message)
        if self.raise_with is not None:
            raise self.raise_with
        if message.channel not in self.capabilities.channels:
            return DeliveryReceipt(
                state=DeliveryState.FAILED,
                provider=self.name,
                error=f"channel {message.channel} not supported",
            )
        if self.fail_with is not None:
            return DeliveryReceipt(
                state=DeliveryState.FAILED, provider=self.name, error=self.fail_with
            )
        return DeliveryReceipt(
            state=DeliveryState.ACCEPTED,
            provider=self.name,
            provider_message_id=f"mock-{message.idempotency_key}",
        )

    def parse_delivery_callback(
        self, headers: Mapping[str, str], raw_body: bytes
    ) -> DeliveryUpdate | None:
        """No callback route exists in V1, so there is nothing to parse."""
        return None

    # --- test helpers --------------------------------------------------------

    def reset(self) -> None:
        self.sent.clear()
        self.fail_with = None
        self.raise_with = None
