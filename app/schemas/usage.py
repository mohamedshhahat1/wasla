"""Usage API contracts.

Read models map field by field, as everywhere else, so a column added to
`usage_events` never silently widens the API.

Every response carries the window it covers. A client that asked for defaults
gets back the exact bounds that were applied, which is what makes a figure
quotable: "1,204 messages" means nothing without "between these two instants".
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel

from app.db.models.usage import UsageEventType, UsageUnit
from app.repositories.usage_repository import UsagePoint, UsageTotal
from app.services.usage_service import UsageSummary, UsageWindow


class WindowRead(BaseModel):
    """The half-open window `[since, until)` a figure covers."""

    since: datetime
    until: datetime

    @classmethod
    def from_window(cls, window: UsageWindow) -> Self:
        return cls(since=window.since, until=window.until)


class UsageTotalRead(BaseModel):
    """One meter's consumption."""

    event_type: UsageEventType
    unit: UsageUnit
    quantity: int
    events: int

    @classmethod
    def from_total(cls, total: UsageTotal) -> Self:
        return cls(
            event_type=total.event_type,
            unit=total.unit,
            quantity=total.quantity,
            events=total.events,
        )


class UsageCounters(BaseModel):
    """The named counters, which are what a plan limit is written against.

    A meter with nothing recorded reads as zero rather than being absent, so a
    dashboard renders an empty month without knowing the vocabulary.
    """

    messages_received: int
    messages_sent: int
    ai_requests: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    rag_queries: int
    media_processed: int
    voice_transcriptions: int
    storage_bytes: int
    leads_created: int
    conversations_created: int
    campaign_messages: int
    api_requests: int


class UsageSummaryRead(BaseModel):
    """Everything one workspace consumed in a window."""

    window: WindowRead
    counters: UsageCounters
    totals: list[UsageTotalRead]

    @classmethod
    def from_summary(cls, summary: UsageSummary) -> Self:
        return cls(
            window=WindowRead.from_window(summary.window),
            counters=UsageCounters(
                messages_received=summary.messages_received,
                messages_sent=summary.messages_sent,
                ai_requests=summary.ai_requests,
                input_tokens=summary.input_tokens,
                output_tokens=summary.output_tokens,
                total_tokens=summary.total_tokens,
                rag_queries=summary.rag_queries,
                media_processed=summary.media_processed,
                voice_transcriptions=summary.voice_transcriptions,
                storage_bytes=summary.storage_bytes,
                leads_created=summary.leads_created,
                conversations_created=summary.conversations_created,
                campaign_messages=summary.campaign_messages,
                api_requests=summary.api_requests,
            ),
            totals=[UsageTotalRead.from_total(total) for total in summary.totals],
        )


class UsagePointRead(BaseModel):
    """One day of one meter."""

    day: datetime
    event_type: UsageEventType
    quantity: int

    @classmethod
    def from_point(cls, point: UsagePoint) -> Self:
        return cls(day=point.day, event_type=point.event_type, quantity=point.quantity)


class UsageSeriesRead(BaseModel):
    """A daily series, for drawing.

    Sparse: a day on which nothing happened has no point. Filling in zeros is
    the client's job, because only the client knows whether it is drawing bars
    (which want the gaps) or a cumulative line (which does not).
    """

    window: WindowRead
    points: list[UsagePointRead]

    @classmethod
    def from_points(cls, window: UsageWindow, points: list[UsagePoint]) -> Self:
        return cls(
            window=WindowRead.from_window(window),
            points=[UsagePointRead.from_point(point) for point in points],
        )


__all__ = [
    "UsageCounters",
    "UsagePointRead",
    "UsageSeriesRead",
    "UsageSummaryRead",
    "UsageTotalRead",
    "WindowRead",
]
