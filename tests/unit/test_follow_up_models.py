"""Follow-up rules that hold without a database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import ValidationError
from app.db.models.follow_up import (
    MAX_ATTEMPTS,
    TERMINAL_FOLLOW_UP_STATUSES,
    FollowUp,
    FollowUpStatus,
)
from app.schemas.follow_up import FollowUpCreateRequest
from app.services.follow_up_service import (
    MAX_DELAY,
    MIN_DELAY,
    _backoff,
    _validated_body,
    _validated_template,
)


def test_only_pending_is_not_terminal() -> None:
    """Everything else is finished work and releases the per-conversation slot."""
    assert set(FollowUpStatus) - {FollowUpStatus.PENDING} == TERMINAL_FOLLOW_UP_STATUSES


def test_skipped_is_terminal_and_distinct_from_failed() -> None:
    """Policy refused the send; the window will not reopen, so it is not retried."""
    assert FollowUpStatus.SKIPPED in TERMINAL_FOLLOW_UP_STATUSES
    # Compared through a variable typed as the enum, because comparing two
    # members directly is a comparison mypy can settle at check time - and what
    # this asserts is that they are *distinct members*, which is a fact about
    # the enum rather than about a value.
    skipped: FollowUpStatus = FollowUpStatus.SKIPPED
    assert skipped is not FollowUpStatus.FAILED


def test_a_follow_up_knows_whether_it_can_leave_the_service_window() -> None:
    assert FollowUp(template_name="nudge", template_language="en").has_template
    # Half a template is not a template: Meta needs both, and a name alone would
    # fail after the send had already been attempted.
    assert not FollowUp(template_name="nudge", template_language=None).has_template
    assert not FollowUp(template_name=None, template_language="en").has_template
    assert not FollowUp().has_template


def test_attempts_are_exhausted_at_the_limit() -> None:
    assert not FollowUp(attempts=MAX_ATTEMPTS - 1).is_exhausted
    assert FollowUp(attempts=MAX_ATTEMPTS).is_exhausted


def test_backoff_grows_with_each_attempt() -> None:
    """A retry that came straight back would burn every attempt in seconds."""
    delays = [_backoff(attempt) for attempt in range(1, MAX_ATTEMPTS + 1)]

    assert delays == sorted(delays)
    assert delays[0] > timedelta(0)
    assert delays[-1] > delays[0]


def test_a_blank_body_is_not_a_message() -> None:
    assert _validated_body("   ") is None
    assert _validated_body(None) is None
    assert _validated_body("  Still there?  ") == "Still there?"


def test_an_overlong_body_is_refused() -> None:
    with pytest.raises(ValidationError):
        _validated_body("x" * 5000)


def test_a_template_needs_both_halves() -> None:
    assert _validated_template("nudge", "en") == ("nudge", "en")
    assert _validated_template(None, None) == (None, None)
    with pytest.raises(ValidationError):
        _validated_template("nudge", None)
    with pytest.raises(ValidationError):
        _validated_template(None, "en")


def test_a_request_needs_exactly_one_kind_of_time() -> None:
    """Accepting both would leave the service silently picking a winner."""
    both = {
        "conversation_id": "11111111-1111-1111-1111-111111111111",
        "body": "Still there?",
        "delay_minutes": 30,
        "scheduled_at": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(ValueError, match="exactly one"):
        FollowUpCreateRequest.model_validate(both)

    neither = {
        "conversation_id": "11111111-1111-1111-1111-111111111111",
        "body": "Still there?",
    }
    with pytest.raises(ValueError, match="exactly one"):
        FollowUpCreateRequest.model_validate(neither)


def test_a_request_needs_something_to_send() -> None:
    with pytest.raises(ValueError, match="body, a template"):
        FollowUpCreateRequest.model_validate(
            {
                "conversation_id": "11111111-1111-1111-1111-111111111111",
                "delay_minutes": 30,
            }
        )


def test_a_template_only_request_is_accepted() -> None:
    """Valid on its own: it is the only thing that works outside the window."""
    request = FollowUpCreateRequest.model_validate(
        {
            "conversation_id": "11111111-1111-1111-1111-111111111111",
            "delay_minutes": 1440,
            "template_name": "gentle_nudge",
            "template_language": "ar",
        }
    )

    assert request.body is None
    assert request.template_name == "gentle_nudge"


@pytest.mark.parametrize("minutes", [0, -5, int(MAX_DELAY.total_seconds() // 60) + 1])
def test_a_delay_outside_the_bounds_is_refused_by_the_schema(minutes: int) -> None:
    with pytest.raises(ValueError):
        FollowUpCreateRequest.model_validate(
            {
                "conversation_id": "11111111-1111-1111-1111-111111111111",
                "body": "Still there?",
                "delay_minutes": minutes,
            }
        )


def test_the_delay_bounds_are_ordered_sensibly() -> None:
    assert MIN_DELAY < MAX_DELAY
