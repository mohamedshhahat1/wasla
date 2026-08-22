"""How a customer sounds, and how urgent that makes their conversation.

One row per inbound message that was analysed, plus the current reading carried
on the conversation itself. The split is the same one media makes and for the
same reasons: `messages` is the table every conversation read touches, and the
readings need a history that a single current value cannot hold.

What each side is for:

- `Conversation.sentiment` and friends: the state an inbox sorts and filters by.
- `MessageSentiment`: what was concluded about one message, when, and by which
  model - the record that answers "why was this escalated" after the fact, and
  the time series analytics will count.

The unique constraint on `message_id` is the idempotency key. An agent job that
is retried must not pay for a second inference on a message already read, and it
is that constraint rather than a check in a service that guarantees it.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Final

from sqlalchemy import Boolean, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type


class SentimentLabel(StrEnum):
    """How a customer's message reads.

    `ANGRY` is deliberately separate from `NEGATIVE` rather than folded into a
    score. "This is broken" and "this is the third time I have asked" call for
    different handling, and a business configuring escalation needs to be able
    to say which one it means.
    """

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    ANGRY = "angry"


class ConversationPriority(StrEnum):
    """How urgently a conversation wants a person's attention.

    Three values, not five. Priority exists to sort one inbox, and a scale finer
    than a person can act on is a scale nobody maintains.
    """

    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


# Severity order, used to compare a reading against the threshold an agent is
# configured with. Written out rather than inferred from declaration order,
# because reordering the enum for any other reason would silently change which
# conversations escalate.
SENTIMENT_SEVERITY: Final[dict[SentimentLabel, int]] = {
    SentimentLabel.POSITIVE: 0,
    SentimentLabel.NEUTRAL: 1,
    SentimentLabel.NEGATIVE: 2,
    SentimentLabel.ANGRY: 3,
}

PRIORITY_RANK: Final[dict[ConversationPriority, int]] = {
    ConversationPriority.NORMAL: 0,
    ConversationPriority.HIGH: 1,
    ConversationPriority.URGENT: 2,
}

# What a reading raises the conversation to. Absent labels leave it alone: a
# cheerful message is not a reason to touch a priority somebody set.
SENTIMENT_PRIORITY: Final[dict[SentimentLabel, ConversationPriority]] = {
    SentimentLabel.NEGATIVE: ConversationPriority.HIGH,
    SentimentLabel.ANGRY: ConversationPriority.URGENT,
}

MAX_INTENT_LENGTH: Final = 120

SENTIMENT_LABEL_TYPE = _enum_type(SentimentLabel, name="sentiment_label")
CONVERSATION_PRIORITY_TYPE = _enum_type(ConversationPriority, name="conversation_priority")


def is_at_least(label: SentimentLabel, threshold: SentimentLabel) -> bool:
    """Whether `label` is as severe as `threshold`, or worse."""
    return SENTIMENT_SEVERITY[label] >= SENTIMENT_SEVERITY[threshold]


def raised_priority(
    current: ConversationPriority,
    label: SentimentLabel,
) -> ConversationPriority:
    """The priority a reading implies, never below what is already set.

    Sentiment raises priority and never lowers it. A customer who was furious
    five minutes ago and is now merely terse has not stopped being a problem,
    and a conversation quietly demoted out of somebody's queue is one nobody
    ever looks at again. Lowering it is a person's decision, made through the
    API.
    """
    implied = SENTIMENT_PRIORITY.get(label)
    if implied is None or PRIORITY_RANK[implied] <= PRIORITY_RANK[current]:
        return current
    return implied


class MessageSentiment(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """What was concluded about one inbound message."""

    __tablename__ = "message_sentiments"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_message_sentiments_message_id"),
        Index("ix_message_sentiments_tenant_id", "tenant_id"),
        Index("ix_message_sentiments_tenant_id_label", "tenant_id", "label"),
        Index("ix_message_sentiments_conversation_id", "conversation_id"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalised for the same reason the media row denormalises it: the
    # per-conversation history is read without joining back through messages.
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[SentimentLabel] = mapped_column(SENTIMENT_LABEL_TYPE, nullable=False)
    # -1 for as negative as it gets, +1 for as positive. Stored alongside the
    # label rather than instead of it, because the label is what rules are
    # written against and the score is what a chart is drawn from.
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    intent: Mapped[str | None] = mapped_column(String(MAX_INTENT_LENGTH), nullable=True)
    # The model's own estimate, 0 to 1. Weakly calibrated by nature, so it is
    # used only as a floor below which nothing is escalated - never as evidence
    # that a reading is right.
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Provenance. Readings taken by different models are not comparable, and a
    # workspace that changes model will see its numbers move for that reason
    # alone; without this there is no way to tell that from a change in mood.
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
