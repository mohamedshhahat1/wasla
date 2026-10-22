"""`next_local`: the next wall time after now, never in a daylight-saving gap or fold (DT-04)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.timezones import zone
from tests.clock_moments import exists_once, next_local

CAIRO = zone("Africa/Cairo")


def cairo(year: int, month: int, day: int, hour: int, minute: int = 0, fold: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=CAIRO, fold=fold)


def test_the_same_day_when_it_is_still_ahead_and_the_next_when_it_has_passed() -> None:
    def half_past_six(after: datetime) -> datetime:
        return next_local(6, 30, tz=CAIRO, after=after)

    assert half_past_six(cairo(2026, 10, 21, 6, 0)) == cairo(2026, 10, 21, 6, 30)
    assert half_past_six(cairo(2026, 10, 21, 6, 30)) == cairo(2026, 10, 22, 6, 30)
    assert half_past_six(cairo(2026, 10, 21, 7, 0)) == cairo(2026, 10, 22, 6, 30)


def test_a_wall_time_in_the_october_fold_is_taken_the_next_day() -> None:
    """2026-10-29 24:00 (+03:00) is 23:00 (+02:00): 23:00-23:59 that day happens twice."""
    first, second = cairo(2026, 10, 29, 23, 30), cairo(2026, 10, 29, 23, 30, fold=1)
    assert first.utcoffset() != second.utcoffset()
    assert not exists_once(first) and not exists_once(second)
    moment = next_local(23, 30, tz=CAIRO, after=cairo(2026, 10, 29, 22, 0))
    assert moment == cairo(2026, 10, 30, 23, 30)
    assert moment.utcoffset() == timedelta(hours=2)


def test_a_wall_time_in_the_april_gap_is_taken_the_next_day() -> None:
    """2026-04-24 00:00 (+02:00) is 01:00 (+03:00): 00:00-00:59 that day never happens."""
    assert not exists_once(cairo(2026, 4, 24, 0, 30))
    moment = next_local(0, 30, tz=CAIRO, after=cairo(2026, 4, 23, 23, 0))
    assert moment == cairo(2026, 4, 25, 0, 30)
    assert moment.utcoffset() == timedelta(hours=3)


def test_times_on_either_side_of_a_transition_exist_once() -> None:
    assert exists_once(cairo(2026, 10, 29, 22, 30))
    assert exists_once(cairo(2026, 10, 30, 0, 30))
    assert exists_once(cairo(2026, 4, 24, 1, 30))
    assert exists_once(datetime(2026, 10, 29, 23, 30, tzinfo=UTC))


def test_in_utc_and_from_the_real_clock_by_default() -> None:
    after = datetime(2026, 10, 20, 5, 30, tzinfo=UTC)
    assert next_local(5, 30, tz=UTC, after=after) == datetime(2026, 10, 21, 5, 30, tzinfo=UTC)
    before = datetime.now(UTC)
    moment = next_local(6, 30, tz=CAIRO)
    assert before < moment <= before + timedelta(days=1, hours=1)
    assert (moment.hour, moment.minute) == (6, 30) and exists_once(moment)
