"""Deterministic time and the authoritative business-date rule.

Resolves P0 risk R4. The rules frozen here:

* Instants are UTC, always timezone-aware.
* A **business date** ("today") is derived by the *server* from the *tenant's*
  timezone. A device clock can never choose it.
* ``client_created_at`` is advisory metadata only; nothing authoritative reads it.
* An explicitly supplied historical service date is validated separately
  (:func:`validate_service_date`) rather than inferred from client time.

The clock is injected so tests can freeze time and exercise midnight boundaries.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone  # timedelta: FixedClock.advance
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "Clock",
    "SystemClock",
    "FixedClock",
    "DEFAULT_TIMEZONE",
    "resolve_timezone",
    "business_date",
    "validate_business_date",
    "validate_service_date",
]

# Tenant default until the client confirms otherwise (P0 §16 assumed defaults).
# Always read from the tenant row in practice; this is only the column default.
DEFAULT_TIMEZONE = "Asia/Karachi"


class Clock(Protocol):
    """Source of the current instant. Always UTC and timezone-aware."""

    def now_utc(self) -> datetime: ...


class SystemClock:
    """Real wall clock. The only implementation used outside tests."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Test clock. Frozen unless explicitly advanced or set."""

    def __init__(self, now: datetime) -> None:
        self.set(now)

    def now_utc(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = now.astimezone(timezone.utc)

    def advance(self, **delta: float) -> None:
        self.set(self._now + timedelta(**delta))


def resolve_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name, failing loudly on an unknown zone."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"unknown timezone: {name!r}") from exc


def business_date(now_utc: datetime, tenant_timezone: str) -> date:
    """The tenant's current business date.

    This is the *only* way "today" is determined for a write. Recording at
    23:59 local time and again at 00:01 local time therefore lands on two
    different service dates, while two devices in different physical timezones
    recording at the same instant always agree.
    """
    if now_utc.tzinfo is None:
        raise ValueError("business_date requires a timezone-aware instant")
    return now_utc.astimezone(resolve_timezone(tenant_timezone)).date()


def validate_business_date(requested: date, *, today: date, field: str) -> date:
    """Validate an explicitly requested (historical) business date.

    Separate from :func:`business_date` on purpose: an explicit date is a
    deliberate act that gets its own check, never an inference from the caller's
    clock.

    The **only** V1 rule is that it may not be in the future relative to the
    tenant-local business date. There is deliberately no maximum historical age:
    a backdate window would be an invented product policy. Cycle close does not
    add one either — P0 §11.1 ends with "no period locking beyond cycle close",
    and a date inside a closed period is still accepted; it simply posts to the
    open cycle (§5.5). Do not add a window without a client decision.
    """
    if requested > today:
        raise ValueError(
            f"{field} {requested.isoformat()} is in the future "
            f"(tenant business date is {today.isoformat()})"
        )
    return requested


def validate_service_date(requested: date, *, today: date) -> date:
    """The :func:`validate_business_date` rule, applied to a service date."""
    return validate_business_date(requested, today=today, field="service_date")
