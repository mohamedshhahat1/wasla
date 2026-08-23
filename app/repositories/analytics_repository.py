"""Data access for analytics events.

Writes are staged, never committed, exactly as usage is: an event describes
something that happened in the caller's transaction, so it lives or dies with
it. A handoff that rolled back did not happen.

Reads are aggregates. The one exception is the per-conversation history, which
is genuinely a list of rows - "why did this conversation end up with a person"
is answered by reading them, not by counting them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, func, select

from app.db.models.analytics import AnalyticsEvent, AnalyticsEventType, AnalyticsSource
from app.repositories.base import TenantScopedRepository


@dataclass(frozen=True, slots=True)
class EventCount:
    """How many of one kind of event, from one kind of source."""

    event_type: AnalyticsEventType
    source: AnalyticsSource
    count: int


class AnalyticsEventRepository(TenantScopedRepository[AnalyticsEvent]):
    """Analytics events written and read for one workspace."""

    model = AnalyticsEvent

    def _tenant_filter(self) -> ColumnElement[bool]:
        return AnalyticsEvent.tenant_id == self.tenant_id

    def record(
        self,
        *,
        event_type: AnalyticsEventType,
        source: AnalyticsSource,
        conversation_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AnalyticsEvent:
        """Stage one occurrence. Synchronous, like the usage recorder."""
        event = AnalyticsEvent(
            tenant_id=self.tenant_id,
            event_type=event_type,
            source=source,
            conversation_id=conversation_id,
            actor_id=actor_id,
            occurred_at=occurred_at if occurred_at is not None else datetime.now(UTC),
            meta=meta,
        )
        return self.add(event)

    async def counts(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        event_types: Iterable[AnalyticsEventType] | None = None,
    ) -> list[EventCount]:
        """Counts per event type and source over a half-open window."""
        statement = (
            select(AnalyticsEvent.event_type, AnalyticsEvent.source, func.count())
            .where(self._tenant_filter())
            .group_by(AnalyticsEvent.event_type, AnalyticsEvent.source)
            .order_by(AnalyticsEvent.event_type, AnalyticsEvent.source)
        )
        if since is not None:
            statement = statement.where(AnalyticsEvent.occurred_at >= since)
        if until is not None:
            statement = statement.where(AnalyticsEvent.occurred_at < until)
        selected = list(event_types) if event_types is not None else None
        if selected is not None:
            statement = statement.where(AnalyticsEvent.event_type.in_(selected))

        result = await self.session.execute(statement)
        return [
            EventCount(event_type=row[0], source=row[1], count=int(row[2])) for row in result.all()
        ]

    async def conversations_touched(
        self,
        *,
        event_type: AnalyticsEventType,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        """How many *distinct* conversations had this event in the window.

        Different from a count of events, and both are worth having: a
        conversation handed over three times is three handoffs but one unhappy
        customer, and an AI resolution rate built on the first number would
        punish the same conversation repeatedly.
        """
        statement = (
            select(func.count(func.distinct(AnalyticsEvent.conversation_id)))
            .where(self._tenant_filter())
            .where(AnalyticsEvent.event_type == event_type)
            .where(AnalyticsEvent.conversation_id.is_not(None))
        )
        if since is not None:
            statement = statement.where(AnalyticsEvent.occurred_at >= since)
        if until is not None:
            statement = statement.where(AnalyticsEvent.occurred_at < until)
        return int(await self.session.scalar(statement) or 0)

    async def list_for_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        limit: int = 50,
    ) -> list[AnalyticsEvent]:
        """The history of one conversation, newest first."""
        return await self._all(
            self._select()
            .where(AnalyticsEvent.conversation_id == conversation_id)
            .order_by(AnalyticsEvent.occurred_at.desc(), AnalyticsEvent.id.desc())
            .limit(limit)
        )
