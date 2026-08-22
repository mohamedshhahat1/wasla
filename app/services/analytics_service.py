"""Recording analytics events.

The write side only. Reporting reads the domain tables directly and is built on
top of this in the metrics service; what lives here is the narrow set of things
that leave no other trace (ADR-028).

Shaped exactly like `UsageRecorder`, and for the same reasons: synchronous, no
I/O, staged in the caller's transaction. A handoff that rolled back did not
happen, and an event written on its own connection would say it did.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analytics import AnalyticsEventType, AnalyticsSource
from app.repositories.analytics_repository import AnalyticsEventRepository

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
