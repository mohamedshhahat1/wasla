"""The quiet-hours window as pure arithmetic in a workspace's zone (TZ-03)."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from app.core.timezones import zone
from app.services.quiet_hours import QuietHours

pytestmark = pytest.mark.quiet_hours

CAIRO = QuietHours(enabled=True, start=time(22), end=time(9), timezone="Africa/Cairo")


def cairo(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=zone("Africa/Cairo"))


@pytest.mark.parametrize(
    ("local", "inside"),
    [
        ((2026, 10, 20, 21, 59), False),
        ((2026, 10, 20, 22, 0), True),
        ((2026, 10, 21, 3, 0), True),
        ((2026, 10, 21, 8, 59), True),
        ((2026, 10, 21, 9, 0), False),
        ((2026, 10, 21, 13, 0), False),
    ],
)
def test_a_window_crossing_midnight(local: tuple[int, int, int, int, int], inside: bool) -> None:
    assert CAIRO.contains(cairo(*local)) is inside


def test_a_window_within_one_day() -> None:
    afternoon = QuietHours(enabled=True, start=time(13), end=time(15), timezone="Africa/Cairo")
    assert afternoon.contains(cairo(2026, 10, 20, 13, 0))
    assert not afternoon.contains(cairo(2026, 10, 20, 15, 0))
    assert afternoon.ends_after(cairo(2026, 10, 20, 14, 0)) == cairo(2026, 10, 20, 15, 0)


def test_the_end_is_the_next_morning_from_the_evening_and_today_from_the_small_hours() -> None:
    assert CAIRO.ends_after(cairo(2026, 10, 20, 23, 0)) == cairo(2026, 10, 21, 9, 0)
    assert CAIRO.ends_after(cairo(2026, 10, 21, 2, 0)) == cairo(2026, 10, 21, 9, 0)
    assert CAIRO.ends_after(cairo(2026, 10, 21, 10, 0)) == cairo(2026, 10, 21, 10, 0)


def test_switched_off_is_never_quiet() -> None:
    off = QuietHours(enabled=False, start=time(22), end=time(9), timezone="Africa/Cairo")
    assert not off.contains(cairo(2026, 10, 20, 23, 0))
    assert off.next_start(cairo(2026, 10, 20, 12, 0)) is None


def test_the_window_end_follows_daylight_saving_not_a_fixed_offset() -> None:
    assert CAIRO.ends_after(cairo(2026, 4, 23, 23, 30)) == datetime(2026, 4, 24, 6, tzinfo=UTC)
    assert CAIRO.ends_after(cairo(2026, 10, 29, 22, 30)) == datetime(2026, 10, 30, 7, tzinfo=UTC)


def test_the_next_start() -> None:
    assert CAIRO.next_start(cairo(2026, 10, 20, 12, 0)) == cairo(2026, 10, 20, 22, 0)
    assert CAIRO.next_start(cairo(2026, 10, 20, 23, 0)) == cairo(2026, 10, 20, 23, 0)
