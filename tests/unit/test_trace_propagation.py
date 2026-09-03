"""One trace from the request that queued a job to the worker that ran it.

This is the property P2-C exists for. A customer's message arrives at the
webhook, is stored, and is answered minutes later by a different process
reading a Redis list; `request_id` correlates the first leg and stops at the
queue. These tests hold the thread across it.

Three things are being asserted, and the third is the one that matters most:

1. Enqueueing produces a `PRODUCER` span and puts W3C trace context in the
   envelope.
2. A worker's attempt is a `CONSUMER` span in the *same trace*, parented to the
   publish.
3. **None of it is load-bearing.** A missing carrier, a truncated one, a
   hostile one, or one written by a release that had never heard of tracing all
   produce a job that runs normally under a new trace. Tracing describes the
   system; it is never a participant in it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from app.core.tracing import (
    JOB_ATTEMPT,
    QUEUE,
    TRACEPARENT,
    TRACESTATE,
    SpanKind,
    carrier,
    context_from,
    sanitise_carrier,
    span,
)
from app.workers.dispatch import job_span
from app.workers.queue import AgentJob, AgentQueue, JobEnvelope, ReliableQueue
from app.workers.retry import FailureCategory
from tests.fake_queue_redis import FakeQueueRedis
from tests.fakes import as_redis
from tests.tracing_recorder import Recording, recording_spans

TENANT = "11111111-1111-1111-1111-111111111111"
CONVERSATION = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def record() -> Iterator[Recording]:
    yield from recording_spans()


@pytest.fixture
def redis() -> FakeQueueRedis:
    return FakeQueueRedis()


@pytest.fixture
def queue(redis: FakeQueueRedis) -> AgentQueue:
    return AgentQueue(as_redis(redis))


def _job() -> AgentJob:
    return AgentJob(
        tenant_id=uuid.UUID(TENANT),
        conversation_id=uuid.UUID(CONVERSATION),
    )


def _queued(redis: FakeQueueRedis, queue: ReliableQueue) -> JobEnvelope:
    entries = redis.lists[queue.namespace + ":pending"]
    assert len(entries) == 1
    return JobEnvelope.decode(entries[0])


# --------------------------------------------------------------- publishing


async def test_enqueueing_produces_a_publish_span(record: Recording, queue: AgentQueue) -> None:
    await queue.enqueue(_job())

    published = record.named("queue.publish agent")
    assert published.kind is SpanKind.PRODUCER
    assert published.attributes is not None
    assert published.attributes[QUEUE] == "agent"


async def test_the_envelope_carries_the_trace_it_was_queued_from(
    record: Recording,
    redis: FakeQueueRedis,
    queue: AgentQueue,
) -> None:
    with span("api.request"):
        await queue.enqueue(_job())

    envelope = _queued(redis, queue)
    assert envelope.trace is not None
    assert TRACEPARENT in envelope.trace


async def test_the_carrier_names_the_publish_span(
    record: Recording,
    redis: FakeQueueRedis,
    queue: AgentQueue,
) -> None:
    """So the worker's attempt hangs from "the moment the job was queued"."""
    await queue.enqueue(_job())

    published = record.named("queue.publish agent")
    envelope = _queued(redis, queue)
    assert envelope.trace is not None
    assert published.context is not None
    assert format(published.context.span_id, "016x") in envelope.trace[TRACEPARENT]


async def test_only_the_two_w3c_keys_ever_reach_the_envelope(
    record: Recording,
    redis: FakeQueueRedis,
    queue: AgentQueue,
) -> None:
    """Not a header bag, not baggage, not an application field."""
    await queue.enqueue(_job())

    stored = json.loads(redis.lists[queue.namespace + ":pending"][0])
    assert set(stored["trace"]) <= {TRACEPARENT, TRACESTATE}


async def test_tracing_off_puts_no_trace_field_in_the_envelope(
    redis: FakeQueueRedis,
    queue: AgentQueue,
) -> None:
    """The default. An envelope is byte-identical to what it always was."""
    await queue.enqueue(_job())

    stored = json.loads(redis.lists[queue.namespace + ":pending"][0])
    assert "trace" not in stored


# ------------------------------------------------------------------ the link


async def test_the_worker_attempt_joins_the_publisher_trace(
    record: Recording,
    redis: FakeQueueRedis,
    queue: AgentQueue,
) -> None:
    """The whole point: one trace across two processes and a Redis list."""
    with span("api.request"):
        await queue.enqueue(_job())
    envelope = _queued(redis, queue)

    with job_span(job_type="agent", envelope=envelope):
        pass

    request = record.named("api.request")
    published = record.named("queue.publish agent")
    attempt = record.named("worker.agent")
    assert request.context is not None
    assert attempt.context is not None
    assert attempt.context.trace_id == request.context.trace_id
    assert published.context is not None
    assert attempt.parent is not None
    assert attempt.parent.span_id == published.context.span_id


async def test_the_attempt_span_says_which_queue_and_which_attempt(
    record: Recording,
    redis: FakeQueueRedis,
    queue: AgentQueue,
) -> None:
    await queue.enqueue(_job())
    envelope = _queued(redis, queue)

    with job_span(job_type="agent", envelope=envelope):
        pass

    attempt = record.named("worker.agent")
    assert attempt.kind is SpanKind.CONSUMER
    assert attempt.attributes is not None
    assert attempt.attributes[QUEUE] == "agent"
    assert attempt.attributes[JOB_ATTEMPT] == 1


# --------------------------------------------------------------- retries


async def test_each_attempt_is_its_own_span_in_the_same_trace(
    record: Recording,
    redis: FakeQueueRedis,
    queue: AgentQueue,
) -> None:
    """Three attempts, three spans, one story.

    Reusing a span across attempts would overwrite the history of the first
    two; starting a new trace per attempt would lose the connection to the
    request that queued the work. Neither is what an operator asking "why did
    this customer wait four minutes" needs.
    """
    with span("api.request"):
        await queue.enqueue(_job())
    envelope = _queued(redis, queue)

    now = datetime.now(UTC)
    for _ in range(3):
        with job_span(job_type="agent", envelope=envelope):
            pass
        envelope = envelope.next_attempt(category=FailureCategory.DEPENDENCY_UNAVAILABLE, now=now)

    attempts = record.all_named("worker.agent")
    assert len(attempts) == 3
    assert [item.attributes[JOB_ATTEMPT] for item in attempts if item.attributes] == [1, 2, 3]
    assert len({item.context.span_id for item in attempts if item.context}) == 3
    assert len({item.context.trace_id for item in attempts if item.context}) == 1


async def test_a_retry_keeps_the_carrier(record: Recording) -> None:
    """The envelope a retry is queued under still names the original trace."""
    with span("api.request"):
        original = JobEnvelope.wrap("payload").with_trace(carrier())

    retried = original.next_attempt(
        category=FailureCategory.DEPENDENCY_UNAVAILABLE,
        now=datetime.now(UTC),
    )

    assert retried.trace == original.trace


async def test_a_retry_survives_the_round_trip_through_redis(record: Recording) -> None:
    """Encoded and decoded, because that is how a delayed retry travels."""
    with span("api.request"):
        original = JobEnvelope.wrap("payload").with_trace(carrier())

    restored = JobEnvelope.decode(original.encode())

    assert restored.trace == original.trace


# ------------------------------------------------- tracing is never required


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"tracestate": "vendor=1"},
        {"traceparent": ""},
        {"traceparent": "not-a-traceparent"},
        {"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01"},
        {"traceparent": "x" * 200},
        {"traceparent": 12345},
        "a string, not a mapping",
        ["traceparent", "value"],
    ],
)
async def test_a_carrier_that_makes_no_sense_still_runs_the_job(
    record: Recording,
    raw: object,
) -> None:
    """Every shape a broken carrier can take, and none of them refuses work."""
    envelope = JobEnvelope(
        body="payload",
        attempt=1,
        enqueued_at=datetime.now(UTC),
        trace=sanitise_carrier(raw) or None,
    )

    ran = False
    with job_span(job_type="agent", envelope=envelope):
        ran = True

    assert ran
    attempt = record.named("worker.agent")
    assert attempt.parent is None, "a broken carrier must start a new trace, not join one"


async def test_an_oversized_tracestate_is_dropped_and_the_traceparent_kept() -> None:
    """A hostile entry must not make every envelope large."""
    valid = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"

    cleaned = sanitise_carrier({TRACEPARENT: valid, TRACESTATE: "x" * 5_000})

    assert cleaned == {TRACEPARENT: valid}


async def test_an_oversized_traceparent_is_no_carrier_at_all() -> None:
    cleaned = sanitise_carrier({TRACEPARENT: "0" * 500, TRACESTATE: "vendor=1"})

    assert cleaned == {}


async def test_an_envelope_from_a_release_without_tracing_decodes(record: Recording) -> None:
    """Forward compatibility in the direction that actually happens."""
    legacy = json.dumps(
        {"attempt": 2, "body": "payload", "enqueued_at": "2026-09-02T12:00:00+00:00"},
        separators=(",", ":"),
        sort_keys=True,
    )

    envelope = JobEnvelope.decode(legacy)

    assert envelope.trace is None
    assert envelope.attempt == 2
    assert context_from(envelope.trace) is None


async def test_a_bare_payload_still_decodes_as_a_first_attempt() -> None:
    """The other legacy shape the queue has always accepted."""
    envelope = JobEnvelope.decode("just-a-body")

    assert envelope.body == "just-a-body"
    assert envelope.attempt == 1
    assert envelope.trace is None


# --------------------------------------------------- the release invariant


async def test_an_envelope_re_encodes_byte_for_byte(record: Recording) -> None:
    """`release` removes an in-flight entry by exact value.

    An envelope that serialised differently on the way back would never match,
    and the job would stay in flight for ever. The trace field is a nested
    object, which is the one thing in the envelope that could break this, so it
    is asserted rather than assumed.
    """
    with span("api.request"):
        original = JobEnvelope.wrap("payload").with_trace(carrier())
    encoded = original.encode()

    assert JobEnvelope.decode(encoded).encode() == encoded


async def test_a_traced_job_can_be_released(
    record: Recording,
    redis: FakeQueueRedis,
    queue: AgentQueue,
) -> None:
    """The same invariant, through the queue rather than the dataclass."""
    with span("api.request"):
        await queue.enqueue(_job())

    reserved = await queue.reserve(wait_seconds=0)
    assert reserved is not None
    await queue.release(reserved)

    assert redis.lists.get(queue.namespace + ":inflight") == []
