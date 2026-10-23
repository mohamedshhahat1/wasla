"""The inbox's event stream: tickets, one reader per workspace, and the SSE frames (RT-01, RT-05).

**A ticket, because EventSource cannot set headers** (WHATWG HTML, "Server-sent
events": the constructor takes a URL and `withCredentials`, nothing else). An
authenticated member asks `POST /events/ticket` for a random single-use
ticket, valid `RT_TICKET_TTL_SECONDS`, bound to (user, workspace, token
version) and stored hashed in Redis; the stream route redeems it with GETDEL,
so it works once. The ticket is never logged.

**One Redis reader per workspace per process** (RT-03). A stream does not hold
a Redis connection of its own: the hub runs one `XREAD BLOCK` loop per
workspace that has a subscriber in this process and fans each entry out to
that process's queues. Two hundred open streams of one workspace are one
blocking read.

**What a stream re-checks** at every keep-alive: the person's token version
(logout, password change), their membership in the workspace, the workspace
still being served, and whether they opened a stream on another workspace
since (`workspace_switched` - one active workspace per person, as a session
has). Any of those closes it with an event `closed` and its reason.

**Every member reads every conversation of their workspace** today (the route
table: MEMBER on the inbox). The stream delivers exactly the workspace's hints,
which are ids only; `may_read` is the seam where a narrower rule would go.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from redis.asyncio import Redis
from sqlalchemy import select

from app.core.events import CLOSED, RESYNC, stream_key
from app.core.logging import get_logger
from app.core.telemetry import record_events_stream_closed
from app.db.models.enums import MembershipStatus
from app.db.models.membership import Membership
from app.db.models.user import User
from app.db.session import Database
from app.services import realtime_streams
from app.services.workspace_serving import workspace_is_served

logger = get_logger(__name__)

_TICKET: Final = "wasla:rt:ticket:"
_ACTIVE_WORKSPACE: Final = "wasla:rt:workspace:"
#: The browser's reconnect delay (the SSE `retry:` field), in milliseconds.
RETRY_MILLISECONDS: Final = 3000
#: How long one XREAD blocks before the reader looks at its subscribers again.
_BLOCK_MILLISECONDS: Final = 5000


@dataclass(frozen=True, slots=True)
class Ticket:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    token_version: int


def _hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


async def issue_ticket(
    redis: Redis, *, user_id: uuid.UUID, tenant_id: uuid.UUID, token_version: int, ttl: int
) -> tuple[str, datetime]:
    """A single-use ticket for one stream, and when it expires."""
    token = secrets.token_urlsafe(32)
    value = json.dumps(
        {"user_id": str(user_id), "tenant_id": str(tenant_id), "token_version": token_version}
    )
    await redis.set(f"{_TICKET}{_hash(token)}", value, ex=ttl)
    return token, datetime.now(UTC) + timedelta(seconds=ttl)


async def redeem_ticket(redis: Redis, ticket: str) -> Ticket | None:
    """The ticket's binding, once: GETDEL, so a second use finds nothing."""
    if not ticket or len(ticket) > 200:
        return None
    raw = await redis.getdel(f"{_TICKET}{_hash(ticket)}")
    if raw is None:
        return None
    stored = json.loads(raw)
    return Ticket(
        user_id=uuid.UUID(stored["user_id"]),
        tenant_id=uuid.UUID(stored["tenant_id"]),
        token_version=int(stored["token_version"]),
    )


# ------------------------------------------------------------------ the hub


@dataclass(eq=False)  # one subscriber is one stream: identity, so it can live in a set
class _Subscriber:
    queue: asyncio.Queue[tuple[str, str, str] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=1000)
    )
    closing: str | None = None
    # Set when the queue was full and a hint was dropped: the stream says
    # `resync` so the client re-reads, rather than silently missing it.
    overflowed: bool = False


#: Every hub of this process, so a shutdown signal can close their streams.
_HUBS: Final[weakref.WeakSet[StreamHub]] = weakref.WeakSet()


class StreamHub:
    """This process's readers - one per workspace with a subscriber - and their queues."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._subscribers: dict[uuid.UUID, set[_Subscriber]] = {}
        self._readers: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        _HUBS.add(self)

    @property
    def open_streams(self) -> int:
        return sum(len(group) for group in self._subscribers.values())

    def subscribe(self, tenant_id: uuid.UUID, *, start: str) -> _Subscriber:
        """Join the workspace's reader; a new reader reads from `start`, never from `$`.

        `$` would mean "whatever arrives after my first XREAD", and a hint
        committed between this call and that read would reach nobody.
        """
        self._loop = asyncio.get_running_loop()
        subscriber = _Subscriber()
        self._subscribers.setdefault(tenant_id, set()).add(subscriber)
        if tenant_id not in self._readers or self._readers[tenant_id].done():
            self._readers[tenant_id] = asyncio.create_task(self._read(tenant_id, start))
        return subscriber

    def unsubscribe(self, tenant_id: uuid.UUID, subscriber: _Subscriber) -> None:
        group = self._subscribers.get(tenant_id)
        if group is not None:
            group.discard(subscriber)
            if not group:
                self._subscribers.pop(tenant_id, None)
                reader = self._readers.pop(tenant_id, None)
                if reader is not None:
                    reader.cancel()

    async def _read(self, tenant_id: uuid.UUID, start: str) -> None:
        key = stream_key(tenant_id)
        last = start
        while self._subscribers.get(tenant_id):
            try:
                found = await self._redis.xread({key: last}, block=_BLOCK_MILLISECONDS, count=200)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("events.reader_failed", extra={"event": "events.reader_failed"})
                await asyncio.sleep(1.0)
                continue
            for _stream, entries in found or ():
                for entry_id, raw in entries:
                    last = entry_id
                    fields = _fields(raw)
                    item = (_text(entry_id), fields.get("type", ""), fields.get("data", "{}"))
                    for subscriber in list(self._subscribers.get(tenant_id, ())):
                        try:
                            subscriber.queue.put_nowait(item)
                        except asyncio.QueueFull:
                            subscriber.overflowed = True

    def close_all(self, reason: str) -> None:
        """Every open stream of this process says `closed` and ends (graceful shutdown)."""
        for group in list(self._subscribers.values()):
            for subscriber in list(group):
                subscriber.closing = reason
                # The queue may be full: make room, the stream is ending anyway.
                while True:
                    try:
                        subscriber.queue.put_nowait(None)
                        break
                    except asyncio.QueueFull:
                        subscriber.queue.get_nowait()


def close_every_stream(reason: str = "server_restart") -> None:
    """Close every open stream of this process; safe to call from a signal handler.

    Uvicorn waits for open connections to finish before it runs the lifespan's
    shutdown, so a stream must be told to end when the exit signal arrives, not
    after (RT-06).
    """
    for hub in list(_HUBS):
        loop = hub._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(hub.close_all, reason)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _fields(raw: Any) -> dict[str, str]:
    return {_text(key): _text(value) for key, value in dict(raw or {}).items()}


# ------------------------------------------------------------------ frames


def frame(*, data: dict[str, Any], event: str | None = None, event_id: str | None = None) -> str:
    """One SSE event: `id:`, `event:`, `data:` lines and a blank line."""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


def may_read(_ticket: Ticket, _data: dict[str, Any]) -> bool:
    """Whether this person may read what the hint names: every member reads the inbox today."""
    return True


Clock = Callable[[], Awaitable[None]]


@dataclass
class StreamSession:
    """One open stream: what it was opened as, and how it is checked."""

    ticket: Ticket
    stream_id: str
    database: Database
    redis: Redis
    hub: StreamHub
    keepalive_seconds: float
    last_event_id: str | None = None

    async def closing_reason(self) -> str | None:
        """Why this stream must close now, or None (RT-05)."""
        active = await self.redis.get(f"{_ACTIVE_WORKSPACE}{self.ticket.user_id}")
        if active is not None and _text(active) != str(self.ticket.tenant_id):
            return "workspace_switched"
        async with self.database.session() as session:
            version = await session.scalar(
                select(User.token_version).where(User.id == self.ticket.user_id)
            )
            if version is None or int(version) != self.ticket.token_version:
                return "token_changed"
            member = await session.scalar(
                select(Membership.id).where(
                    Membership.user_id == self.ticket.user_id,
                    Membership.tenant_id == self.ticket.tenant_id,
                    Membership.status == MembershipStatus.ACTIVE,
                )
            )
            if member is None:
                return "membership_revoked"
            if not await workspace_is_served(session, self.ticket.tenant_id):
                return "workspace_unavailable"
        return None

    async def frames(self) -> AsyncIterator[str]:
        """The stream: retry, the catch-up since Last-Event-ID (or resync), then live hints."""
        await self.redis.set(
            f"{_ACTIVE_WORKSPACE}{self.ticket.user_id}",
            str(self.ticket.tenant_id),
            ex=int(self.keepalive_seconds * 4) + 3600,
        )
        # The newest hint before subscribing: everything after it reaches this
        # stream, either from the catch-up read below or from the reader.
        floor = self.last_event_id or await self._newest()
        subscriber = self.hub.subscribe(self.ticket.tenant_id, start=floor)
        loop = asyncio.get_running_loop()
        try:
            yield f"retry: {RETRY_MILLISECONDS}\n\n"
            async for text, last in self._catch_up(floor):
                floor = last
                yield text
            check_at = loop.time() + self.keepalive_seconds
            while True:
                idle = False
                try:
                    item = await asyncio.wait_for(
                        subscriber.queue.get(), timeout=max(0.0, check_at - loop.time())
                    )
                except TimeoutError:
                    item, idle = None, True
                if loop.time() >= check_at:
                    # On a schedule, not only when idle: a busy workspace's
                    # streams are re-checked as often as a quiet one's (RT-05).
                    check_at = loop.time() + self.keepalive_seconds
                    yield ": keep-alive\n\n"
                    await realtime_streams.refresh(
                        self.redis,
                        stream_id=self.stream_id,
                        user_id=self.ticket.user_id,
                        tenant_id=self.ticket.tenant_id,
                        keepalive_seconds=self.keepalive_seconds,
                    )
                    reason = await self.closing_reason()
                    if reason is not None:
                        yield await self._closed(reason)
                        return
                if idle:
                    continue
                if item is None:
                    yield await self._closed(subscriber.closing or "server_restart")
                    return
                if subscriber.overflowed:
                    subscriber.overflowed = False
                    yield frame(data={}, event=RESYNC)
                entry_id, kind, data = item
                if _before_or_at(entry_id, floor):
                    continue
                floor = entry_id
                payload = json.loads(data)
                if may_read(self.ticket, payload):
                    yield frame(data=payload, event=kind, event_id=entry_id)
        finally:
            self.hub.unsubscribe(self.ticket.tenant_id, subscriber)
            with contextlib.suppress(Exception):
                await realtime_streams.unregister(
                    self.redis,
                    stream_id=self.stream_id,
                    user_id=self.ticket.user_id,
                    tenant_id=self.ticket.tenant_id,
                )

    async def _newest(self) -> str:
        """The id of the workspace's newest hint, or `0-0` when it has none."""
        newest = await self.redis.xrevrange(stream_key(self.ticket.tenant_id), count=1)
        return _text(newest[0][0]) if newest else "0-0"

    async def _catch_up(self, floor: str) -> AsyncIterator[tuple[str, str]]:
        """Every hint after `floor`, each with its id (RT-01).

        For a reconnect, `floor` is the client's Last-Event-ID: when the stream
        no longer holds it the first event is `resync`, and the stream carries
        on from the newest hint. For a fresh open it is the newest hint seen
        before subscribing, and this reads what arrived since.
        """
        key = stream_key(self.ticket.tenant_id)
        if self.last_event_id is not None:
            oldest = await self.redis.xrange(key, count=1)
            if not oldest or _before(self.last_event_id, _text(oldest[0][0])):
                yield frame(data={}, event=RESYNC), await self._newest()
                return
        for entry_id, raw in await self.redis.xrange(key, min=f"({floor}"):
            identifier = _text(entry_id)
            fields = _fields(raw)
            payload = json.loads(fields.get("data", "{}"))
            if may_read(self.ticket, payload):
                yield (
                    frame(data=payload, event=fields.get("type"), event_id=identifier),
                    identifier,
                )

    async def _closed(self, reason: str) -> str:
        await record_events_stream_closed(reason)
        logger.info(
            "events.stream_closed", extra={"event": "events.stream_closed", "reason": reason}
        )
        return frame(data={"reason": reason}, event=CLOSED)


def _parts(entry_id: str) -> tuple[int, int]:
    milliseconds, _, sequence = entry_id.partition("-")
    return int(milliseconds), int(sequence or 0)


def _before(left: str, right: str) -> bool:
    try:
        return _parts(left) < _parts(right)
    except ValueError:
        return True


def _before_or_at(left: str, right: str) -> bool:
    try:
        return _parts(left) <= _parts(right)
    except ValueError:
        return False


def valid_event_id(value: str | None) -> str | None:
    """A Last-Event-ID that names a stream entry, or None."""
    if not value or len(value) > 40:
        return None
    try:
        _parts(value)
    except ValueError:
        return None
    return value


def new_stream_id() -> str:
    return uuid.uuid4().hex


__all__ = [
    "RETRY_MILLISECONDS",
    "StreamHub",
    "StreamSession",
    "Ticket",
    "close_every_stream",
    "frame",
    "issue_ticket",
    "may_read",
    "new_stream_id",
    "redeem_ticket",
    "valid_event_id",
]
