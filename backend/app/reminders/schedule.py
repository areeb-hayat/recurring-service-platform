"""The reminder schedule, in one place (REM-1).

**The days are data, not constants.** ``1 / 4 / 8 / 12 / 15`` is the frozen
*default*, stored on ``tenant.reminder_schedule`` when a tenant is provisioned
(see ``app.tenancy.models.DEFAULT_REMINDER_SCHEDULE``). Nothing in the engine,
the API or the UI writes a day number down; every one of them asks this module.
Making the schedule tenant-configurable later is a write path onto a column that
already exists, not a redesign — and P7 deliberately does not build that write
path, because no brief asks for it and a workflow designer is not a reminder
engine.

**Per-customer schedules do not exist.** The schedule belongs to the tenant. A
customer-level override would multiply the stage arithmetic by the customer
count for a requirement nobody has stated.

**Invalid configuration fails loudly.** A tenant row whose schedule is malformed
raises rather than falling back to the default: silently reminding on days the
owner did not configure is worse than not reminding at all, and it would hide
the mistake for a month.

The one piece of real logic here is :func:`due_stage`, which is REM-8's
definition of catch-up and the reason an outage costs the intermediate nudges
instead of delivering them all at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.core.errors import ValidationFailed
from app.reminders.models import ReminderKind

__all__ = [
    "Stage",
    "SCHEDULE_KINDS",
    "load_schedule",
    "due_stage",
    "stage_on_day",
    "next_stage_after",
    "serialize_schedule",
]

#: The kinds a *schedule entry* may carry. ``OWNER_ALERT`` is not one of them:
#: P0 §10 pairs the owner alert with the ``FINAL`` stage rather than giving it a
#: day of its own, so it is derived and never configured.
SCHEDULE_KINDS: frozenset[str] = frozenset(
    {ReminderKind.STATEMENT, ReminderKind.REMINDER, ReminderKind.FINAL}
)


@dataclass(frozen=True, slots=True)
class Stage:
    """One configured step of the monthly schedule."""

    day: int
    kind: str

    @property
    def is_final(self) -> bool:
        return self.kind == ReminderKind.FINAL

    def as_dict(self) -> dict[str, Any]:
        return {"day": self.day, "kind": self.kind}


def load_schedule(raw: Any) -> tuple[Stage, ...]:
    """Validate and normalise a tenant's ``reminder_schedule`` column.

    Days are bounded to 1..28 for the same reason ``tenant.cycle_start_day`` is:
    a stage on the 31st simply does not occur in February, and a schedule that
    silently skips a month is worse than one that was refused at write time.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise ValidationFailed(
            "tenant reminder_schedule must be a non-empty list of {day, kind} entries"
        )

    stages: list[Stage] = []
    seen_days: set[int] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValidationFailed(f"reminder schedule entry is not an object: {entry!r}")
        day = entry.get("day")
        kind = entry.get("kind")
        if isinstance(day, bool) or not isinstance(day, int):
            raise ValidationFailed(f"reminder schedule day must be an integer (got {day!r})")
        if not 1 <= day <= 28:
            raise ValidationFailed(
                f"reminder schedule day must be between 1 and 28 (got {day}); "
                "a day above 28 does not occur in every month"
            )
        if kind not in SCHEDULE_KINDS:
            raise ValidationFailed(
                f"reminder schedule kind must be one of {sorted(SCHEDULE_KINDS)} "
                f"(got {kind!r})"
            )
        if day in seen_days:
            raise ValidationFailed(f"reminder schedule has two entries for day {day}")
        seen_days.add(day)
        stages.append(Stage(day=day, kind=kind))

    ordered = tuple(sorted(stages, key=lambda s: s.day))
    finals = [s for s in ordered if s.is_final]
    if len(finals) > 1:
        raise ValidationFailed("reminder schedule has more than one FINAL stage")
    if finals and finals[0].day != ordered[-1].day:
        raise ValidationFailed("the FINAL stage must be the last stage in the schedule")
    return ordered


def due_stage(schedule: Iterable[Stage], day_of_month: int) -> Stage | None:
    """REM-8: the highest configured stage on or before ``day_of_month``.

    This single line is the whole of catch-up. It is the ordinary path, not a
    special case for outages — which is exactly why an outage cannot produce a
    burst. A missed day cannot be re-run "for its own business date", because by
    the time the host comes back the tenant-local date has already moved on, so
    the only question ever asked is "what is due *today*".

        days 4-8 missed, running on day 9  ->  the day-8 stage, alone
        down through day 15, running on 16 ->  the day-15 FINAL, alone

    Returns ``None`` before the first configured day of the month.
    """
    latest: Stage | None = None
    for stage in schedule:
        if stage.day <= day_of_month and (latest is None or stage.day > latest.day):
            latest = stage
    return latest


def stage_on_day(schedule: Iterable[Stage], day: int) -> Stage | None:
    """The stage configured for exactly ``day``, if any."""
    for stage in schedule:
        if stage.day == day:
            return stage
    return None


def next_stage_after(schedule: Iterable[Stage], day: int) -> Stage | None:
    """The next stage a customer can expect after ``day``, for the owner's screen.

    Purely informational: nothing decides anything from it, and it says nothing
    about whether that stage will actually be sent — a payment before then stops
    it (REM-4).
    """
    upcoming: Stage | None = None
    for stage in schedule:
        if stage.day > day and (upcoming is None or stage.day < upcoming.day):
            upcoming = stage
    return upcoming


def serialize_schedule(schedule: Iterable[Stage]) -> list[dict[str, Any]]:
    return [stage.as_dict() for stage in schedule]
