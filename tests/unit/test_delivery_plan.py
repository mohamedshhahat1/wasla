"""The delivery plan as a pure replay of the worker's rules (LIM-05, LIM-06, PG-13)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from app.core.timezones import zone
from app.services.delivery_plan import (
    EtaBasis,
    PlanInputs,
    PlannedRecipient,
    PlanNotice,
    confidence,
    plan,
)
from app.services.quiet_hours import QuietHours

pytestmark = pytest.mark.quiet_hours

QUIET = QuietHours(enabled=True, start=time(22), end=time(9), timezone="Africa/Cairo")
OFF = QuietHours(enabled=False, start=time(22), end=time(9), timezone="Africa/Cairo")


def cairo(*parts: int) -> datetime:
    year, month, day, hour, minute = (*parts, 0)[:5]
    return datetime(year, month, day, hour, minute, tzinfo=zone("Africa/Cairo"))


def people(count: int, **traits: object) -> tuple[PlannedRecipient, ...]:
    return tuple(PlannedRecipient(**traits) for _ in range(count))  # type: ignore[arg-type]


def test_600_on_a_budget_of_200_spread_over_three_days() -> None:
    now = cairo(2026, 10, 20, 10)
    result = plan(PlanInputs(recipients=people(600), rate=600, quiet=OFF, budget=200), now=now)
    assert [day.recipients for day in result.days] == [200, 200, 200]
    assert PlanNotice.SPREADS_OVER_DAYS in result.notices
    assert EtaBasis.DAILY_LIMIT in result.basis
    assert result.estimated_completion_at is not None
    assert result.estimated_completion_at - now > timedelta(days=2)
    assert confidence(result) == "low"


def test_rate_alone_is_minutes_and_trusted() -> None:
    now = cairo(2026, 10, 20, 10)
    result = plan(PlanInputs(recipients=people(600), rate=60, quiet=OFF, budget=None), now=now)
    assert [day.recipients for day in result.days] == [600]
    assert result.estimated_completion_at is not None
    assert timedelta(minutes=9) <= result.estimated_completion_at - now <= timedelta(minutes=10)
    assert result.basis == (EtaBasis.RATE,)
    assert confidence(result) == "high"


def test_a_start_inside_quiet_hours_begins_at_nine_local() -> None:
    now = cairo(2026, 10, 20, 12)
    result = plan(
        PlanInputs(
            recipients=people(10), rate=600, quiet=QUIET, budget=None, start=cairo(2026, 10, 20, 23)
        ),
        now=now,
    )
    assert result.first_send_at == cairo(2026, 10, 21, 9)
    assert PlanNotice.STARTS_AFTER_QUIET_HOURS in result.notices
    assert confidence(result) == "medium"


def test_quiet_hours_and_the_limit_together() -> None:
    """21:30 local, 300 people, budget 200: 200 now; 100 a day later, still before 22:00."""
    now = cairo(2026, 10, 20, 21, 30)
    result = plan(PlanInputs(recipients=people(300), rate=600, quiet=QUIET, budget=200), now=now)
    assert [day.recipients for day in result.days] == [200, 100]
    assert result.days[1].first_send_at >= now + timedelta(hours=24)


def test_open_windows_and_people_already_counted_take_no_place() -> None:
    now = cairo(2026, 10, 20, 10)
    recipients = people(150) + people(100, window_open=True) + people(50, counted=True)
    result = plan(PlanInputs(recipients=recipients, rate=600, quiet=OFF, budget=150), now=now)
    assert [day.recipients for day in result.days] == [300]


def test_places_already_used_come_back_24_hours_after_they_were_taken() -> None:
    now = cairo(2026, 10, 20, 10)
    used = tuple(now - timedelta(hours=23) for _ in range(200))
    result = plan(
        PlanInputs(recipients=people(10), rate=600, quiet=OFF, budget=200, counted_at=used),
        now=now,
    )
    assert result.first_send_at == now + timedelta(hours=1, minutes=5)


def test_a_deadline_leaves_the_rest_and_says_so() -> None:
    now = cairo(2026, 10, 20, 10)
    result = plan(
        PlanInputs(
            recipients=people(600),
            rate=600,
            quiet=OFF,
            budget=200,
            send_until=now + timedelta(hours=30),
        ),
        now=now,
    )
    assert result.planned == 400
    assert result.left_by_deadline == 200
    assert PlanNotice.DEADLINE_LEAVES_RECIPIENTS in result.notices
    assert result.estimated_completion_at is None
    assert EtaBasis.DEADLINE in result.basis


def test_a_plan_past_thirty_days_is_flagged() -> None:
    now = cairo(2026, 10, 20, 10)
    result = plan(PlanInputs(recipients=people(2000), rate=600, quiet=OFF, budget=40), now=now)
    assert len(result.days) == 50
    assert PlanNotice.PLAN_EXCEEDS_TTL in result.notices


def test_deferred_people_wait_for_their_moment() -> None:
    now = cairo(2026, 10, 20, 10)
    later = now + timedelta(hours=3)
    result = plan(
        PlanInputs(
            recipients=people(5) + people(5, cap_until=later), rate=600, quiet=OFF, budget=None
        ),
        now=now,
    )
    assert result.planned == 10
    assert result.estimated_completion_at is not None
    assert result.estimated_completion_at >= later
    assert PlanNotice.CONTACT_CAP_DEFERS in result.notices
    assert EtaBasis.CONTACT_CAP in result.basis


def test_an_unknown_limit_has_no_plan() -> None:
    now = datetime(2026, 10, 20, 10, tzinfo=UTC)
    result = plan(
        PlanInputs(recipients=people(5), rate=600, quiet=OFF, budget=None, limit_known=False),
        now=now,
    )
    assert result.days == ()
    assert result.notices == (PlanNotice.LIMIT_UNKNOWN,)


def test_a_shared_limit_is_named_when_it_spreads() -> None:
    now = cairo(2026, 10, 20, 10)
    result = plan(
        PlanInputs(recipients=people(300), rate=600, quiet=OFF, budget=200, shared=True), now=now
    )
    assert PlanNotice.LIMIT_SHARED_WITH_OTHER_NUMBERS in result.notices
