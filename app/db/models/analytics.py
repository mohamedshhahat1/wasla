"""Occurrences the rest of the schema does not already timestamp.

This table is deliberately small, and the reason is worth stating plainly
(ADR-028): almost everything a tenant dashboard reports is already a row
somewhere. Messages are in `messages`, leads in `leads` and `lead_activities`,
angry customers in `message_sentiments`, broadcast outcomes in
`campaign_recipients`. Writing a second copy of those as analytics events would
mean two shapes to migrate, two places to fix a count, and two answers to the
same question when they drift.

What is *not* recorded anywhere is a handoff. `conversations.mode` and
`handoff_reason` describe the state a conversation is in now; they cannot say
when it changed, how often, or who decided it - and "how many conversations did
somebody have to take over last week, and why" is a question a business asks
before it asks anything else about its AI.

So this table records events that leave no other trace, starting with that one.
It is append-only for the same reason `usage_events` is: an event is something
that happened, and a past figure that can change is not a figure.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type


class AnalyticsEventType(StrEnum):
    """What happened.

    Two members, not thirteen. A member is added when something happens that no
    other table records - not to mirror a count that `messages` or `leads`
    already answers.
    """

    HANDOFF = "handoff"
    HANDOFF_RESUMED = "handoff_resumed"


class AnalyticsSource(StrEnum):
    """Who or what decided it.

    A dashboard groups by this before anything else: a business that discovers
    its agents are handing over half their conversations has a different problem
    from one whose staff are taking conversations over by hand, and the counts
    are identical without this column.
    """

    AGENT = "agent"
    SENTIMENT = "sentiment"
    USER = "user"
    SYSTEM = "system"


ANALYTICS_EVENT_TYPE = _enum_type(AnalyticsEventType, name="analytics_event_type")
ANALYTICS_SOURCE = _enum_type(AnalyticsSource, name="analytics_source")


class AnalyticsEvent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin):
    """One recorded occurrence in one workspace."""

    __tablename__ = "analytics_events"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        Index("ix_analytics_events_tenant_id", "tenant_id"),
        Index("ix_analytics_events_tenant_id_occurred_at", "tenant_id", "occurred_at"),
        Index(
            "ix_analytics_events_tenant_id_event_type_occurred_at",
            "tenant_id",
            "event_type",
            "occurred_at",
        ),
        # The per-conversation history: why was this one taken over, and how
        # many times has it bounced between a person and the agent.
        Index("ix_analytics_events_conversation_id", "conversation_id"),
    )

    event_type: Mapped[AnalyticsEventType] = mapped_column(ANALYTICS_EVENT_TYPE, nullable=False)
    source: Mapped[AnalyticsSource] = mapped_column(ANALYTICS_SOURCE, nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The person who did it, when a person did. Null for anything the system
    # decided, which is not the same as unknown: the source column says which.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Whatever makes the event explicable later - the handoff reason, the
    # sentiment that triggered it. Never load-bearing.
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return (
            f"AnalyticsEvent(tenant_id={self.tenant_id!r}, "
            f"event_type={self.event_type!r}, source={self.source!r})"
        )
