"""Conversation memory windows.

No database: memory takes messages, so these tests build them in memory and
assert on what an agent would actually see.
"""

from datetime import UTC, datetime, timedelta

from app.agents.memory import build_window, estimate_tokens
from app.db.models.conversation import Message, MessageDirection, MessageKind, MessageStatus

BASE_TIME = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
ARABIC_LETTER = "\u0645"


def _message(
    *,
    direction=MessageDirection.INBOUND,
    body="hello",
    minutes=0,
    status=MessageStatus.RECEIVED,
    kind=MessageKind.TEXT,
):
    return Message(
        direction=direction,
        kind=kind,
        status=status,
        body=body,
        created_at=BASE_TIME + timedelta(minutes=minutes),
    )


def test_empty_text_costs_nothing():
    assert estimate_tokens("") == 0


def test_any_text_costs_at_least_one_token():
    assert estimate_tokens("a") == 1


def test_non_ascii_text_costs_more_than_ascii_of_the_same_length():
    """Arabic is roughly twice as expensive per character as English.

    A single divisor would under-count the budget for this product's main
    language, which is the whole reason the estimate is split.
    """
    ascii_estimate = estimate_tokens("a" * 40)
    arabic_estimate = estimate_tokens(ARABIC_LETTER * 40)

    assert arabic_estimate > ascii_estimate


def test_turns_are_chronological_whatever_order_they_arrive_in():
    newest = _message(body="second", minutes=5)
    oldest = _message(body="first", minutes=0)

    window = build_window([newest, oldest], message_limit=10, token_budget=1000)

    assert [turn.text for turn in window.turns] == ["first", "second"]


def test_direction_decides_the_role():
    window = build_window(
        [
            _message(body="customer", minutes=0),
            _message(
                body="business",
                minutes=1,
                direction=MessageDirection.OUTBOUND,
                status=MessageStatus.SENT,
            ),
        ],
        message_limit=10,
        token_budget=1000,
    )

    assert [turn.role for turn in window.turns] == ["user", "assistant"]


def test_message_limit_keeps_the_newest_and_reports_the_rest():
    messages = [_message(body=str(index), minutes=index) for index in range(5)]

    window = build_window(messages, message_limit=2, token_budget=1000)

    assert [turn.text for turn in window.turns] == ["3", "4"]
    assert window.dropped == 3


def test_history_stays_contiguous_when_the_budget_runs_out():
    """Older messages are dropped together, not selectively.

    A gap in the middle would read to the model as the customer changing
    subject, which is worse than a shorter history.
    """
    long_text = "a" * 400
    messages = [
        _message(body="tiny", minutes=0),
        _message(body=long_text, minutes=1),
        _message(body="newest", minutes=2),
    ]

    window = build_window(messages, message_limit=10, token_budget=30)

    assert [turn.text for turn in window.turns] == ["newest"]
    assert window.dropped == 2


def test_the_newest_message_survives_a_budget_too_small_to_hold_it():
    window = build_window(
        [_message(body="a" * 4000, minutes=0)],
        message_limit=10,
        token_budget=1,
    )

    assert len(window.turns) == 1


def test_failed_outbound_messages_are_not_shown_to_the_model():
    """The customer never saw them, so the agent must not think it replied."""
    window = build_window(
        [
            _message(body="question", minutes=0),
            _message(
                body="never arrived",
                minutes=1,
                direction=MessageDirection.OUTBOUND,
                status=MessageStatus.FAILED,
            ),
        ],
        message_limit=10,
        token_budget=1000,
    )

    assert [turn.text for turn in window.turns] == ["question"]


def test_media_without_text_becomes_a_readable_placeholder():
    window = build_window(
        [_message(body=None, kind=MessageKind.IMAGE)],
        message_limit=10,
        token_budget=1000,
    )

    assert window.turns[0].text == "[image]"


def test_a_window_with_nothing_usable_is_empty():
    window = build_window([], message_limit=10, token_budget=1000)

    assert window.is_empty
    assert window.estimated_tokens == 0
