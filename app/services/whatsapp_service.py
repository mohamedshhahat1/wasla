"""Inbound WhatsApp ingestion: Meta's webhook, parsed, then ingested like any channel's.

What this module used to hold - ownership resolution, event storage, projection,
hand-offs, follow-up cancellation, opt-out, settlement - is channel-neutral and
lives in `ChannelIngestionService` (OMNI-006). What stays WhatsApp's is the
parsing, which is the WhatsApp adapter's (`WhatsAppAdapter.parse`), and this
name, which the webhook route and the suites that drive it have always used.

The properties it is responsible for are unchanged and are `ChannelIngestionService`'s
to keep: the workspace is the one that held the number when the event happened
(MSG-01, ADR-101); a stored event that still owes work says so (MSG-02,
ADR-102); a refused message is contained (MEDIA-05); only a new customer
message has consequences (OMNI-005); and nothing Meta sent disappears unseen -
a username sender is a sender (OMNI-002), and what the parser refuses is
counted (OMNI-010).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.services.channel_ingestion_service import (
    AGENT_NOT_QUEUED,
    MEDIA_NOT_QUEUED,
    ChannelIngestionService,
    IngestionOutcome,
)
from app.workers.media_queue import MediaQueue
from app.workers.queue import AgentQueue


class WhatsAppIngestionService:
    """Turns one WhatsApp webhook delivery into stored events and conversation rows.

    The queues are optional. Without them nothing is asked to answer, but
    events are still stored and projected, which is all the projection tests
    need.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        queue: AgentQueue | None = None,
        media_queue: MediaQueue | None = None,
    ) -> None:
        self._adapter = WhatsAppAdapter()
        self._ingestion = ChannelIngestionService(
            session=session,
            adapter=self._adapter,
            queue=queue,
            media_queue=media_queue,
        )

    async def ingest(self, payload: Mapping[str, Any]) -> IngestionOutcome:
        return await self._ingestion.ingest(self._adapter.parse(payload))


__all__ = [
    "AGENT_NOT_QUEUED",
    "MEDIA_NOT_QUEUED",
    "IngestionOutcome",
    "WhatsAppIngestionService",
]
