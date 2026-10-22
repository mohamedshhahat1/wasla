"""Moments built from the real clock, in a zone, for tests that meet a real-clock decision.

A test whose `SimClock` meets a decision the application makes on the real
clock - releasing a quiet-hours wait, keeping a scheduled time that is still
in the future - cannot pin a calendar date: the day the calendar passes it,
the two clocks disagree and the test fails on every run after (CPM-01). It
builds the moment from the real now instead, as a local wall time that exists
exactly once that day, so a daylight-saving gap or fold never makes it
ambiguous.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo


def exists_once(moment: datetime) -> bool:
    """Whether `moment`'s wall time occurs exactly once in its zone: not in a gap, not in a fold."""
    return moment.utcoffset() == moment.replace(fold=1 - moment.fold).utcoffset()


def next_local(
    hour: int, minute: int = 0, *, tz: tzinfo, after: datetime | None = None
) -> datetime:
    """The first `hour:minute` in `tz` strictly after `after` (the real now) that exists once.

    A day on which that wall time falls in a daylight-saving gap or fold is
    passed over: the next day's is returned instead.
    """
    start = (after if after is not None else datetime.now(UTC)).astimezone(tz)
    for ahead in range(4):
        day = start.date() + timedelta(days=ahead)
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if candidate.astimezone(UTC) > start.astimezone(UTC) and exists_once(candidate):
            return candidate
    raise AssertionError(f"no single {hour:02d}:{minute:02d} within four days of {start}")
