"""Communication adapters, and the one factory that chooses between them.

Exactly one implementation exists: :class:`MockCommunicationProvider`. P0 §9
makes it the development and test default, and P7's scope stops here — the real
transport (WhatsApp, and SMS if the business wants it) is P10, arrives as
another class in this package, and changes nothing above the port.

``COMMS_PROVIDER`` selects by name. An unknown name fails loudly at startup
rather than silently falling back to the mock: a production deployment that
believes it is sending real messages and is not would be the worst possible
failure mode for a dunning system.
"""

from __future__ import annotations

from app.adapters.comms.mock import MockCommunicationProvider
from app.ports.comms import CommunicationProvider

__all__ = ["MockCommunicationProvider", "build_communication_provider"]

_PROVIDERS = {"mock": MockCommunicationProvider}


def build_communication_provider(name: str) -> CommunicationProvider:
    key = (name or "mock").strip().lower()
    factory = _PROVIDERS.get(key)
    if factory is None:
        raise RuntimeError(
            f"COMMS_PROVIDER={name!r} is not a known communication provider "
            f"(available: {', '.join(sorted(_PROVIDERS))}). Real message "
            "transport is not implemented yet."
        )
    return factory()
