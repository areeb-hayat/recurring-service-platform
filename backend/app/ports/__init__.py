"""Port (Protocol) definitions.

P0 §2.1 freezes four ports: ``CommunicationProvider``, ``SpeechToTextProvider``,
``SearchInterpreter`` and ``OperationalIntentInterpreter``.

Each is declared by the package that first needs it, never earlier: a Protocol
with no implementation and no caller is speculative code. P7 declares the first
one — :mod:`app.ports.comms` — because the reminder engine has something real to
deliver. ``SpeechToTextProvider`` (P9) and the two interpreter ports (P8, P9)
are still absent, and ``tests/test_architecture.py`` asserts they stay absent.

The architecture guard also enforces the domain -> adapters prohibition
(A-SLOT-5): domain modules import from here, and only ``app/api`` and
``app/adapters`` know which implementation is wired in.
"""

from __future__ import annotations

from app.ports.comms import (
    Channel,
    CommsCapabilities,
    CommunicationProvider,
    DeliveryReceipt,
    DeliveryState,
    DeliveryUpdate,
    OutboundMessage,
    TEMPLATE_KEYS,
)

__all__ = [
    "Channel",
    "CommsCapabilities",
    "CommunicationProvider",
    "DeliveryReceipt",
    "DeliveryState",
    "DeliveryUpdate",
    "OutboundMessage",
    "TEMPLATE_KEYS",
]
