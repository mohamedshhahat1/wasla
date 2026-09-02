"""The Redis job queues, and what happens to a job that does not succeed.

A reliable queue rather than a plain list pop. Work is moved onto an in-flight
list as it is reserved, so a worker killed mid-job leaves the job recoverable
instead of silently dropping a customer's reply.

Releasing a job removes it from the in-flight list by exact value, which is why
encoding sorts its keys and uses compact separators: a payload re-serialised in
a different key order would never match, and the job would linger forever.

Four lists and a sorted set per queue
-------------------------------------

``pending`` is the queue. ``inflight`` holds what a worker has claimed.
``delayed`` is a sorted set scored by the moment a retry becomes due, promoted
back into ``pending`` at the head of every reserve. ``failed`` is the
dead-letter list, and since this module grew attempt counting it holds a
*record* rather than the bare payload — an operator reading it needs to know
how many times the job was tried and what stopped it, not just what it was.

The envelope
------------

A queued entry is now a `JobEnvelope`: the job's own payload plus how many
times it has been attempted, when it was first enqueued, and the category of
the last failure. The payload is carried as an opaque **string**, not as a
nested object, and that is deliberate. Every job type in this package encodes
with sorted keys and compact separators precisely so a re-serialisation
matches byte for byte; nesting the object here would make the envelope depend
on that invariant holding for every job type anybody adds later, and the cost
of it silently not holding is a job that can never be released. A string
cannot break that way.

`JobEnvelope.decode` accepts a bare payload as attempt 1, so a deployment that
rolls out while jobs are sitting in a queue does not strand them. That
tolerance is not a permanent shape; it is what makes the first deploy of this
change safe.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Self, cast

from redis.asyncio import Redis

from app.core.redis import MAX_BLOCKING_SECONDS
from app.workers.retry import FailureCategory

QUEUE_NAMESPACE: Final = "agent:jobs"
# The other two queues' namespaces live here rather than each in its own module,
# so that the closed set below can exist at all: `ingestion_queue` and
# `media_queue` import *from* this module, so this module cannot import them
# back. Each still re-exports its own, so every existing import keeps working.
INGESTION_NAMESPACE: Final = "knowledge:ingestion"
MEDIA_NAMESPACE: Final = "media:understanding"

# Every queue, keyed by the short name it reports under in metrics, in
# dead-letter records and in the operator command. Three fixed values, which is
# the whole domain of the `queue` label - declared once so the exposition and
# the operator command cannot come to disagree about what exists.
QUEUES: Final[dict[str, str]] = {
    "agent": QUEUE_NAMESPACE,
    "ingestion": INGESTION_NAMESPACE,
    "media": MEDIA_NAMESPACE,
}
# How long a reserve call waits before returning empty, so a worker loop can
# notice it has been asked to stop.
# From the Redis client, which sizes its read timeout around this. The two
# must be chosen together or a blocking reserve trips its own socket.
BLOCK_SECONDS: Final = MAX_BLOCKING_SECONDS

# How many due retries one reserve promotes. Bounded so a queue that filled up
# during an outage returns to work in batches rather than in one command that
# blocks Redis for everyone else.
PROMOTE_LIMIT: Final = 64

# How many dead-letter records are kept per queue. A cap rather than a TTL:
# the list is evidence for an operator, and evidence that expires while nobody
# is looking is the failure this whole list exists to prevent. Trimming from
# the *old* end keeps the newest, because an incident is diagnosed from what
# just happened. `wasla_queue_dead_letter_jobs` is what says the list is
# filling; the records are what says why.
DEAD_LETTER_LIMIT: Final = 1_000


async def _command[T](result: Awaitable[T] | T) -> T:
    """Await a redis-py command result.

    redis-py types every command as sync-or-async because one class backs both
    clients. On the async client the result is always awaitable, so this states
    that once here instead of at each of the call sites below.
    """
    return await cast("Awaitable[T]", result)


class MalformedJobError(Exception):
    """A queue entry that cannot be decoded.

    Separate from a processing failure: retrying it would fail identically
    forever, so the worker dead-letters it instead.
    """


def _isoformat(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse_moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class JobEnvelope:
    """One queued attempt, with everything needed to decide about the next.

    `attempt` is the number of the attempt *being made* — a freshly enqueued
    job is attempt 1, not attempt 0 — so "attempt 3 of 5" reads the way an
    operator says it.
    """

    body: str
    attempt: int
    enqueued_at: datetime
    first_attempted_at: datetime | None = None
    last_failure: FailureCategory | None = None

    @classmethod
    def wrap(cls, body: str, *, now: datetime | None = None) -> Self:
        return cls(body=body, attempt=1, enqueued_at=now or datetime.now(UTC))

    def encode(self) -> str:
        payload: dict[str, Any] = {
            "attempt": self.attempt,
            "body": self.body,
            "enqueued_at": _isoformat(self.enqueued_at),
        }
        if self.first_attempted_at is not None:
            payload["first_attempted_at"] = _isoformat(self.first_attempted_at)
        if self.last_failure is not None:
            payload["last_failure"] = str(self.last_failure)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @classmethod
    def decode(cls, raw: str) -> Self:
        """Read an envelope, or treat the whole entry as a first attempt.

        Never raises. An entry this cannot read is handed on as a body, and
        the job decoder is what refuses it — which keeps "unreadable" a single
        decision made in one place rather than two that could disagree.
        """
        try:
            payload = json.loads(raw)
        except ValueError:
            return cls.wrap(raw)
        if not isinstance(payload, dict):
            return cls.wrap(raw)
        body = payload.get("body")
        attempt = payload.get("attempt")
        if not isinstance(body, str) or not isinstance(attempt, int) or isinstance(attempt, bool):
            return cls.wrap(raw)

        enqueued_at = _parse_moment(payload.get("enqueued_at")) or datetime.now(UTC)
        failure = payload.get("last_failure")
        try:
            last_failure = FailureCategory(failure) if isinstance(failure, str) else None
        except ValueError:
            last_failure = None
        return cls(
            body=body,
            attempt=max(1, attempt),
            enqueued_at=enqueued_at,
            first_attempted_at=_parse_moment(payload.get("first_attempted_at")),
            last_failure=last_failure,
        )

    def next_attempt(self, *, category: FailureCategory, now: datetime) -> Self:
        """The envelope the retry is queued under."""
        return type(self)(
            body=self.body,
            attempt=self.attempt + 1,
            enqueued_at=self.enqueued_at,
            first_attempted_at=self.first_attempted_at or now,
            last_failure=category,
        )


@dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    """What is written down when a job stops being retried.

    **What this deliberately does not carry.** No exception text, no provider
    response, no message body, no customer identifier, no credential. The
    payloads on these queues are identifiers only by construction, and the
    reason a failure is a `FailureCategory` rather than a `repr` is that a
    dead-letter list outlives the incident and is read by whoever is on call,
    not by whoever wrote the code. Everything else is in the structured log
    line this record's `job_id` will find.

    `tenant_id` is carried because it is already the operational context of
    every log line in this system, and an operator whose dead-letter list has
    forty entries needs to know whether that is forty workspaces or one.
    """

    queue: str
    job_type: str
    tenant_id: str | None
    job_id: str | None
    attempts: int
    category: FailureCategory
    enqueued_at: datetime
    first_attempted_at: datetime | None
    last_attempted_at: datetime
    dead_lettered_at: datetime
    body: str

    def encode(self) -> str:
        payload: dict[str, Any] = {
            "attempts": self.attempts,
            "body": self.body,
            "category": str(self.category),
            "dead_lettered_at": _isoformat(self.dead_lettered_at),
            "enqueued_at": _isoformat(self.enqueued_at),
            "job_type": self.job_type,
            "last_attempted_at": _isoformat(self.last_attempted_at),
            "queue": self.queue,
        }
        if self.first_attempted_at is not None:
            payload["first_attempted_at"] = _isoformat(self.first_attempted_at)
        if self.tenant_id is not None:
            payload["tenant_id"] = self.tenant_id
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


class ReliableQueue:
    """The Redis mechanics every job queue in this package shares.

    One class rather than the three near-identical copies this file used to
    hold beside its neighbours. Retry scheduling, attempt counting and
    dead-lettering are the kind of logic that goes subtly wrong when it is
    written out three times, and the differences between the queues — the
    namespace, the payload, how urgent the work is — are all data.
    """

    def __init__(self, redis: Redis, *, namespace: str) -> None:
        self._redis = redis
        self._namespace = namespace
        self._pending = namespace + ":pending"
        self._inflight = namespace + ":inflight"
        self._delayed = namespace + ":delayed"
        self._failed = namespace + ":failed"

    @property
    def namespace(self) -> str:
        return self._namespace

    async def enqueue_body(self, body: str, *, now: datetime | None = None) -> None:
        await _command(self._redis.rpush(self._pending, JobEnvelope.wrap(body, now=now).encode()))

    async def reserve(
        self,
        *,
        wait_seconds: int = BLOCK_SECONDS,
        now: datetime | None = None,
    ) -> str | None:
        """Claim the oldest job, or return None if none arrives in time.

        Due retries are promoted first, so a worker on an otherwise quiet queue
        picks them up rather than blocking past them. The payload is returned
        rather than a decoded envelope because releasing it later requires the
        exact original bytes.
        """
        await self.promote_due(now=now)
        raw: Any = await _command(self._redis.blmove(self._pending, self._inflight, wait_seconds))
        return raw if isinstance(raw, str) else None

    async def promote_due(self, *, now: datetime | None = None) -> int:
        """Move retries whose moment has come back onto the pending list.

        The `zrem` is the claim: two workers promoting at the same instant both
        see the entry, and only the one whose removal returns 1 pushes it. The
        other has done nothing, which is the correct outcome — the alternative
        is the same job on the pending list twice.
        """
        moment = (now or datetime.now(UTC)).timestamp()
        due: Any = await _command(
            self._redis.zrangebyscore(self._delayed, "-inf", moment, start=0, num=PROMOTE_LIMIT)
        )
        promoted = 0
        for raw in due or ():
            if not isinstance(raw, str):
                continue
            if await _command(self._redis.zrem(self._delayed, raw)):
                await _command(self._redis.rpush(self._pending, raw))
                promoted += 1
        return promoted

    async def release(self, raw: str) -> None:
        """Mark a reserved job done."""
        await _command(self._redis.lrem(self._inflight, 1, raw))

    async def schedule_retry(
        self,
        raw: str,
        envelope: JobEnvelope,
        *,
        category: FailureCategory,
        delay_seconds: float,
        now: datetime | None = None,
    ) -> bool:
        """Put the job back for another attempt. Returns whether it was taken.

        The in-flight removal is the claim, exactly as in `dead_letter`: only
        the worker still holding the job may reschedule it, so a second call
        for the same reservation adds nothing.
        """
        removed = await _command(self._redis.lrem(self._inflight, 1, raw))
        if not removed:
            return False
        moment = now or datetime.now(UTC)
        follow_up = envelope.next_attempt(category=category, now=moment)
        due_at = moment.timestamp() + max(0.0, delay_seconds)
        await _command(self._redis.zadd(self._delayed, {follow_up.encode(): due_at}))
        return True

    async def dead_letter(self, raw: str, record: DeadLetterRecord) -> bool:
        """Record a terminal failure. Returns whether this call was the one.

        **This is the deduplication.** Removing the entry from the in-flight
        list is what proves the caller still holds the reservation, so calling
        this twice for one reservation writes one record: the second removal
        finds nothing and the second record is never pushed. Without that check
        a retry of the dead-letter path itself — a `dead_letter` that raised on
        the `rpush`, say — would double every entry an operator counts.
        """
        removed = await _command(self._redis.lrem(self._inflight, 1, raw))
        if not removed:
            return False
        await _command(self._redis.rpush(self._failed, record.encode()))
        # Newest kept. A negative-index trim is one command and needs no length
        # read, so it cannot race with a concurrent push the way read-then-trim
        # would.
        await _command(self._redis.ltrim(self._failed, -DEAD_LETTER_LIMIT, -1))
        return True

    async def depth(self) -> int:
        """How many jobs are waiting."""
        return int(await _command(self._redis.llen(self._pending)))

    async def inflight_depth(self) -> int:
        return int(await _command(self._redis.llen(self._inflight)))

    async def delayed_depth(self) -> int:
        return int(await _command(self._redis.zcard(self._delayed)))

    async def failed_depth(self) -> int:
        return int(await _command(self._redis.llen(self._failed)))

    async def oldest_pending_age_seconds(self, *, now: datetime | None = None) -> float | None:
        """How long the job at the head of the queue has been waiting.

        The head is the oldest by construction — this is a FIFO list — so one
        `LINDEX` answers it. None when the queue is empty, which is a different
        thing from zero and is rendered as an absent sample rather than a
        misleading floor.

        Measured from `enqueued_at`, which a retry deliberately carries forward
        from the original: what an operator needs to know is how long the
        customer has been waiting, not how long since the last attempt failed.
        """
        head: Any = await _command(self._redis.lindex(self._pending, 0))
        if not isinstance(head, str):
            return None
        envelope = JobEnvelope.decode(head)
        age = (now or datetime.now(UTC)) - envelope.enqueued_at
        return max(0.0, age.total_seconds())

    async def dead_letters(self, *, limit: int = 50) -> list[str]:
        """The most recent dead-letter records, newest first.

        For an operator and for the runbook. Returned as raw JSON strings
        rather than parsed objects because the caller printing them is the
        only consumer, and re-parsing to re-serialise would be theatre.
        """
        entries: Any = await _command(self._redis.lrange(self._failed, -limit, -1))
        return [entry for entry in reversed(entries or ()) if isinstance(entry, str)]


def _identifier(payload: dict[str, Any], key: str) -> uuid.UUID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise MalformedJobError(f"The job is missing {key}.")
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise MalformedJobError(f"The job has an unusable {key}.") from error


def _optional_identifier(payload: dict[str, Any], key: str) -> uuid.UUID | None:
    if payload.get(key) is None:
        return None
    return _identifier(payload, key)


def _decode_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise MalformedJobError("The job is not valid JSON.") from error
    if not isinstance(payload, dict):
        raise MalformedJobError("The job is not an object.")
    return payload


@dataclass(frozen=True, slots=True)
class AgentJob:
    """A request for an agent to look at one conversation.

    Carries identifiers only. The conversation's contents are read fresh when
    the job runs, because a queued job may wait behind others and the customer
    may have sent more in the meantime.
    """

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    agent_id: uuid.UUID | None = None

    def encode(self) -> str:
        payload: dict[str, str] = {
            "tenant_id": str(self.tenant_id),
            "conversation_id": str(self.conversation_id),
        }
        if self.agent_id is not None:
            payload["agent_id"] = str(self.agent_id)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @classmethod
    def decode(cls, raw: str) -> Self:
        payload = _decode_object(raw)
        return cls(
            tenant_id=_identifier(payload, "tenant_id"),
            conversation_id=_identifier(payload, "conversation_id"),
            agent_id=_optional_identifier(payload, "agent_id"),
        )


class AgentQueue(ReliableQueue):
    """Redis-backed queue of agent jobs.

    Takes a raw client rather than the wrapper, so a test can pass a fake and
    the queue stays independent of application startup.
    """

    def __init__(self, redis: Redis, *, namespace: str = QUEUE_NAMESPACE) -> None:
        super().__init__(redis, namespace=namespace)

    async def enqueue(self, job: AgentJob, *, now: datetime | None = None) -> None:
        await self.enqueue_body(job.encode(), now=now)


__all__ = [
    "BLOCK_SECONDS",
    "DEAD_LETTER_LIMIT",
    "INGESTION_NAMESPACE",
    "MEDIA_NAMESPACE",
    "PROMOTE_LIMIT",
    "QUEUES",
    "QUEUE_NAMESPACE",
    "AgentJob",
    "AgentQueue",
    "DeadLetterRecord",
    "JobEnvelope",
    "MalformedJobError",
    "ReliableQueue",
    "_command",
    "_decode_object",
    "_identifier",
]
