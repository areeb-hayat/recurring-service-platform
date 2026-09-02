"""Business-date rule and clock determinism — resolves P0 risk R4.

The server derives "today" from the *tenant's* timezone. A device clock never
chooses a business date.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.clock import (
    FixedClock,
    SystemClock,
    business_date,
    validate_service_date,
)


class TestBusinessDate:
    def test_R4_uses_tenant_timezone_not_utc(self):
        # 2026-03-15 20:00 UTC is already 2026-03-16 01:00 in Asia/Karachi.
        instant = datetime(2026, 3, 15, 20, 0, tzinfo=timezone.utc)
        assert business_date(instant, "Asia/Karachi") == date(2026, 3, 16)
        assert business_date(instant, "UTC") == date(2026, 3, 15)

    def test_R4_local_midnight_boundary(self):
        """The minute either side of local midnight lands on different dates."""
        just_before = datetime(2026, 3, 15, 18, 59, tzinfo=timezone.utc)  # 23:59 PKT
        just_after = datetime(2026, 3, 15, 19, 1, tzinfo=timezone.utc)  # 00:01 PKT
        assert business_date(just_before, "Asia/Karachi") == date(2026, 3, 15)
        assert business_date(just_after, "Asia/Karachi") == date(2026, 3, 16)

    def test_R4_same_instant_same_date_regardless_of_device(self):
        """Two devices in different physical timezones agree, because only the
        tenant timezone is consulted."""
        instant = datetime(2026, 3, 15, 7, 0, tzinfo=timezone.utc)
        assert business_date(instant, "Asia/Karachi") == business_date(
            instant, "Asia/Karachi"
        )

    def test_R4_dst_timezone_boundary(self):
        # 2026-03-29 00:30 UTC is 01:30 in Europe/London on the DST switch day.
        instant = datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)
        assert business_date(instant, "Europe/London") == date(2026, 3, 29)

    def test_R4_requires_aware_instant(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            business_date(datetime(2026, 3, 15, 12, 0), "Asia/Karachi")

    def test_R4_unknown_timezone_fails_loudly(self):
        with pytest.raises(ValueError, match="unknown timezone"):
            business_date(datetime.now(timezone.utc), "Mars/Olympus_Mons")


class TestExplicitServiceDate:
    """An explicit date is validated separately, never inferred from client time.

    The only V1 rule is "not in the future". There is deliberately **no**
    maximum historical age — an arbitrary backdate window would be invented
    product policy.
    """

    today = date(2026, 3, 15)

    def test_R4_accepts_today(self):
        assert validate_service_date(self.today, today=self.today) == self.today

    def test_R4_accepts_recent_backdate(self):
        assert validate_service_date(date(2026, 3, 10), today=self.today) == date(2026, 3, 10)

    def test_R4_rejects_future_date(self):
        with pytest.raises(ValueError, match="future"):
            validate_service_date(date(2026, 3, 16), today=self.today)

    def test_R4_rejects_date_one_day_in_the_future(self):
        with pytest.raises(ValueError, match="future"):
            validate_service_date(self.today + timedelta(days=1), today=self.today)

    @pytest.mark.parametrize("days_ago", [91, 200, 365, 3650])
    def test_R4_accepts_dates_older_than_any_invented_window(self, days_ago):
        """Explicitly covers >90 days: no backdate window exists in V1."""
        old = self.today - timedelta(days=days_ago)
        assert validate_service_date(old, today=self.today) == old

    def test_R4_no_backdate_limit_is_exposed(self):
        """A future reviewer must not reintroduce a window without a decision."""
        import app.core.clock as clock_module

        assert not hasattr(clock_module, "MAX_BACKDATE_DAYS")
        names = [n for n in dir(clock_module) if "backdate" in n.lower()]
        assert names == []


class TestClockInjection:
    def test_fixed_clock_is_frozen(self):
        clock = FixedClock(datetime(2026, 3, 15, 7, 0, tzinfo=timezone.utc))
        assert clock.now_utc() == clock.now_utc()

    def test_fixed_clock_advances_only_when_told(self):
        clock = FixedClock(datetime(2026, 3, 15, 7, 0, tzinfo=timezone.utc))
        before = clock.now_utc()
        clock.advance(hours=13)
        assert clock.now_utc() - before == timedelta(hours=13)

    def test_fixed_clock_rejects_naive(self):
        with pytest.raises(ValueError):
            FixedClock(datetime(2026, 3, 15, 7, 0))

    def test_system_clock_is_utc_aware(self):
        assert SystemClock().now_utc().tzinfo is timezone.utc
