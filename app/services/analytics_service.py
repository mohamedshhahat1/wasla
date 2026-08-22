"""Analytics: recording the few events that need it, and reporting on the rest.

`AnalyticsRecorder` is the write side, and it is narrow on purpose: almost
everything a dashboard reports is already a row in a domain table, so the only
events recorded here are the ones that leave no other trace (ADR-028).

`AnalyticsService` is the read side. It composes one report out of the metrics
repository's queries, over the same half-open window usage uses, so a figure on
the analytics page and a figure on the usage page cover exactly the same period.

Shaped exactly like `UsageRecorder`, and for the same reasons: synchronous, no
I/O, staged in the caller's transaction. A handoff that rolled back did not
happen, and an event written on its own connection would say it did.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analytics import AnalyticsEvent, AnalyticsEventType, AnalyticsSource
from app.repositories.analytics_repository import AnalyticsEventRepository, EventCount
from app.repositories.metrics_repository import (
    CampaignMetrics,
    ConversationMetrics,
    LeadMetrics,
    MessageMetrics,
    SentimentMetrics,
    TenantMetricsRepository,
)
from app.services.usage_service import UsageWindow, resolve_window

# A handoff reason is a sentence somebody typed or a model produced. Kept short
# in the event because it is already stored in full on the conversation; this
# copy exists so a historical row explains itself after the conversation has
# moved on.
MAX_REASON_LENGTH: Final = 200


class AnalyticsRecorder:
    """Stages analytics events for one workspace."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._events = AnalyticsEventRepository(session, tenant_id=tenant_id)

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._events.tenant_id

    def handoff(
        self,
        *,
        conversation_id: uuid.UUID,
        source: AnalyticsSource,
        reason: str | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> None:
        """A conversation moved to a person.

        `source` is the column a dashboard groups by first. A business whose
        agents hand over half their conversations has a different problem from
        one whose staff take them over by hand, and the totals are identical
        without it.
        """
        meta: dict[str, Any] | None = None
        if reason:
            meta = {"reason": reason[:MAX_REASON_LENGTH]}
        self._events.record(
            event_type=AnalyticsEventType.HANDOFF,
            source=source,
            conversation_id=conversation_id,
            actor_id=actor_id,
            meta=meta,
        )

    def handoff_resumed(
        self,
        *,
        conversation_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
    ) -> None:
        """A conversation given back to the agent.

        Always a person's decision today - nothing automatic hands a customer
        back - so the source is fixed rather than a parameter that could only
        ever be wrong.
        """
        self._events.record(
            event_type=AnalyticsEventType.HANDOFF_RESUMED,
            source=AnalyticsSource.USER,
            conversation_id=conversation_id,
            actor_id=actor_id,
        )


@dataclass(frozen=True, slots=True)
class TenantAnalytics:
    """One workspace's numbers for one window.

    Grouped rather than flattened, because the groups are how a dashboard is
    laid out and a flat bag of thirty fields makes every consumer invent its
    own grouping.
    """

    window: UsageWindow
    conversations: ConversationMetrics
    messages: MessageMetrics
    leads: LeadMetrics
    sentiment: SentimentMetrics
    campaigns: CampaignMetrics
    handoffs: tuple[EventCount, ...] = ()


class AnalyticsService:
    """Reports on one workspace."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._metrics = TenantMetricsRepository(session, tenant_id=tenant_id)
        self._events = AnalyticsEventRepository(session, tenant_id=tenant_id)

    async def report(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> TenantAnalytics:
        """Every figure for a window.

        Several queries rather than one, and deliberately so: each is an
        indexed range scan over one workspace's rows, and a single query
        joining messages to leads to sentiment would produce a plan nobody can
        read and a cartesian product somebody eventually has to debug.
        """
        window = resolve_window(since=since, until=until)
        bounds = {"since": window.since, "until": window.until}
        return TenantAnalytics(
            window=window,
            conversations=await self._metrics.conversations(**bounds),
            messages=await self._metrics.messages(**bounds),
            leads=await self._metrics.leads(**bounds),
            sentiment=await self._metrics.sentiment(**bounds),
            campaigns=await self._metrics.campaigns(**bounds),
            handoffs=tuple(
                await self._events.counts(
                    since=window.since,
                    until=window.until,
                    event_types=[AnalyticsEventType.HANDOFF],
                )
            ),
        )

    async def conversation_history(
        self,
        conversation_id: uuid.UUID,
        *,
        limit: int = 50,
    ) -> list[AnalyticsEvent]:
        """Why this one conversation ended up where it did."""
        return await self._events.list_for_conversation(conversation_id, limit=limit)
