"""Analytics API contracts.

Grouped the way a dashboard is laid out, because a flat bag of thirty numbers
makes every consumer invent its own grouping and none of them agree.

Rates are returned alongside the counts they are computed from, never instead of
them. A rate on its own cannot be checked, cannot be re-aggregated across two
windows, and hides the difference between "nine of ten" and "nine hundred of a
thousand".
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel

from app.db.models.analytics import AnalyticsEvent, AnalyticsEventType, AnalyticsSource
from app.db.models.lead import LeadStatus
from app.db.models.sentiment import SentimentLabel
from app.repositories.analytics_repository import EventCount
from app.repositories.metrics_repository import (
    CampaignMetrics,
    ConversationMetrics,
    LeadMetrics,
    MessageMetrics,
    SentimentMetrics,
)
from app.schemas.usage import WindowRead
from app.services.analytics_service import TenantAnalytics


class ConversationMetricsRead(BaseModel):
    """Volume and outcome for the conversations opened in the window."""

    created: int
    handed_off: int
    escalated: int
    ai_resolved: int
    ai_resolution_rate: float

    @classmethod
    def from_metrics(cls, metrics: ConversationMetrics) -> Self:
        return cls(
            created=metrics.created,
            handed_off=metrics.handed_off,
            escalated=metrics.escalated,
            ai_resolved=metrics.ai_resolved,
            ai_resolution_rate=metrics.ai_resolution_rate,
        )


class MessageMetricsRead(BaseModel):
    """Traffic, and how long customers waited.

    `average_response_seconds` is null when nothing in the window was answered -
    not zero, which would read as instant service. `unanswered` counts customers
    who started a conversation and are still waiting.
    """

    received: int
    sent: int
    failed: int
    average_response_seconds: float | None
    unanswered: int

    @classmethod
    def from_metrics(cls, metrics: MessageMetrics) -> Self:
        return cls(
            received=metrics.received,
            sent=metrics.sent,
            failed=metrics.failed,
            average_response_seconds=metrics.average_response_seconds,
            unanswered=metrics.unanswered,
        )


class LeadMetricsRead(BaseModel):
    """Pipeline created in the window."""

    created: int
    qualified: int
    won: int
    lost: int
    conversion_rate: float
    by_status: dict[LeadStatus, int]

    @classmethod
    def from_metrics(cls, metrics: LeadMetrics) -> Self:
        return cls(
            created=metrics.created,
            qualified=metrics.qualified,
            won=metrics.won,
            lost=metrics.lost,
            conversion_rate=metrics.conversion_rate,
            # Every status named, including the empty ones, so a dashboard can
            # render the column without knowing the vocabulary.
            by_status={status: metrics.by_status.get(status, 0) for status in LeadStatus},
        )


class SentimentMetricsRead(BaseModel):
    """How customers sounded.

    `unhappy_conversations` counts conversations, not readings: a customer who
    complains six times is one unhappy customer.
    """

    readings: int
    unhappy_conversations: int
    by_label: dict[SentimentLabel, int]

    @classmethod
    def from_metrics(cls, metrics: SentimentMetrics) -> Self:
        return cls(
            readings=metrics.readings,
            unhappy_conversations=metrics.unhappy_conversations,
            by_label={label: metrics.by_label.get(label, 0) for label in SentimentLabel},
        )


class CampaignMetricsRead(BaseModel):
    """Broadcast outcomes. `delivered` is Meta's word, read from the messages."""

    sent: int
    delivered: int
    failed: int
    skipped: int

    @classmethod
    def from_metrics(cls, metrics: CampaignMetrics) -> Self:
        return cls(
            sent=metrics.sent,
            delivered=metrics.delivered,
            failed=metrics.failed,
            skipped=metrics.skipped,
        )


class HandoffCountRead(BaseModel):
    """Handoffs in the window, split by who decided."""

    source: AnalyticsSource
    count: int

    @classmethod
    def from_count(cls, row: EventCount) -> Self:
        return cls(source=row.source, count=row.count)


class TenantAnalyticsRead(BaseModel):
    """One workspace's numbers for one window."""

    window: WindowRead
    conversations: ConversationMetricsRead
    messages: MessageMetricsRead
    leads: LeadMetricsRead
    sentiment: SentimentMetricsRead
    campaigns: CampaignMetricsRead
    handoffs_by_source: list[HandoffCountRead]

    @classmethod
    def from_report(cls, report: TenantAnalytics) -> Self:
        return cls(
            window=WindowRead.from_window(report.window),
            conversations=ConversationMetricsRead.from_metrics(report.conversations),
            messages=MessageMetricsRead.from_metrics(report.messages),
            leads=LeadMetricsRead.from_metrics(report.leads),
            sentiment=SentimentMetricsRead.from_metrics(report.sentiment),
            campaigns=CampaignMetricsRead.from_metrics(report.campaigns),
            handoffs_by_source=[HandoffCountRead.from_count(row) for row in report.handoffs],
        )


class AnalyticsEventRead(BaseModel):
    """One recorded occurrence, for a conversation's own history."""

    id: str
    event_type: AnalyticsEventType
    source: AnalyticsSource
    conversation_id: str | None
    actor_id: str | None
    occurred_at: datetime
    reason: str | None

    @classmethod
    def from_model(cls, event: AnalyticsEvent) -> Self:
        meta = event.meta or {}
        reason = meta.get("reason")
        return cls(
            id=str(event.id),
            event_type=event.event_type,
            source=event.source,
            conversation_id=str(event.conversation_id) if event.conversation_id else None,
            actor_id=str(event.actor_id) if event.actor_id else None,
            occurred_at=event.occurred_at,
            reason=reason if isinstance(reason, str) else None,
        )


__all__ = [
    "AnalyticsEventRead",
    "CampaignMetricsRead",
    "ConversationMetricsRead",
    "HandoffCountRead",
    "LeadMetricsRead",
    "MessageMetricsRead",
    "SentimentMetricsRead",
    "TenantAnalyticsRead",
]
