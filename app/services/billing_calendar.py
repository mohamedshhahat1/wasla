"""Where billing periods begin and end.

Calendar arithmetic rather than a fixed number of days. Nobody bills in 30-day
units: "monthly" means the same date next month, and 30 days drifts a renewal
backwards through the year until it lands in the wrong month.

**Every period end is counted from the anchor, never from the previous end**
(BILL-18). A subscription that began on 31 January renews on 28 February, on 31
March, on 30 April and on 31 May. Chaining each end from the one before it -
which is what this used to do - clamped to the 28th in February and then stayed
on the 28th for ever, so a customer who signed up on the 31st lost two or three
days in every month for the life of the subscription.

The same rule gives the yearly case its answer: an anchor of 29 February renews
on 28 February in a common year and on 29 February again in the next leap year,
because each renewal is the anchor's own date clamped to that year's February.

Pure functions, no I/O, all timezone-aware in whatever zone the anchor carries
(the application stores UTC). The time of day is the anchor's and never moves.
"""

from __future__ import annotations

import calendar
from datetime import datetime
from typing import Final

from app.db.models.billing import BillingInterval

_MONTHS_PER_INTERVAL: Final[dict[BillingInterval, int]] = {
    BillingInterval.MONTHLY: 1,
    BillingInterval.YEARLY: 12,
}


def anniversary(anchor: datetime, count: int, interval: BillingInterval) -> datetime:
    """The `count`-th period boundary after `anchor`.

    `count=0` is the anchor itself. The day is the anchor's day, clamped to the
    length of the target month - never the previous boundary's day, which is
    the whole difference between this and the arithmetic it replaced.
    """
    months = count * _MONTHS_PER_INTERVAL[interval]
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def add_interval(start: datetime, interval: BillingInterval) -> datetime:
    """One interval after `start`, treating `start` as its own anchor."""
    return anniversary(start, 1, interval)


def next_boundary(anchor: datetime, after: datetime, interval: BillingInterval) -> datetime:
    """The first anchored boundary strictly later than `after`.

    What a roll-over uses: the new period starts where the old one ended, and
    ends at the next anniversary of the anchor. Strictly later, so a period can
    never have zero length even when `after` falls exactly on a boundary.
    """
    step = _MONTHS_PER_INTERVAL[interval]
    elapsed = (after.year - anchor.year) * 12 + (after.month - anchor.month)
    count = max(1, elapsed // step - 1)
    candidate = anniversary(anchor, count, interval)
    while candidate <= after:
        count += 1
        candidate = anniversary(anchor, count, interval)
    return candidate


__all__ = ["add_interval", "anniversary", "next_boundary"]
