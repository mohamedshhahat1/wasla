"""The rules a sentiment reading is worth anything for.

Two of them carry the whole phase: which readings count as bad enough to act
on, and the fact that priority only ever goes up on its own.
"""

from __future__ import annotations

import pytest

from app.db.models.conversation import Conversation
from app.db.models.sentiment import (
    PRIORITY_RANK,
    SENTIMENT_PRIORITY,
    SENTIMENT_SEVERITY,
    ConversationPriority,
    SentimentLabel,
    is_at_least,
    raised_priority,
)


def test_every_label_has_a_severity() -> None:
    """A label added later must be placed deliberately, not sort as harmless."""
    assert set(SENTIMENT_SEVERITY) == set(SentimentLabel)
    assert set(PRIORITY_RANK) == set(ConversationPriority)


def test_severity_runs_from_pleased_to_furious() -> None:
    ordered = [
        SentimentLabel.POSITIVE,
        SentimentLabel.NEUTRAL,
        SentimentLabel.NEGATIVE,
        SentimentLabel.ANGRY,
    ]
    severities = [SENTIMENT_SEVERITY[label] for label in ordered]
    assert severities == sorted(severities)
    assert len(set(severities)) == len(ordered)


def test_a_threshold_matches_itself_and_anything_worse() -> None:
    assert is_at_least(SentimentLabel.ANGRY, SentimentLabel.ANGRY) is True
    assert is_at_least(SentimentLabel.ANGRY, SentimentLabel.NEGATIVE) is True
    assert is_at_least(SentimentLabel.NEGATIVE, SentimentLabel.NEGATIVE) is True


def test_a_threshold_does_not_match_something_milder() -> None:
    assert is_at_least(SentimentLabel.NEGATIVE, SentimentLabel.ANGRY) is False
    assert is_at_least(SentimentLabel.NEUTRAL, SentimentLabel.NEGATIVE) is False
    assert is_at_least(SentimentLabel.POSITIVE, SentimentLabel.NEUTRAL) is False


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (SentimentLabel.ANGRY, ConversationPriority.URGENT),
        (SentimentLabel.NEGATIVE, ConversationPriority.HIGH),
    ],
)
def test_an_unhappy_customer_raises_the_priority(
    label: SentimentLabel, expected: ConversationPriority
) -> None:
    assert raised_priority(ConversationPriority.NORMAL, label) is expected


def test_a_happy_customer_does_not_touch_the_priority() -> None:
    """Nothing here is a reason to undo what somebody set by hand."""
    for label in (SentimentLabel.POSITIVE, SentimentLabel.NEUTRAL):
        assert raised_priority(ConversationPriority.URGENT, label) is ConversationPriority.URGENT
        assert raised_priority(ConversationPriority.NORMAL, label) is ConversationPriority.NORMAL


def test_priority_is_never_lowered_by_a_milder_reading() -> None:
    """The case this rule exists for.

    A customer who was furious and is now merely unhappy has not stopped being a
    problem. Demoting the conversation would quietly drop it out of the queue
    somebody is working, which is how an escalation gets lost.
    """
    assert (
        raised_priority(ConversationPriority.URGENT, SentimentLabel.NEGATIVE)
        is ConversationPriority.URGENT
    )


def test_only_bad_readings_imply_a_priority_at_all() -> None:
    assert set(SENTIMENT_PRIORITY) == {SentimentLabel.NEGATIVE, SentimentLabel.ANGRY}


def test_a_new_conversation_starts_normal_and_unflagged() -> None:
    conversation = Conversation(priority=ConversationPriority.NORMAL)
    assert conversation.needs_attention is False


def test_a_raised_conversation_asks_for_attention() -> None:
    for priority in (ConversationPriority.HIGH, ConversationPriority.URGENT):
        assert Conversation(priority=priority).needs_attention is True
