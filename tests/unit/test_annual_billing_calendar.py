"""Billing terms and usage cycles: two clocks on one anchor (ADR-116).

Named examples first - the dates a reader checks by hand - then a sample of the
independent property checks in `scripts/billing_calendar_properties.py`, whose
full run (over a million checks) is recorded in ANNUAL_BILLING_IMPLEMENTATION.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.db.models.billing import BillingInterval
from app.services import billing_calendar
from scripts.billing_calendar_properties import oracle_months_after, run


def _at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 9, 30, tzinfo=UTC)


def _term(
    anchor: datetime,
    start: datetime,
    end: datetime,
    *,
    usage_start: datetime | None = None,
    usage_end: datetime | None = None,
) -> Any:
    return SimpleNamespace(
        billing_anchor_at=anchor,
        current_period_start=start,
        current_period_end=end,
        usage_period_start=usage_start or start,
        usage_period_end=usage_end or end,
        ended_at=None,
    )


def test_a_yearly_term_is_twelve_calendar_months_not_365_days() -> None:
    anchor = _at(2026, 10, 15)
    assert billing_calendar.add_interval(anchor, BillingInterval.YEARLY) == _at(2027, 10, 15)
    # Across a leap day the term is 366 days, because it is a calendar year.
    leap = _at(2027, 10, 15)
    assert (billing_calendar.add_interval(leap, BillingInterval.YEARLY) - leap).days == 366


def test_a_29_february_anchor_renews_on_28_february_then_29_again() -> None:
    anchor = _at(2028, 2, 29)
    first = billing_calendar.add_interval(anchor, BillingInterval.YEARLY)
    assert first == _at(2029, 2, 28)
    fourth = billing_calendar.next_boundary(anchor, _at(2031, 2, 28), BillingInterval.YEARLY)
    assert fourth == _at(2032, 2, 29), "the anchor's own day returns in the next leap year"


def test_a_31_january_annual_term_has_twelve_monthly_cycles_that_tile_it() -> None:
    anchor = _at(2026, 1, 31)
    end = billing_calendar.add_interval(anchor, BillingInterval.YEARLY)
    cycles = []
    cursor = anchor
    while cursor < end:
        cycle = billing_calendar.usage_period(anchor, term_start=anchor, term_end=end, at=cursor)
        cycles.append(cycle)
        cursor = cycle[1]
    assert len(cycles) == 12
    assert [cycle[1].day for cycle in cycles] == [28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31, 31]
    assert cycles[0] == (anchor, _at(2026, 2, 28))
    assert cycles[-1][1] == end == _at(2027, 1, 31)


def test_the_first_usage_cycle_of_an_annual_term_is_its_first_month() -> None:
    anchor = _at(2026, 10, 1)
    end = billing_calendar.add_interval(anchor, BillingInterval.YEARLY)
    first = billing_calendar.first_usage_period(_term(anchor, anchor, end))
    assert first == (anchor, _at(2026, 11, 1))


def test_a_monthly_term_is_its_own_usage_cycle() -> None:
    anchor = _at(2026, 1, 31)
    start = billing_calendar.next_boundary(anchor, _at(2026, 2, 28), BillingInterval.MONTHLY)
    assert start == _at(2026, 3, 31)
    term = _term(anchor, _at(2026, 2, 28), start)
    assert billing_calendar.first_usage_period(term) == (_at(2026, 2, 28), start)


def test_a_worker_down_for_months_catches_up_in_one_step() -> None:
    anchor = _at(2026, 1, 10)
    end = billing_calendar.add_interval(anchor, BillingInterval.YEARLY)
    # Stored: January's cycle. Now: April 20th.
    stale = _term(anchor, anchor, end, usage_end=_at(2026, 2, 10))
    assert billing_calendar.current_usage_period(stale, _at(2026, 4, 20)) == (
        _at(2026, 4, 10),
        _at(2026, 5, 10),
    )


def test_the_stored_cycle_stands_past_the_end_of_the_term() -> None:
    """At the term's end the roll-over opens what comes next, not the clock."""
    anchor = _at(2026, 1, 10)
    end = billing_calendar.add_interval(anchor, BillingInterval.YEARLY)
    last = (_at(2026, 12, 10), end)
    stored = _term(anchor, anchor, end, usage_start=last[0], usage_end=last[1])
    assert billing_calendar.current_usage_period(stored, _at(2027, 1, 20)) == last


def test_an_ended_subscription_keeps_its_stored_cycle() -> None:
    anchor = _at(2026, 1, 10)
    end = billing_calendar.add_interval(anchor, BillingInterval.YEARLY)
    stored = _term(anchor, anchor, end, usage_end=_at(2026, 2, 10))
    stored.ended_at = _at(2026, 1, 20)
    assert billing_calendar.current_usage_period(stored, _at(2026, 5, 1)) == (
        anchor,
        _at(2026, 2, 10),
    )


def test_the_oracle_agrees_on_the_hand_checked_dates() -> None:
    assert oracle_months_after(_at(2026, 1, 31), 1) == _at(2026, 2, 28)
    assert oracle_months_after(_at(2028, 2, 29), 12) == _at(2029, 2, 28)
    assert oracle_months_after(_at(2026, 12, 31), -1) == _at(2026, 11, 30)


@pytest.mark.parametrize("count", [0, -1])
def test_a_term_is_at_least_one_interval_long(count: int) -> None:
    with pytest.raises(ValueError):
        billing_calendar.term_months(BillingInterval.MONTHLY, count)


def test_twenty_thousand_independent_property_checks_find_no_violation() -> None:
    tally = run(20_000)
    assert tally.checks >= 20_000
    assert tally.violations == []
