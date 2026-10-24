"""A run of a workspace's automatic follow-ups on one conversation (FUS-02, ADR-151).

A run starts when the AI (or a person on an AI-owned conversation) answers a
customer and the workspace's reminders cover the conversation's platform; its
moment is the run's anchor. Each reminder is one `follow_ups` row with origin
`sequence`, scheduled only when the one before it was sent, so the
one-pending-per-conversation index still holds: only the next step ever waits.

A run ends - the waiting step cancelled or skipped - on every stop rule of
FUS-06, with the reason in `end_reason`. At most one run per conversation is
active, enforced by a partial unique index.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class FollowUpOrigin(StrEnum):
    """Whether a follow-up is a one-off (a person's or the agent's) or a run's step."""

    ONE_OFF = "one_off"
    SEQUENCE = "sequence"


class SequenceRunState(StrEnum):
    ACTIVE = "active"
    ENDED = "ended"


class SequenceEndReason(StrEnum):
    """Why a run ended (FUS-06). A closed vocabulary: the page has a sentence for each."""

    COMPLETED = "completed"
    CUSTOMER_REPLIED = "customer_replied"
    HUMAN_TOOK_OVER = "human_took_over"
    CONVERSATION_CLOSED = "conversation_closed"
    OPTED_OUT = "opted_out"
    LEAD_CLOSED = "lead_closed"
    PURCHASED = "purchased"
    NOT_SERVED = "not_served"
    ALLOWANCE_EXHAUSTED = "allowance_exhausted"
    SETTINGS_CHANGED = "settings_changed"
    AI_SKIPPED = "ai_skipped"
    WINDOW_CLOSING = "window_closing"
    WINDOW_CLOSED = "window_closed"
    AI_UNAVAILABLE = "ai_unavailable"
    GUARDRAIL = "guardrail"
    SEQUENCE_RESTARTED = "sequence_restarted"
    REPLACED_BY_PERSON = "replaced_by_person"
    STOPPED_BY_PERSON = "stopped_by_person"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    FAILED = "failed"


class SequenceSkipReason(StrEnum):
    """Why a run's step was not sent though it came due (FUS-04, FUS-05, FUS-06).

    The first six are the AI's own answers; the rest are Wasla's.
    """

    CONCLUDED = "concluded"
    PURCHASED = "purchased"
    DECLINED = "declined"
    WAITING_ON_BUSINESS = "waiting_on_business"
    INAPPROPRIATE = "inappropriate"
    OTHER = "other"
    GUARDRAIL = "guardrail"
    WINDOW_CLOSED = "window_closed"
    AI_UNAVAILABLE = "ai_unavailable"
    ALLOWANCE_EXHAUSTED = "allowance_exhausted"
    NOT_SERVED = "not_served"
    HUMAN_TOOK_OVER = "human_took_over"
    CONVERSATION_CLOSED = "conversation_closed"
    OPTED_OUT = "opted_out"
    LEAD_CLOSED = "lead_closed"
    CHANNEL_UNAVAILABLE = "channel_unavailable"


#: The skip reasons the AI may answer with (FUS-04).
AI_SKIP_REASONS: Final[frozenset[SequenceSkipReason]] = frozenset(
    {
        SequenceSkipReason.CONCLUDED,
        SequenceSkipReason.PURCHASED,
        SequenceSkipReason.DECLINED,
        SequenceSkipReason.WAITING_ON_BUSINESS,
        SequenceSkipReason.INAPPROPRIATE,
        SequenceSkipReason.OTHER,
    }
)


def end_reason_for(reason: SequenceSkipReason) -> SequenceEndReason:
    """The reason a run ends with when its step is skipped for `reason`."""
    if reason in AI_SKIP_REASONS and reason is not SequenceSkipReason.PURCHASED:
        return SequenceEndReason.AI_SKIPPED
    return SequenceEndReason(reason.value)


#: `cancelled_reason` codes a run's step (or a follow-up a run replaced) is cancelled with.
SEQUENCE_CANCEL_REASONS: Final[frozenset[str]] = frozenset(
    {
        "customer_replied",
        "human_took_over",
        "conversation_closed",
        "opted_out",
        "lead_closed",
        "settings_changed",
        "sequence_restarted",
        "replaced_by_person",
        "replaced_by_sequence",
        "stopped_by_person",
    }
)


def _in(values: frozenset[str] | type[StrEnum]) -> str:
    names = sorted(str(value) for value in values)
    return ", ".join(f"'{name}'" for name in names)


class FollowUpSequenceRun(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One conversation's run of the workspace's reminders, from its anchor to its end."""

    __tablename__ = "follow_up_sequence_runs"
    __table_args__ = (
        Index("ix_follow_up_sequence_runs_tenant_id", "tenant_id"),
        Index("ix_follow_up_sequence_runs_conversation_id", "conversation_id"),
        Index(
            "uq_follow_up_sequence_runs_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
        ),
        UniqueConstraint("tenant_id", "id", name="uq_follow_up_sequence_runs_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_follow_up_sequence_runs_tenant_conversation",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"state IN ({_in(SequenceRunState)})", name="state_known"),
        CheckConstraint(
            f"end_reason IS NULL OR end_reason IN ({_in(SequenceEndReason)})",
            name="end_reason_known",
        ),
        CheckConstraint(
            "(state = 'active') = (end_reason IS NULL AND ended_at IS NULL)",
            name="ended_has_reason",
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The outbound reply the run counts from (FUS-02): "the timer".
    anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    end_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How many of its steps were sent: never more than the settings' maximum.
    steps_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @property
    def is_active(self) -> bool:
        return self.state == SequenceRunState.ACTIVE.value


__all__ = [
    "AI_SKIP_REASONS",
    "SEQUENCE_CANCEL_REASONS",
    "FollowUpOrigin",
    "FollowUpSequenceRun",
    "SequenceEndReason",
    "SequenceRunState",
    "SequenceSkipReason",
    "end_reason_for",
]
