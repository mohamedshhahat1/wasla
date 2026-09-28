"""Where billing terms and usage cycles begin and end.

Calendar arithmetic rather than a fixed number of days. Nobody bills in 30-day
units: "monthly" means the same date next month, "yearly" the same date next
year, and 30 or 365 days drifts a renewal backwards through the calendar until
it lands in the wrong month. Every length here is a whole number of calendar
months (`MONTHS_PER_INTERVAL`).

**Every boundary is counted from the anchor, never from the previous one**
(BILL-18). A subscription that began on 31 January renews on 28 February, on 31
March, on 30 April and on 31 May. Chaining each end from the one before it -
which is what this used to do - clamped to the 28th in February and then stayed
on the 28th for ever, so a customer who signed up on the 31st lost two or three
days in every month for the life of the subscription.

The same rule gives the yearly case its answer: an anchor of 29 February renews
on 28 February in a common year and on 29 February again in the next leap year,
because each renewal is the anchor's own date clamped to that year's February.

**Two clocks run on one anchor** (ADR-116). The *billing term* is one price's
term - one month or twelve. The *usage cycle* is always one calendar month, the
window the `period_*` allowances are counted over. Both count months from
`billing_anchor_at`, so a yearly term's twelfth usage cycle ends exactly where
the term does, and a monthly term and its one cycle are the same window.

Pure functions, no I/O, all timezone-aware in whatever zone the anchor carries
(the application stores UTC). The time of day is the anchor's and never moves.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from typing import Final

from app.db.models.billing import MONTHS_PER_INTERVAL, BillingInterval, Subscription

# The smallest step `datetime` can take: "the last instant of a term".
_INSTANT: Final = timedelta(microseconds=1)


def term_months(interval: BillingInterval, interval_count: int = 1) -> int:
    """How many calendar months one billing term of this shape lasts."""
    if interval_count < 1:
        raise ValueError("A billing term is at least one interval long.")
    return MONTHS_PER_INTERVAL[interval] * interval_count


def months_after(anchor: datetime, months: int) -> datetime:
    """The anchor's date `months` calendar months on, clamped to that month.

    Negative `months` counts backwards the same way. The day is always the
    anchor's own, clamped - never a previous boundary's, which is the whole
    difference between this and the arithmetic it replaced.
    """
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def anniversary(
    anchor: datetime, count: int, interval: BillingInterval, interval_count: int = 1
) -> datetime:
    """The `count`-th term boundary after `anchor`. `count=0` is the anchor."""
    return months_after(anchor, count * term_months(interval, interval_count))


def add_interval(start: datetime, interval: BillingInterval, interval_count: int = 1) -> datetime:
    """One term after `start`, treating `start` as its own anchor."""
    return anniversary(start, 1, interval, interval_count)


def next_boundary(
    anchor: datetime,
    after: datetime,
    interval: BillingInterval,
    interval_count: int = 1,
) -> datetime:
    """The first anchored term boundary strictly later than `after`.

    What a roll-over uses: the new term starts where the old one ended, and
    ends at the next anniversary of the anchor. Strictly later, so a term can
    never have zero length even when `after` falls exactly on a boundary.
    """
    step = term_months(interval, interval_count)
    elapsed = (after.year - anchor.year) * 12 + (after.month - anchor.month)
    count = max(1, elapsed // step - 1)
    candidate = months_after(anchor, count * step)
    while candidate <= after:
        count += 1
        candidate = months_after(anchor, count * step)
    return candidate


def usage_cycle(anchor: datetime, at: datetime) -> tuple[datetime, datetime]:
    """The anchored calendar month `[start, end)` that contains `at`.

    Computed directly, whatever the distance from the anchor - which is what
    lets the sweep catch a subscription up across any number of missed cycles
    in one step rather than one cycle per pass.
    """
    count = (at.year - anchor.year) * 12 + (at.month - anchor.month)
    while months_after(anchor, count) > at:
        count -= 1
    while months_after(anchor, count + 1) <= at:
        count += 1
    return months_after(anchor, count), months_after(anchor, count + 1)


def usage_period(
    anchor: datetime,
    *,
    term_start: datetime,
    term_end: datetime,
    at: datetime,
) -> tuple[datetime, datetime]:
    """The usage cycle in force at `at`, inside the billing term `[start, end)`.

    The anchored month containing `at`, clipped to the term - so a term that
    does not begin on an anniversary (a hand-written legacy row) still has
    cycles that never leave it. `at` before the term reads as its first cycle,
    and at or after its end as its last: nothing beyond a term is paid for, and
    the roll-over is what opens the next one.
    """
    if term_end <= term_start:
        raise ValueError("A billing term ends after it starts.")
    moment = min(max(at, term_start), term_end - _INSTANT)
    start, end = usage_cycle(anchor, moment)
    return max(start, term_start), min(end, term_end)


def first_usage_period(subscription: Subscription) -> tuple[datetime, datetime]:
    """The usage cycle a billing term opens with: its first anchored month."""
    anchor = subscription.billing_anchor_at or subscription.current_period_start
    return usage_period(
        anchor,
        term_start=subscription.current_period_start,
        term_end=subscription.current_period_end,
        at=subscription.current_period_start,
    )


def current_usage_period(subscription: Subscription, at: datetime) -> tuple[datetime, datetime]:
    """The usage cycle actually in force at `at`, whether or not the sweep has run.

    The stored cycle while `at` is inside it. Once it has ended but the paid
    term has not - an annual subscription whose monthly cycle rolled at
    midnight, before the sweep's next pass - the cycle containing `at`,
    computed exactly as the sweep will store it. So enforcement follows the
    clock and never lets a stale window count less than it should; the sweep
    only records what is already true, as it does for top-up expiry.

    Past the end of the term, and for an ended subscription, the stored cycle
    stands: what opens next is the roll-over's decision, not the clock's. A row
    not yet written has no stored cycle; its term is its cycle, as the column
    default makes it on insert.
    """
    term_start, term_end = subscription.current_period_start, subscription.current_period_end
    start = subscription.usage_period_start or term_start
    end = subscription.usage_period_end or term_end
    if (
        subscription.ended_at is not None
        or start <= at < end
        or at < end
        or at >= term_end
        or term_end <= term_start
    ):
        return start, end
    anchor = subscription.billing_anchor_at or term_start
    return usage_period(anchor, term_start=term_start, term_end=term_end, at=at)


__all__ = [
    "add_interval",
    "anniversary",
    "current_usage_period",
    "first_usage_period",
    "months_after",
    "next_boundary",
    "term_months",
    "usage_cycle",
    "usage_period",
]
