"""Realtime hints for the inbox and the campaigns page (RT-02, RT-03, ADR-155).

**Ids and hints, never content.** An event says that something changed - a
message arrived, a status moved, a conversation was taken over, a follow-up or
a campaign changed - and carries ids only: no text, no name, no phone number,
no media id, no template value. A client fetches the content through the REST
routes, which apply the authorization they always did. A stream event is
never evidence that its content is readable by whoever received it.

**Published after the commit that caused it.** The ORM's own flush events
collect hints on the session (`session.info`), for every path that writes the
rows - the inbound webhook, the send path, the status webhook, the takeover,
the follow-up and campaign workers - without each of them remembering to.
`after_commit` hands them to Redis; a rollback drops them, so a transaction
that did not happen publishes nothing. A publish that fails (Redis down) never
fails the business transaction - it already committed - and is counted
(`wasla_events_publish_failed_total`); clients converge through `resync` and
the incremental reads.

**One Redis stream per workspace**, `wasla:rt:events:{tenant_id}`, trimmed to
`RT_STREAM_MAXLEN` entries (approximate) and to `RT_STREAM_RETENTION_SECONDS`,
and expiring when idle that long.

A change written with a bulk `UPDATE` statement fires no ORM event and so
publishes no hint; clients holding a stream re-read on `resync` and poll with
`updated_since` as a safety net (RT-04, RT-07).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from redis.asyncio import Redis
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.base import NO_VALUE

from app.core.logging import get_logger

logger = get_logger(__name__)

#: The event types a stream carries (RT-02). Closed: a metric label domain.
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "message.created",
        "message.status",
        "conversation.updated",
        "follow_up.updated",
        "campaign.changed",
    }
)
#: Sent by the stream itself, never published.
RESYNC: Final = "resync"
CLOSED: Final = "closed"

#: The key prefix of a workspace's stream.
STREAM_PREFIX: Final = "wasla:rt:events:"

_PENDING: Final = "wasla_rt_events"


def stream_key(tenant_id: uuid.UUID | str) -> str:
    return f"{STREAM_PREFIX}{tenant_id}"


@dataclass(frozen=True, slots=True)
class RealtimeEvent:
    """One hint for one workspace."""

    tenant_id: uuid.UUID
    type: str
    data: dict[str, Any]


@dataclass
class _Sink:
    redis: Redis | None = None
    maxlen: int = 5000
    retention_seconds: int = 900
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


_sink = _Sink()


def set_event_sink(
    redis: Redis | None, *, maxlen: int = 5000, retention_seconds: int = 900
) -> None:
    """Point this process's published hints at its Redis client (None: publish nothing)."""
    _sink.redis = redis
    _sink.maxlen = maxlen
    _sink.retention_seconds = retention_seconds


def event_sink() -> Redis | None:
    return _sink.redis


async def drain() -> None:
    """Wait for this process's publishes in flight (tests; graceful shutdown)."""
    if _sink.tasks:
        await asyncio.gather(*list(_sink.tasks), return_exceptions=True)


def stage(session: Session, hint: RealtimeEvent) -> None:
    """Hold a hint on the session until its transaction commits."""
    session.info.setdefault(_PENDING, []).append(hint)


def _stage_for(target: Any, kind: str, data: dict[str, Any]) -> None:
    session = object_session(target)
    tenant_id = _loaded(target, "tenant_id")
    if session is None or tenant_id is None:
        return
    stage(session, RealtimeEvent(tenant_id=tenant_id, type=kind, data=data))


def _iso(moment: Any) -> str | None:
    return moment.isoformat() if isinstance(moment, datetime) else None


def _loaded(target: Any, name: str) -> Any:
    """An attribute's value if it is loaded - never a refresh inside a flush."""
    value = inspect(target).attrs[name].loaded_value
    return None if value is NO_VALUE else value


def _changed(target: Any, *names: str) -> list[str]:
    state = inspect(target)
    return [name for name in names if state.attrs[name].history.has_changes()]


# ------------------------------------------------------------------ the collectors


def _channel_of(connection: Any, target: Any) -> str | None:
    """The message's channel: its conversation's, from the session if loaded, else one read
    on the flush's own connection."""
    from app.db.models.conversation import Conversation

    conversation_id = _loaded(target, "conversation_id")
    session = object_session(target)
    if session is not None:
        for held in session.identity_map.values():
            if isinstance(held, Conversation) and _loaded(held, "id") == conversation_id:
                channel = _loaded(held, "channel")
                if channel is not None:
                    return str(getattr(channel, "value", channel))
    channel = connection.execute(
        select(Conversation.channel).where(Conversation.id == conversation_id)
    ).scalar()
    return None if channel is None else str(getattr(channel, "value", channel))


def _message_created(_mapper: Any, connection: Any, target: Any) -> None:
    direction = _loaded(target, "direction")
    _stage_for(
        target,
        "message.created",
        {
            "conversation_id": str(_loaded(target, "conversation_id")),
            "message_id": str(_loaded(target, "id")),
            "direction": getattr(direction, "value", direction),
            "channel_type": _channel_of(connection, target),
            "created_at": _iso(_loaded(target, "created_at")),
        },
    )


def _message_updated(_mapper: Any, _connection: Any, target: Any) -> None:
    if _changed(target, "status"):
        status = _loaded(target, "status")
        _stage_for(
            target,
            "message.status",
            {
                "conversation_id": str(_loaded(target, "conversation_id")),
                "message_id": str(_loaded(target, "id")),
                "status": getattr(status, "value", status),
            },
        )


#: Which conversation columns a client cares about, under the name it shows.
_CONVERSATION_FIELDS: Final[dict[str, str]] = {
    "status": "status",
    "mode": "owner",
    "assigned_to_id": "assignee",
    "priority": "priority",
}


def _conversation_updated(_mapper: Any, _connection: Any, target: Any) -> None:
    fields = [_CONVERSATION_FIELDS[name] for name in _changed(target, *_CONVERSATION_FIELDS)]
    if fields:
        _stage_for(
            target,
            "conversation.updated",
            {"conversation_id": str(_loaded(target, "id")), "fields": sorted(fields)},
        )


def _follow_up_changed(_mapper: Any, _connection: Any, target: Any) -> None:
    status = _loaded(target, "status")
    _stage_for(
        target,
        "follow_up.updated",
        {
            "conversation_id": str(_loaded(target, "conversation_id")),
            "state": getattr(status, "value", status),
        },
    )


def _follow_up_updated(mapper: Any, connection: Any, target: Any) -> None:
    if _changed(target, "status", "scheduled_at"):
        _follow_up_changed(mapper, connection, target)


def _campaign_updated(_mapper: Any, _connection: Any, target: Any) -> None:
    # Per transition, never per recipient (RT-03).
    if _changed(target, "status", "waiting_reason"):
        _stage_for(target, "campaign.changed", {"campaign_id": str(_loaded(target, "id"))})


# ------------------------------------------------------------------ publishing


def _after_commit(session: Session) -> None:
    hints: list[RealtimeEvent] = session.info.pop(_PENDING, [])
    if not hints or _sink.redis is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - a synchronous session outside the app
        return
    task = loop.create_task(_publish(_sink.redis, hints))
    _sink.tasks.add(task)
    task.add_done_callback(_sink.tasks.discard)


def _after_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)


async def _publish(redis: Redis, hints: list[RealtimeEvent]) -> None:
    from app.core.telemetry import record_events_publish_failed, record_events_published

    for hint in hints:
        key = stream_key(hint.tenant_id)
        try:
            await redis.xadd(
                key,
                {"type": hint.type, "data": json.dumps(hint.data, separators=(",", ":"))},
                maxlen=_sink.maxlen,
                approximate=True,
            )
            oldest = int((time.time() - _sink.retention_seconds) * 1000)
            await redis.xtrim(key, minid=f"{oldest}-0", approximate=True)
            await redis.expire(key, _sink.retention_seconds)
        except Exception:
            logger.warning(
                "events.publish_failed",
                extra={"event": "events.publish_failed", "type": hint.type},
            )
            await record_events_publish_failed()
            continue
        await record_events_published(hint.type)


def install() -> None:
    """Register the collectors and the commit hooks once per process."""
    if getattr(install, "_done", False):
        return
    from app.db.models.campaign import Campaign
    from app.db.models.conversation import Conversation, Message
    from app.db.models.follow_up import FollowUp

    event.listen(Message, "after_insert", _message_created)
    event.listen(Message, "after_update", _message_updated)
    event.listen(Conversation, "after_update", _conversation_updated)
    event.listen(FollowUp, "after_insert", _follow_up_changed)
    event.listen(FollowUp, "after_update", _follow_up_updated)
    event.listen(Campaign, "after_update", _campaign_updated)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    install._done = True  # type: ignore[attr-defined]


__all__ = [
    "CLOSED",
    "EVENT_TYPES",
    "RESYNC",
    "STREAM_PREFIX",
    "RealtimeEvent",
    "drain",
    "event_sink",
    "install",
    "set_event_sink",
    "stage",
    "stream_key",
]
