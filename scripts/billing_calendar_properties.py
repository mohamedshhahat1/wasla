"""Property checks of billing terms and usage cycles against an independent oracle.

ADR-116 puts two clocks on one anchor: a billing term of one price's length (a
month or twelve) and a usage cycle of one calendar month inside it. This checks
`app.services.billing_calendar` against an oracle written a different way:

- the oracle finds "the same date N months on" by stepping from the first of
  the anchor's month through first-of-month dates with day arithmetic (add 32
  days, snap back to the 1st) and clamping to that month's last day, which it
  finds as "the day before the next first" - never `divmod`, never
  `calendar.monthrange`, the arithmetic the implementation uses;
- every expectation is stated as the oracle's boundaries, and the checks
  assert the implementation lands on them.

What is checked, over random anchors (weighted to the 28th-31st, 29 February
and year ends) and random moments:

1. **Monthly terms**: a subscription rolled N times ends on the oracle's Nth
   monthly anniversary, never a 30-day step, and its one usage cycle is the term.
2. **Annual terms**: rolled N times, it ends on the oracle's 12Nth anniversary;
   every term is 365 or 366 days.
3. **Usage cycles inside an annual term**: for a random moment in the term,
   the cycle is the oracle's month containing it; the twelve cycles tile the
   term exactly, each 28-31 days; the first starts at the term's start and the
   last ends at its end.
4. **Catch-up**: from a stale stored cycle, `current_usage_period` lands on the
   oracle's cycle for the moment, however many months were missed.

Run as a script for the full count (`python -m scripts.billing_calendar_properties
1000000`); the unit suite runs a smaller sample of the same checks.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app.db.models.billing import BillingInterval
from app.services import billing_calendar

# ------------------------------------------------------------------- oracle


def _first_of_month_after(first: date, months: int) -> date:
    """The first day of the month `months` after (or before) `first`'s month."""
    step = 1 if months >= 0 else -1
    current = first
    for _ in range(abs(months)):
        if step > 0:
            current = (current + timedelta(days=32)).replace(day=1)
        else:
            current = (current - timedelta(days=1)).replace(day=1)
    return current


def oracle_months_after(anchor: datetime, months: int) -> datetime:
    """The anchor's day `months` months on, clamped to the target month."""
    target_first = _first_of_month_after(anchor.date().replace(day=1), months)
    next_first = _first_of_month_after(target_first, 1)
    last_day = (next_first - timedelta(days=1)).day
    day = anchor.day if anchor.day <= last_day else last_day
    return datetime(
        target_first.year,
        target_first.month,
        day,
        anchor.hour,
        anchor.minute,
        anchor.second,
        anchor.microsecond,
        tzinfo=anchor.tzinfo,
    )


def oracle_cycle_index(anchor: datetime, at: datetime, low: int, high: int) -> int:
    """The month index j with anchor+j <= at < anchor+(j+1), found by bisection."""
    while high - low > 1:
        middle = (low + high) // 2
        if oracle_months_after(anchor, middle) <= at:
            low = middle
        else:
            high = middle
    return low


# ------------------------------------------------------------------- checks


@dataclass
class Tally:
    checks: int = 0
    violations: list[str] = field(default_factory=list)

    def expect(self, condition: bool, message: str) -> None:
        self.checks += 1
        if not condition and len(self.violations) < 50:
            self.violations.append(message)


def _anchor(rng: random.Random) -> datetime:
    """An anchor weighted to the days that break naive calendars."""
    year = rng.randint(1999, 2102)
    roll = rng.random()
    if roll < 0.15:
        month, day = 2, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28
    elif roll < 0.45:
        month = rng.randint(1, 12)
        day = rng.choice([28, 29, 30, 31])
    elif roll < 0.55:
        month, day = 12, rng.choice([30, 31])
    else:
        month, day = rng.randint(1, 12), rng.randint(1, 28)
    last = (_first_of_month_after(date(year, month, 1), 1) - timedelta(days=1)).day
    return datetime(
        year,
        month,
        min(day, last),
        rng.randint(0, 23),
        rng.randint(0, 59),
        rng.randint(0, 59),
        tzinfo=UTC,
    )


def _check_monthly(rng: random.Random, tally: Tally) -> None:
    anchor = _anchor(rng)
    end = billing_calendar.add_interval(anchor, BillingInterval.MONTHLY)
    tally.expect(end == oracle_months_after(anchor, 1), f"first monthly term of {anchor}")
    rolls = rng.randint(1, 30)
    start = end
    for index in range(2, rolls + 2):
        end = billing_calendar.next_boundary(anchor, start, BillingInterval.MONTHLY)
        expected = oracle_months_after(anchor, index)
        tally.expect(end == expected, f"monthly roll {index} of {anchor}: {end} != {expected}")
        days = (end - start).days
        tally.expect(28 <= days <= 31, f"monthly term of {days} days from {start}")
        term = SimpleNamespace(
            billing_anchor_at=anchor, current_period_start=start, current_period_end=end
        )
        tally.expect(
            billing_calendar.first_usage_period(term) == (start, end),  # type: ignore[arg-type]
            f"monthly term {start}..{end} is its own usage cycle",
        )
        start = end


def _check_annual(rng: random.Random, tally: Tally) -> None:
    anchor = _anchor(rng)
    start = anchor
    for year in range(1, rng.randint(1, 6) + 1):
        end = (
            billing_calendar.add_interval(anchor, BillingInterval.YEARLY)
            if year == 1
            else billing_calendar.next_boundary(anchor, start, BillingInterval.YEARLY)
        )
        expected = oracle_months_after(anchor, 12 * year)
        tally.expect(end == expected, f"annual roll {year} of {anchor}: {end} != {expected}")
        tally.expect((end - start).days in (365, 366), f"annual term of {(end - start).days} days")
        _check_cycles(anchor, start, end, year, rng, tally)
        start = end


def _check_cycles(
    anchor: datetime,
    start: datetime,
    end: datetime,
    year: int,
    rng: random.Random,
    tally: Tally,
) -> None:
    base = 12 * (year - 1)
    # The twelve cycles tile the term.
    cursor = start
    for month in range(12):
        cycle = billing_calendar.usage_period(anchor, term_start=start, term_end=end, at=cursor)
        expected = (
            oracle_months_after(anchor, base + month),
            oracle_months_after(anchor, base + month + 1),
        )
        tally.expect(cycle == expected, f"cycle {month} of {start}..{end}: {cycle} != {expected}")
        tally.expect(28 <= (cycle[1] - cycle[0]).days <= 31, f"cycle of odd length {cycle}")
        tally.expect(cycle[0] == cursor, f"cycles do not tile at {cursor}")
        cursor = cycle[1]
    tally.expect(cursor == end, f"twelve cycles end at {cursor}, the term at {end}")
    term = SimpleNamespace(
        billing_anchor_at=anchor, current_period_start=start, current_period_end=end
    )
    first = billing_calendar.first_usage_period(term)  # type: ignore[arg-type]
    tally.expect(
        first == (start, oracle_months_after(anchor, base + 1)),
        f"the first cycle of {start}..{end} is {first}",
    )
    # Random moments inside the term, and catch-up from a stale stored cycle.
    for _ in range(4):
        span = (end - start).total_seconds()
        at = start + timedelta(seconds=rng.uniform(0, span - 1))
        index = oracle_cycle_index(anchor, at, base, base + 12)
        expected = (oracle_months_after(anchor, index), oracle_months_after(anchor, index + 1))
        got = billing_calendar.usage_period(anchor, term_start=start, term_end=end, at=at)
        tally.expect(got == expected, f"cycle at {at} in {start}..{end}: {got} != {expected}")
        stale: Any = SimpleNamespace(
            billing_anchor_at=anchor,
            current_period_start=start,
            current_period_end=end,
            usage_period_start=start,
            usage_period_end=oracle_months_after(anchor, base + 1),
            ended_at=None,
        )
        caught = billing_calendar.current_usage_period(stale, at)
        tally.expect(caught == expected, f"catch-up to {at}: {caught} != {expected}")


def run(checks: int, *, seed: int = 20260927) -> Tally:
    """Run at least `checks` property checks. Deterministic for a seed."""
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not a secret
    tally = Tally()
    while tally.checks < checks:
        _check_monthly(rng, tally)
        _check_annual(rng, tally)
    return tally


def main(argv: list[str]) -> int:
    wanted = int(argv[1]) if len(argv) > 1 else 1_000_000
    tally = run(wanted)
    sys.stdout.write(f"checks: {tally.checks}\nviolations: {len(tally.violations)}\n")
    for violation in tally.violations:
        sys.stdout.write(f"  {violation}\n")
    return 1 if tally.violations else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
