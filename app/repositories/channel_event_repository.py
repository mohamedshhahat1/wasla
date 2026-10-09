"""The inbound event log, for every channel: storing, settling, recovering, redacting.

Moved here from the WhatsApp repository because nothing in it is WhatsApp's
(OMNI-007): a stored event that still owes work is finished by the same sweep
(ADR-102), and a processed event's payload is cleared by the same retention rule
(DB-011), whichever adapter stored it.

**Idempotency is per connection** (ADR-120): an event is the same event when the
same connection delivers the same id. The workspace-wide key that preceded it is
still in the schema until the compatibility cleanup, and it is stricter, so an
id already used by *another* connection of the workspace is refused by it; that
is reported as a collision - counted, never silently dropped and never filed on
the other connection's event.

**Nothing refused disappears** (OMNI-043). The refused delivery is kept as
`failed` evidence on its own connection, payload and all, under a key of its own
(`collision:<connection>:<event id>`) that the workspace-wide key cannot refuse:
for Meta's globally unique ids a collision is an anomaly worth reading, and for
a provider whose ids are unique only per connection it would otherwise be a
customer's message lost without trace. Evidence is never projected, never
recovered and never redacted, like every failed event.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import ColumnElement, func, null, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.logging import get_logger
from app.db.models.channel import Channel
from app.db.models.channel_event import ChannelEvent, ChannelEventKind, ChannelEventState
from app.repositories.base import BaseRepository, TenantScopedRepository

logger = get_logger(__name__)


#: The prefix of a collided delivery's evidence key (OMNI-043).
COLLISION_EVIDENCE_PREFIX = "collision:"
#: Why collision evidence was failed - a bounded token, as every event error is.
EVENT_ID_COLLISION = "event_id_collision"
_EVENT_ID_LENGTH = 255


def collision_evidence_key(connection_id: uuid.UUID, event_id: str) -> str:
    """The key a collided delivery is kept under: its connection and its own id.

    Within the event id column's 255 characters; an id too long to fit beside
    its prefix is kept by its SHA-256, the payload holding the original.
    """
    key = f"{COLLISION_EVIDENCE_PREFIX}{connection_id.hex}:{event_id}"
    if len(key) <= _EVENT_ID_LENGTH:
        return key
    digest = sha256(event_id.encode()).hexdigest()
    return f"{COLLISION_EVIDENCE_PREFIX}{connection_id.hex}:sha256:{digest}"


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """What storing one event came to.

    `event` is None exactly when the id collided with another connection's
    event in this workspace (the legacy key refused it); `evidence` is then the
    failed row the delivery was kept as (OMNI-043).
    """

    event: ChannelEvent | None
    created: bool
    evidence: ChannelEvent | None = None

    @property
    def collided(self) -> bool:
        return self.event is None


class ChannelEventRepository(TenantScopedRepository[ChannelEvent]):
    """The append-only inbound log for one workspace."""

    model = ChannelEvent

    def _tenant_filter(self) -> ColumnElement[bool]:
        return ChannelEvent.tenant_id == self.tenant_id

    async def get(self, *, connection_id: uuid.UUID, event_id: str) -> ChannelEvent | None:
        return await self._first(
            self._select().where(
                ChannelEvent.account_id == connection_id,
                ChannelEvent.event_id == event_id,
            )
        )

    async def get_by_event_id(self, event_id: str) -> ChannelEvent | None:
        """By the provider's id alone, across the workspace's connections (legacy scope)."""
        return await self._first(self._select().where(ChannelEvent.event_id == event_id))

    async def list_recent(self, *, limit: int = 50) -> list[ChannelEvent]:
        return await self._all(
            self._select()
            .order_by(ChannelEvent.received_at.desc(), ChannelEvent.id.desc())
            .limit(limit)
        )

    async def store(
        self,
        *,
        connection_id: uuid.UUID,
        channel: Channel,
        event_id: str,
        kind: ChannelEventKind,
        payload: dict[str, Any],
        received_at: datetime,
    ) -> StoredEvent:
        """Store an event once per connection. Race-safe; never raises for a duplicate.

        The read is the fast path; the unique keys are the guarantee.
        `ON CONFLICT DO NOTHING` with no target yields to either key, so two
        simultaneous deliveries of one event converge on one row and the loser
        reads the winner back (MSG-09). A conflict that leaves nothing to read
        back on this connection was the workspace-wide key refusing an id
        another connection already holds - a collision, returned as such.
        """
        existing = await self.get(connection_id=connection_id, event_id=event_id)
        if existing is not None:
            return StoredEvent(event=existing, created=False)

        statement = (
            pg_insert(ChannelEvent)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                account_id=connection_id,
                channel=channel,
                event_id=event_id,
                kind=kind,
                state=ChannelEventState.RECEIVED,
                payload=payload,
                received_at=received_at,
            )
            .on_conflict_do_nothing()
            .returning(ChannelEvent.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        # Read back rather than constructed, so the caller holds the session's
        # mapped row: it advances the state, and a detached copy would drop
        # that write on the floor.
        stored = await self.get(connection_id=connection_id, event_id=event_id)
        if stored is None:
            logger.warning(
                "channel.event_id_collision",
                extra={"event": "channel.event_id_collision", "channel": channel.value},
            )
            evidence = await self._keep_as_evidence(
                connection_id=connection_id,
                channel=channel,
                event_id=event_id,
                kind=kind,
                payload=payload,
                received_at=received_at,
            )
            return StoredEvent(event=None, created=False, evidence=evidence)
        return StoredEvent(event=stored, created=inserted is not None)

    async def _keep_as_evidence(
        self,
        *,
        connection_id: uuid.UUID,
        channel: Channel,
        event_id: str,
        kind: ChannelEventKind,
        payload: dict[str, Any],
        received_at: datetime,
    ) -> ChannelEvent | None:
        """Store a delivery the workspace-wide key refused as failed evidence (OMNI-043).

        Idempotent: a replay of the same collided delivery finds its evidence
        and writes nothing.
        """
        key = collision_evidence_key(connection_id, event_id)
        now = datetime.now(UTC)
        await self.session.execute(
            pg_insert(ChannelEvent)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                account_id=connection_id,
                channel=channel,
                event_id=key,
                kind=kind,
                state=ChannelEventState.FAILED,
                payload=payload,
                received_at=received_at,
                processed_at=now,
                error=EVENT_ID_COLLISION,
            )
            .on_conflict_do_nothing()
        )
        return await self.get(connection_id=connection_id, event_id=key)

    def mark_processed(self, event: ChannelEvent) -> ChannelEvent:
        """Every handoff this event needed has been made (MSG-02)."""
        event.state = ChannelEventState.PROCESSED
        event.processed_at = datetime.now(UTC)
        event.error = None
        return event

    def mark_unprocessed(self, event: ChannelEvent, *, reason: str) -> ChannelEvent:
        """Stored, and something downstream did not happen: left for the sweep (ADR-102)."""
        event.state = ChannelEventState.RECEIVED
        event.error = reason[:500]
        return event

    def mark_failed(self, event: ChannelEvent, *, reason: str) -> ChannelEvent:
        """Can never be processed, and no retry will change that. Terminal."""
        event.state = ChannelEventState.FAILED
        event.processed_at = datetime.now(UTC)
        event.error = reason[:500]
        return event


class WebhookPayloadRetention(BaseRepository[ChannelEvent]):
    """The unscoped write that clears old raw webhook payloads (DB-011), every channel's alike.

    Unscoped for the same reason as `InboundEventSweep`: retention is a
    platform-wide rule. It clears only the payload of an event that has been
    processed and is older than the window; the event's identity - its id,
    event id, connection, state and timestamps - stays, because that is what
    deduplicates a provider's retry and what recovery reads.
    """

    model = ChannelEvent

    async def redact(self, *, older_than: datetime, now: datetime, limit: int) -> int:
        """Clear one batch of payloads; return how many were cleared. SKIP LOCKED."""
        eligible = (
            select(ChannelEvent.id)
            .where(
                ChannelEvent.state == ChannelEventState.PROCESSED,
                ChannelEvent.payload.is_not(None),
                ChannelEvent.processed_at < older_than,
            )
            .order_by(ChannelEvent.processed_at, ChannelEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        result = await self.session.execute(
            update(ChannelEvent)
            .where(ChannelEvent.id.in_(eligible))
            # SQL NULL, spelled out: the JSONB type writes a Python None as the
            # JSON value `null`, which IS NOT NULL.
            .values(payload=null(), payload_redacted_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def pending(self, *, older_than: datetime) -> int:
        """Processed events past the window that still hold a payload."""
        count = await self.session.scalar(
            select(func.count(ChannelEvent.id)).where(
                ChannelEvent.state == ChannelEventState.PROCESSED,
                ChannelEvent.payload.is_not(None),
                ChannelEvent.processed_at < older_than,
            )
        )
        return int(count or 0)


class InboundEventSweep(BaseRepository[ChannelEvent]):
    """The unscoped read over the inbound log, for the recovery sweep and its gauge only.

    A backlog of unfinished inbound work is a platform-wide condition - the
    Redis outage that produced it did not choose a workspace or a channel.
    Nothing here is reachable from an API route.
    """

    model = ChannelEvent

    async def claim_unprocessed(self, *, older_than: datetime, limit: int) -> list[ChannelEvent]:
        """Events still owing work, oldest first, locked so one sweeper gets each.

        `FOR UPDATE SKIP LOCKED`: two sweepers recovering one event would
        enqueue one agent turn twice. `older_than` keeps the sweep off events
        still in flight, measured from `created_at` - ours - rather than the
        provider's `received_at`, which a late redelivery carries from days ago.
        """
        return await self._all(
            self._select()
            .where(
                ChannelEvent.state == ChannelEventState.RECEIVED,
                ChannelEvent.created_at < older_than,
            )
            .order_by(ChannelEvent.created_at, ChannelEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    async def backlog(self, *, older_than: datetime) -> tuple[int, float]:
        """How many events still owe work, and how old the oldest one is."""
        rows = await self.session.execute(
            select(func.count(ChannelEvent.id), func.min(ChannelEvent.created_at)).where(
                ChannelEvent.state == ChannelEventState.RECEIVED,
                ChannelEvent.created_at < older_than,
            )
        )
        count, oldest = rows.one()
        if not count or oldest is None:
            return 0, 0.0
        return int(count), max((datetime.now(UTC) - oldest).total_seconds(), 0.0)

    async def backlog_by_channel(self, *, older_than: datetime) -> dict[str, int]:
        """The same backlog, split by channel - a closed label, never a connection."""
        rows = await self.session.execute(
            select(ChannelEvent.channel, func.count(ChannelEvent.id))
            .where(
                ChannelEvent.state == ChannelEventState.RECEIVED,
                ChannelEvent.created_at < older_than,
            )
            .group_by(ChannelEvent.channel)
        )
        return {channel.value: int(count) for channel, count in rows.all()}


__all__ = [
    "ChannelEventRepository",
    "InboundEventSweep",
    "StoredEvent",
    "WebhookPayloadRetention",
]
