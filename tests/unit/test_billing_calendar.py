"""BILL-18: period ends are counted from a durable anchor, never chained.

The defect: each period end was computed from the previous one, so a
subscription that began on 31 January clamped to 28 February and then renewed
on the 28th for ever. These pin the anchored arithmetic over many cycles.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db.models.billing import BillingInterval
from app.services.billing_calendar import add_interval, anniversary, next_boundary


def _cycle(anchor: datetime, count: int, interval: BillingInterval) -> list[datetime]:
    """The boundaries a subscription rolling over `count` times would reach."""
    ends: list[datetime] = []
    start = anchor
    for _ in range(count):
        end = next_boundary(anchor, start, interval)
        ends.append(end)
        start = end
    return ends


@pytest.mark.parametrize(
    ("anchor", "expected_days"),
    [
        # 31 Jan -> 28 Feb -> 31 Mar -> 30 Apr -> 31 May -> 30 Jun -> 31 Jul -> 31 Aug.
        (
            datetime(2026, 1, 31, 9, 30, tzinfo=UTC),
            [28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31, 31],
        ),
        # 30 Jan -> 28 Feb -> 30 Mar -> 30 Apr ...
        (datetime(2026, 1, 30, tzinfo=UTC), [28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30]),
        # 31 Mar -> 30 Apr -> 31 May -> 30 Jun -> 31 Jul -> 31 Aug -> 30 Sep ...
        (datetime(2026, 3, 31, tzinfo=UTC), [30, 31, 30, 31, 31, 30, 31, 30, 31, 31, 28, 31]),
        # 31 Aug -> 30 Sep -> 31 Oct -> 30 Nov -> 31 Dec -> 31 Jan -> 28 Feb -> 31 Mar
        (datetime(2026, 8, 31, tzinfo=UTC), [30, 31, 30, 31, 31, 28, 31, 30, 31, 30, 31, 31]),
    ],
)
def test_a_monthly_anchor_keeps_its_day_through_short_months(
    anchor: datetime, expected_days: list[int]
) -> None:
    ends = _cycle(anchor, 12, BillingInterval.MONTHLY)
    assert [end.day for end in ends] == expected_days
    # The time of day never moves, and every boundary is one calendar month on.
    assert {(end.hour, end.minute) for end in ends} == {(anchor.hour, anchor.minute)}
    assert [
        (end.year * 12 + end.month) - (anchor.year * 12 + anchor.month) for end in ends
    ] == list(range(1, 13))


def test_the_old_chained_arithmetic_is_what_drifted() -> None:
    """The control: chaining from the previous end loses the 31st for ever."""
    chained = [datetime(2026, 1, 31, tzinfo=UTC)]
    for _ in range(3):
        chained.append(add_interval(chained[-1], BillingInterval.MONTHLY))
    assert [moment.day for moment in chained[1:]] == [28, 28, 28]
    assert [end.day for end in _cycle(chained[0], 3, BillingInterval.MONTHLY)] == [28, 31, 30]


def test_a_leap_day_yearly_anchor_returns_to_the_29th_in_a_leap_year() -> None:
    """Documented policy: 29 Feb renews on 28 Feb in common years, 29 Feb in leap years."""
    anchor = datetime(2028, 2, 29, 12, tzinfo=UTC)
    ends = _cycle(anchor, 8, BillingInterval.YEARLY)
    assert [(end.year, end.month, end.day) for end in ends] == [
        (2029, 2, 28),
        (2030, 2, 28),
        (2031, 2, 28),
        (2032, 2, 29),
        (2033, 2, 28),
        (2034, 2, 28),
        (2035, 2, 28),
        (2036, 2, 29),
    ]


def test_the_next_boundary_is_strictly_later_even_on_a_boundary() -> None:
    anchor = datetime(2026, 1, 15, tzinfo=UTC)
    boundary = anniversary(anchor, 3, BillingInterval.MONTHLY)
    assert next_boundary(anchor, boundary, BillingInterval.MONTHLY) == anniversary(
        anchor, 4, BillingInterval.MONTHLY
    )


def test_a_period_that_started_before_its_anchor_still_moves_forward() -> None:
    anchor = datetime(2026, 5, 10, tzinfo=UTC)
    assert next_boundary(anchor, datetime(2026, 4, 1, tzinfo=UTC), BillingInterval.MONTHLY) == (
        datetime(2026, 6, 10, tzinfo=UTC)
    )
