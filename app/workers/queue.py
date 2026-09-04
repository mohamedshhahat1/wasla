"""The Redis job queues, and what happens to a job that does not succeed.

A reliable queue rather than a plain list pop. Work is moved onto an in-flight
list as it is reserved, so a worker killed mid-job leaves the job recoverable
instead of silently dropping a customer's reply.

Releasing a job removes it from the in-flight list by exact value, which is why
encoding sorts its keys and uses compact separators: a payload re-serialised in
a different key order would never match, and the job would linger forever.

Four lists, a sorted set and a hash per queue
--------------------------------------------

``pending`` is the queue. ``inflight`` holds what a worker has claimed.
``delayed`` is a sorted set scored by the moment a retry becomes due, promoted
back into ``pending`` at the head of every reserve. ``failed`` is the
dead-letter list, and since this module grew attempt counting it holds a
*record* rather than the bare payload — an operator reading it needs to know
how many times the job was tried and what stopped it, not just what it was.

``reservations`` is the newest of them, and it is what makes a worker crash
survivable. An entry on the in-flight list used to be the whole of a
reservation, which meant a worker that died left its job there for ever: no
owner to ask about, no moment to measure staleness from, and nothing to say
how far the job had got. The hash records all three per in-flight payload —
who holds it, until when, and whether the job has reached the point where it
may have had an effect outside this process (ADR-074).

**The lease is renewed, not merely long.** A worker extends the leases it
holds on a timer, so a job that legitimately takes ten minutes is never stolen
from the process still working on it, and a job whose process died is
reclaimable within one timeout rather than one worst-case-job-duration. What
the renewal asserts is exactly what the heartbeat asserts: the process is up
and its event loop is scheduling.

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
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal, Self, cast

from redis.asyncio import Redis

from app.core.redis import MAX_BLOCKING_SECONDS
from app.core.tracing import QUEUE, SpanKind, carrier, sanitise_carrier, span
from app.workers.retry import RETRYABLE, FailureCategory, RetryPolicy

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

# How long a reservation is good for before a reaper may reclaim it.
#
# Two minutes, and the number matters less than the renewal beside it: a worker
# extends the leases it holds every `LEASE_RENEWAL_FRACTION` of this, so the
# timeout does not have to cover the longest job anybody might ever run - which
# is the trap a lease-without-renewal design falls into, where the only safe
# value is so large that a crash goes unnoticed for an hour.
DEFAULT_VISIBILITY_TIMEOUT_SECONDS: Final = 120.0

# How often a lease is refreshed, as a fraction of its life. A third, so two
# consecutive renewal failures are a blip and three are a death - the same
# shape, and for the same reason, as the worker heartbeat's TTL.
LEASE_RENEWAL_FRACTION: Final = 1 / 3

# How many in-flight entries one recovery pass examines. Bounded so a queue
# that accumulated reservations during an outage is worked through in batches
# rather than in one command that blocks Redis for everybody else.
RECOVERY_SCAN_LIMIT: Final = 128

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

    `trace` is W3C trace context and nothing else: at most `traceparent` and
    `tracestate`, sanitised on the way in and again on the way out. It is
    carried so that the worker's attempt is causally connected to the request
    that queued the job, and it is **never** used as identity. The job's
    identity is its payload, its retry budget is `attempt`, and its
    deduplication is a unique constraint in PostgreSQL - none of which consults
    this field. A missing, truncated or hostile carrier costs the attempt its
    place in a trace and nothing else.
    """

    body: str
    attempt: int
    enqueued_at: datetime
    first_attempted_at: datetime | None = None
    last_failure: FailureCategory | None = None
    trace: Mapping[str, str] | None = None

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
        if self.trace:
            # Sorted with the rest, because `release` removes an in-flight
            # entry by exact value: an envelope that re-serialised differently
            # would never match and the job would stay in flight for ever.
            # `json.dumps(sort_keys=True)` sorts nested keys too, and there are
            # only ever two of them.
            payload["trace"] = dict(self.trace)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def with_trace(self, context: Mapping[str, str]) -> Self:
        """The same envelope, carrying trace context."""
        return replace(self, trace=dict(context) or None)

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
        # Sanitised rather than trusted: this is the one field in the envelope
        # that did not come from a job encoder, and it is read back from a
        # store an older release also wrote to. Anything that is not a short
        # W3C value under one of the two W3C keys is dropped.
        trace = sanitise_carrier(payload.get("trace"))
        return cls(
            body=body,
            attempt=max(1, attempt),
            enqueued_at=enqueued_at,
            first_attempted_at=_parse_moment(payload.get("first_attempted_at")),
            last_failure=last_failure,
            trace=trace or None,
        )

    def next_attempt(self, *, category: FailureCategory, now: datetime) -> Self:
        """The envelope the retry is queued under.

        The trace context travels with it, so attempt three is still part of
        the story that began with the request that queued the job. Each attempt
        gets its own span; what is preserved is the thread between them, not
        the span.
        """
        return type(self)(
            body=self.body,
            attempt=self.attempt + 1,
            enqueued_at=self.enqueued_at,
            first_attempted_at=self.first_attempted_at or now,
            last_failure=category,
            trace=self.trace,
        )


class ReservationStage(StrEnum):
    """How far a reserved job had got when it was last heard from.

    The whole vocabulary, and it is deliberately three values rather than a
    progress percentage: what a recovery decision needs is not how much work
    was done but whether any of it can be seen from outside this process.
    """

    #: Claimed, and nothing has left the process. A crash here is invisible to
    #: everyone but us, so the job can simply be run again.
    RESERVED = "reserved"
    #: The job has begun talking to somebody else's API. A crash here may have
    #: sent a customer a message, taken an inference, or both.
    ENGAGED = "engaged"
    #: On the in-flight list with no reservation record - a crash in the
    #: moment between claiming the job and recording the claim, or an entry
    #: left by a release of this code that predates leases. Which of the two
    #: above it should be treated as is the *queue's* decision, because only
    #: the queue knows whether repeating its work is free.
    UNKNOWN = "unknown"


#: Named so the decoder below can fall back to it without repeating itself.
_UNKNOWN_STAGE: Final = ReservationStage.UNKNOWN


@dataclass(frozen=True, slots=True)
class Reservation:
    """Who holds an in-flight job, until when, and how far it had got."""

    worker: str
    reserved_at: datetime
    lease_until: datetime
    stage: ReservationStage = ReservationStage.RESERVED

    def encode(self) -> str:
        return json.dumps(
            {
                "lease_until": _isoformat(self.lease_until),
                "reserved_at": _isoformat(self.reserved_at),
                "stage": str(self.stage),
                "worker": self.worker,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def decode(cls, raw: str, *, now: datetime) -> Self:
        """Read a reservation, treating anything unreadable as long expired.

        Never raises. A reservation record this cannot parse is worse than
        useless - it would keep a job in-flight for ever while looking like a
        live claim - so it is read as an expired, stage-unknown reservation and
        recovered on that basis.
        """
        expired = cls(worker="", reserved_at=now, lease_until=now, stage=ReservationStage.UNKNOWN)
        try:
            payload = json.loads(raw)
        except ValueError:
            return expired
        if not isinstance(payload, dict):
            return expired
        lease_until = _parse_moment(payload.get("lease_until"))
        if lease_until is None:
            return expired
        raw_stage = payload.get("stage")
        try:
            stage = ReservationStage(raw_stage) if isinstance(raw_stage, str) else _UNKNOWN_STAGE
        except ValueError:
            stage = _UNKNOWN_STAGE
        worker = payload.get("worker")
        return cls(
            worker=worker if isinstance(worker, str) else "",
            reserved_at=_parse_moment(payload.get("reserved_at")) or lease_until,
            lease_until=lease_until,
            stage=stage,
        )

    def is_expired(self, *, now: datetime) -> bool:
        return self.lease_until <= now

    def renewed(self, *, until: datetime) -> Self:
        return type(self)(
            worker=self.worker,
            reserved_at=self.reserved_at,
            lease_until=until,
            stage=self.stage,
        )

    def engaged(self) -> Self:
        return type(self)(
            worker=self.worker,
            reserved_at=self.reserved_at,
            lease_until=self.lease_until,
            stage=ReservationStage.ENGAGED,
        )


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What a recovery pass did to one expired reservation."""

    raw: str
    envelope: JobEnvelope
    stage: ReservationStage
    #: `requeued` when the job went back for another attempt, `quarantined`
    #: when it was dead-lettered instead - either because its budget was spent
    #: or because repeating it might duplicate something a customer can see.
    action: Literal["requeued", "quarantined"]
    category: FailureCategory


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

    #: The short name this queue reports under in metrics, in dead-letter
    #: records and in the operator command. One value per queue, declared
    #: beside the namespace so the label a dashboard groups by and the label a
    #: recovery pass writes cannot drift apart - which they had, the recovery
    #: counter reporting `agent:jobs` while every depth gauge reported `agent`.
    label: str = "queue"

    #: Whether running one of this queue's jobs a second time is free.
    #: Overridden per queue, and it is the single fact recovery consults when
    #: it cannot tell how far a crashed job had got.
    idempotent: bool = False

    def __init__(
        self,
        redis: Redis,
        *,
        namespace: str,
        worker_id: str | None = None,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> None:
        self._redis = redis
        self._namespace = namespace
        self._pending = namespace + ":pending"
        self._inflight = namespace + ":inflight"
        self._delayed = namespace + ":delayed"
        self._failed = namespace + ":failed"
        self._reservations = namespace + ":reservations"
        # Identifies this process in a reservation record. Generated per queue
        # instance rather than shared, because what it is for is telling
        # *somebody else's* claim from ours in a log line - not for locking,
        # which the in-flight list already does.
        self._worker_id = worker_id if worker_id is not None else uuid.uuid4().hex
        self._visibility = visibility_timeout_seconds
        # What this process currently holds, so its leases can be renewed. In
        # memory deliberately: it is a property of this process's liveness, and
        # a process that dies should stop renewing.
        self._held: set[str] = set()

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def visibility_timeout_seconds(self) -> float:
        return self._visibility

    @property
    def held(self) -> frozenset[str]:
        """The reservations this process is responsible for renewing."""
        return frozenset(self._held)

    async def enqueue_body(self, body: str, *, now: datetime | None = None) -> None:
        """Put one job on the queue, carrying the trace it was queued from.

        The span is opened *around* the push so that the context injected into
        the envelope names this span - a worker's attempt is then a child of
        "the moment the job was queued", which is the causal link a reader
        follows backwards to the request. With tracing off, `carrier()` answers
        empty and the envelope carries no trace field at all.
        """
        with span(
            f"queue.publish {self.label}",
            kind=SpanKind.PRODUCER,
            attributes={QUEUE: self.label},
        ):
            envelope = JobEnvelope.wrap(body, now=now).with_trace(carrier())
            await _command(self._redis.rpush(self._pending, envelope.encode()))

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
        if not isinstance(raw, str):
            return None
        # Recorded immediately after the move rather than as part of it: Redis
        # cannot transform a value while moving it, and a blocking move cannot
        # be scripted. The gap is microseconds wide, and a crash inside it
        # leaves an in-flight entry with no reservation - which `adopt` exists
        # to pick up, so the gap costs one extra visibility timeout rather
        # than a stranded job.
        await self._record_reservation(raw, now=now)
        self._held.add(raw)
        return raw

    async def _record_reservation(self, raw: str, *, now: datetime | None = None) -> Reservation:
        moment = now or datetime.now(UTC)
        reservation = Reservation(
            worker=self._worker_id,
            reserved_at=moment,
            lease_until=moment + timedelta(seconds=self._visibility),
        )
        await _command(self._redis.hset(self._reservations, raw, reservation.encode()))
        return reservation

    async def reservation(self, raw: str, *, now: datetime | None = None) -> Reservation | None:
        """What is known about one in-flight job's claim."""
        stored: Any = await _command(self._redis.hget(self._reservations, raw))
        if not isinstance(stored, str):
            return None
        return Reservation.decode(stored, now=now or datetime.now(UTC))

    async def mark_engaged(self, raw: str, *, now: datetime | None = None) -> bool:
        """Record that this job has begun talking to somebody else's API.

        The single most important write in this module. `_TurnProgress` in the
        agent worker knows the same fact, and knows it only in memory - so a
        process that dies takes that knowledge with it and a reaper is left
        guessing whether a customer already has a reply. This puts the answer
        somewhere the crash cannot reach (ADR-074).

        Returns whether the reservation was still there to mark. False means a
        reaper has already reclaimed the job, which the caller cannot fix but
        which belongs in a log line.
        """
        moment = now or datetime.now(UTC)
        current = await self.reservation(raw, now=moment)
        if current is None:
            return False
        await _command(self._redis.hset(self._reservations, raw, current.engaged().encode()))
        return True

    async def renew_leases(self, *, now: datetime | None = None) -> int:
        """Extend every lease this process holds. Returns how many were extended.

        Only extends reservations that still exist: a job a reaper has already
        reclaimed must not be resurrected by the renewal of the worker that
        lost it, which is why this reads before it writes and drops what it
        cannot find.
        """
        moment = now or datetime.now(UTC)
        until = moment + timedelta(seconds=self._visibility)
        renewed = 0
        for raw in list(self._held):
            current = await self.reservation(raw, now=moment)
            if current is None:
                # Reclaimed by a reaper, or acknowledged by us already.
                self._held.discard(raw)
                continue
            await _command(
                self._redis.hset(self._reservations, raw, current.renewed(until=until).encode())
            )
            renewed += 1
        return renewed

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

    async def _claim_inflight(self, raw: str) -> bool:
        """Take exclusive responsibility for one in-flight entry.

        The whole of this module's mutual exclusion, in one line. `LREM`
        returns how many it removed, so exactly one caller can ever get a 1 for
        a given entry - whether that caller is the worker acknowledging its own
        job or a reaper reclaiming a dead one. Everything that finishes a job
        goes through here.
        """
        removed = await _command(self._redis.lrem(self._inflight, 1, raw))
        self._held.discard(raw)
        return bool(removed)

    async def _forget_reservation(self, raw: str) -> None:
        await _command(self._redis.hdel(self._reservations, raw))

    async def release(self, raw: str) -> bool:
        """Mark a reserved job done. Returns whether this call was the one.

        The answer matters to a worker whose *last* act depends on having
        finished the job rather than on having tried: the media worker asks an
        agent to reply only if this call removed the entry, so a worker whose
        lease a reaper had already reclaimed cannot release a conversation the
        requeued attempt is about to release again (ADR-092). Callers with no
        such act ignore the result, exactly as they did when this returned
        nothing.
        """
        claimed = await self._claim_inflight(raw)
        await self._forget_reservation(raw)
        return claimed

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
        if not await self._claim_inflight(raw):
            return False
        await self._forget_reservation(raw)
        await self._schedule(envelope, category=category, delay_seconds=delay_seconds, now=now)
        return True

    async def _schedule(
        self,
        envelope: JobEnvelope,
        *,
        category: FailureCategory,
        delay_seconds: float,
        now: datetime | None = None,
    ) -> None:
        moment = now or datetime.now(UTC)
        follow_up = envelope.next_attempt(category=category, now=moment)
        due_at = moment.timestamp() + max(0.0, delay_seconds)
        await _command(self._redis.zadd(self._delayed, {follow_up.encode(): due_at}))

    async def dead_letter(self, raw: str, record: DeadLetterRecord) -> bool:
        """Record a terminal failure. Returns whether this call was the one.

        **This is the deduplication.** Removing the entry from the in-flight
        list is what proves the caller still holds the reservation, so calling
        this twice for one reservation writes one record: the second removal
        finds nothing and the second record is never pushed. Without that check
        a retry of the dead-letter path itself — a `dead_letter` that raised on
        the `rpush`, say — would double every entry an operator counts.
        """
        if not await self._claim_inflight(raw):
            return False
        await self._forget_reservation(raw)
        await self._record_dead_letter(record)
        return True

    async def _record_dead_letter(self, record: DeadLetterRecord) -> None:
        await _command(self._redis.rpush(self._failed, record.encode()))
        # Newest kept. A negative-index trim is one command and needs no length
        # read, so it cannot race with a concurrent push the way read-then-trim
        # would.
        await _command(self._redis.ltrim(self._failed, -DEAD_LETTER_LIMIT, -1))

    # ------------------------------------------------------------- recovery

    async def expired(
        self,
        *,
        now: datetime | None = None,
        limit: int = RECOVERY_SCAN_LIMIT,
    ) -> list[tuple[str, Reservation]]:
        """In-flight entries whose lease has run out.

        Enumerated from the **in-flight list**, not from the reservation hash,
        and that direction is load-bearing. The list is what `_claim_inflight`
        can atomically take, so it is the only thing that can be recovered; a
        reservation record with no matching entry is debris from a worker that
        renewed a lease a reaper had already reclaimed, and reading in this
        direction means nothing ever looks at it.

        An entry with no reservation at all is *adopted* rather than recovered
        on the spot: it is given a lease starting now, so the next pass handles
        it by the same rule as everything else. A worker that is mid-reserve
        overwrites that adoption microseconds later with its own, so adopting
        cannot steal work that is genuinely starting.
        """
        moment = now or datetime.now(UTC)
        entries: Any = await _command(self._redis.lrange(self._inflight, 0, limit - 1))
        stale: list[tuple[str, Reservation]] = []
        for raw in entries or ():
            if not isinstance(raw, str):
                continue
            held = await self.reservation(raw, now=moment)
            if held is None:
                await self._adopt(raw, now=moment)
                continue
            if held.is_expired(now=moment):
                stale.append((raw, held))
        return stale

    async def _adopt(self, raw: str, *, now: datetime) -> None:
        """Give an unleased in-flight entry a lease, so the next pass can judge it."""
        reservation = Reservation(
            worker="",
            reserved_at=now,
            lease_until=now + timedelta(seconds=self._visibility),
            stage=ReservationStage.UNKNOWN,
        )
        await _command(self._redis.hset(self._reservations, raw, reservation.encode()))

    def identify(self, body: str) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """Who this job was for, as (tenant, subject), for a dead-letter record.

        Overridden per queue, because only the queue knows what its payload
        means. The base answers "no idea", which is the right answer for a body
        that will not decode.

        Never raises. It is called while writing down why something failed, and
        a record that failed to be written because the failure was unusual is
        the worst possible outcome.
        """
        return None, None

    def _is_safe_to_repeat(self, stage: ReservationStage) -> bool:
        """Whether running this job again cannot duplicate anything visible.

        Three inputs, one answer. An idempotent queue is safe whatever stage it
        reached - re-ingesting a document replaces its chunks, and a file
        already read is not read again - so the stage does not matter there. On
        a queue that is not idempotent, only `RESERVED` is safe: it means
        nothing has left the process, so nobody outside can have seen anything.
        `UNKNOWN` is treated as unsafe there, because a reservation whose stage
        was lost is exactly the case where guessing costs a customer a second
        message.
        """
        if self.idempotent:
            return True
        return stage is ReservationStage.RESERVED

    async def recover_expired(
        self,
        *,
        policy: RetryPolicy,
        now: datetime | None = None,
        jitter: float = 0.0,
        limit: int = RECOVERY_SCAN_LIMIT,
    ) -> list[RecoveryOutcome]:
        """Reclaim jobs whose worker stopped answering for them.

        Each expired reservation gets exactly one outcome, because
        `_claim_inflight` can only succeed once: two reapers looking at the
        same entry produce one requeue or one dead-letter between them, and the
        loser simply moves on.

        **A crash spends an attempt.** The job goes back as `next_attempt`,
        carrying its history, so a job that crashes a worker every time still
        runs out of budget instead of looping for ever. A job already on its
        last attempt is dead-lettered rather than given a hidden extra one.
        """
        moment = now or datetime.now(UTC)
        outcomes: list[RecoveryOutcome] = []

        for raw, reservation in await self.expired(now=moment, limit=limit):
            envelope = JobEnvelope.decode(raw)
            safe = self._is_safe_to_repeat(reservation.stage)
            category = (
                FailureCategory.WORKER_CRASHED if safe else FailureCategory.UNCERTAIN_DELIVERY
            )
            # The last failure is consulted as well as the crash itself: a job
            # whose previous attempt ended in something terminal has no
            # business being resurrected because a later worker happened to die
            # holding it.
            previously_terminal = (
                envelope.last_failure is not None and envelope.last_failure not in RETRYABLE
            )
            requeue = (
                safe
                and not previously_terminal
                and policy.should_retry(category, attempt=envelope.attempt)
            )

            if not await self._claim_inflight(raw):
                # Another reaper got there first. Nothing to undo: it has not
                # been requeued or recorded by us, and it will be by them.
                continue
            await self._forget_reservation(raw)

            if requeue:
                await self._schedule(
                    envelope,
                    category=category,
                    delay_seconds=policy.delay_for(envelope.attempt, jitter=jitter),
                    now=moment,
                )
                action: Literal["requeued", "quarantined"] = "requeued"
            else:
                # Identified here rather than left in the opaque body, because
                # the runbook step for a quarantined turn is "open the
                # conversation" - and an operator should not have to parse a
                # nested JSON string to find out which one.
                tenant, subject = self.identify(envelope.body)
                await self._record_dead_letter(
                    DeadLetterRecord(
                        queue=self._namespace,
                        job_type=self.label,
                        tenant_id=str(tenant) if tenant else None,
                        job_id=str(subject) if subject else None,
                        attempts=envelope.attempt,
                        category=category,
                        enqueued_at=envelope.enqueued_at,
                        first_attempted_at=envelope.first_attempted_at,
                        last_attempted_at=reservation.reserved_at,
                        dead_lettered_at=moment,
                        body=envelope.body,
                    )
                )
                action = "quarantined"

            outcomes.append(
                RecoveryOutcome(
                    raw=raw,
                    envelope=envelope,
                    stage=reservation.stage,
                    action=action,
                    category=category,
                )
            )
        return outcomes

    async def expired_depth(self, *, now: datetime | None = None) -> int:
        """How many in-flight entries are past their lease, without reclaiming any.

        For the exposition. Read-only on purpose: a scrape must not change what
        it is measuring, so this does not adopt unleased entries the way
        `expired` does - it counts them, because an entry nobody has leased is
        every bit as stuck as one whose lease ran out.
        """
        moment = now or datetime.now(UTC)
        entries: Any = await _command(self._redis.lrange(self._inflight, 0, -1))
        count = 0
        for raw in entries or ():
            if not isinstance(raw, str):
                continue
            held = await self.reservation(raw, now=moment)
            if held is None or held.is_expired(now=moment):
                count += 1
        return count

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

    label = "agent"

    #: An agent turn ends in a WhatsApp message that carries no idempotency
    #: key, so repeating one a customer may already have received is not free.
    #: This single flag is what makes crash recovery refuse to guess here.
    idempotent = False

    def __init__(
        self,
        redis: Redis,
        *,
        namespace: str = QUEUE_NAMESPACE,
        worker_id: str | None = None,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            redis,
            namespace=namespace,
            worker_id=worker_id,
            visibility_timeout_seconds=visibility_timeout_seconds,
        )

    async def enqueue(self, job: AgentJob, *, now: datetime | None = None) -> None:
        await self.enqueue_body(job.encode(), now=now)

    def identify(self, body: str) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        try:
            job = AgentJob.decode(body)
        except MalformedJobError:
            return None, None
        return job.tenant_id, job.conversation_id


__all__ = [
    "BLOCK_SECONDS",
    "DEAD_LETTER_LIMIT",
    "DEFAULT_VISIBILITY_TIMEOUT_SECONDS",
    "INGESTION_NAMESPACE",
    "LEASE_RENEWAL_FRACTION",
    "MEDIA_NAMESPACE",
    "PROMOTE_LIMIT",
    "QUEUES",
    "QUEUE_NAMESPACE",
    "RECOVERY_SCAN_LIMIT",
    "AgentJob",
    "AgentQueue",
    "DeadLetterRecord",
    "JobEnvelope",
    "MalformedJobError",
    "RecoveryOutcome",
    "ReliableQueue",
    "Reservation",
    "ReservationStage",
    "_command",
    "_decode_object",
    "_identifier",
]
