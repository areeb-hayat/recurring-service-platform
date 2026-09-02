"""Identifier generation.

P0 §6 freezes ids as UUIDv7: time-ordered (index-friendly, naturally sorted by
creation), generatable on a device while offline, and not enumerable across
tenants.

Python 3.12/3.13 has no ``uuid.uuid7``, and the implementation is ~15 lines, so
it lives here rather than pulling in a dependency (RFC 9562 §5.7).
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["uuid7", "new_id"]


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7: 48-bit big-endian millisecond timestamp + 74 random bits."""
    timestamp_ms = time.time_ns() // 1_000_000
    raw = bytearray(16)
    raw[0:6] = timestamp_ms.to_bytes(6, "big")
    raw[6:16] = os.urandom(10)
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(raw))


# Alias used by model defaults so the choice of scheme is swappable in one place.
new_id = uuid7
