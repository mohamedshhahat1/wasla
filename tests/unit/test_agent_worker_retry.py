"""The one queue whose retry policy is decided by *where* the failure happened.

An agent turn is not idempotent: it reserves an allowance, tools write rows,
and it ends by sending a customer a WhatsApp message that carries no
idempotency key. So this worker offers a second attempt only while nothing has
left the process, and stops offering one the moment the turn engages the
provider.

These drive `run_once` directly with a stubbed `_handle`, because what is being
proved is the decision around the turn rather than the turn itself - which has
its own suite and its own database.
"""

import ast
import inspect
import json
import textwrap
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from app.core.exceptions import ExternalServiceError, TenantIsolationError, ValidationError
from app.workers.ai_worker import AGENT_RETRY, AgentWorker, _TurnProgress
from app.workers.queue import AgentJob, JobEnvelope
from app.workers.retry import NO_RETRY
from tests.fake_queue_redis import FakeQueueRedis

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class FakeRedisClient:
    def __init__(self, client: AsyncClient) -> None:
        self.client = client


class _WorkerSettings:
    default_plan_code = "starter"
    openai_api_key = None
    openai_embedding_model = "text-embedding-3-small"
    openai_sentiment_model = "gpt-4.1-mini"


def build_worker(redis: FakeQueueRedis) -> AgentWorker:
    return AgentWorker(
        database=object(),  # type: ignore[arg-type]
        redis=FakeRedisClient(redis),  # type: ignore[arg-type]
        settings=_WorkerSettings(),  # type: ignore[arg-type]
    )


async def enqueue(worker: AgentWorker) -> None:
    await worker.queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)


# --------------------------------------------- before the provider is engaged


async def test_a_blip_before_the_provider_is_retried() -> None:
    """Loading a workspace and reading an allowance touch nothing outside."""
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def fail_early(job: AgentJob, progress: _TurnProgress) -> None:
        raise ExternalServiceError("the database was restarting")

    worker._handle = fail_early  # type: ignore[method-assign]
    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.delayed_depth() == 1
    assert await worker.queue.failed_depth() == 0


async def test_the_retried_job_carries_the_next_attempt() -> None:
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def fail_early(job: AgentJob, progress: _TurnProgress) -> None:
        raise ExternalServiceError("502")

    worker._handle = fail_early  # type: ignore[method-assign]
    await worker.run_once(wait_seconds=1)

    (scheduled,) = redis.zsets["agent:jobs:delayed"]
    assert JobEnvelope.decode(scheduled).attempt == 2


# ---------------------------------------------- after the provider is engaged


async def test_a_failure_after_the_provider_is_engaged_is_never_retried() -> None:
    """The invariant: a retry here could send a customer a second reply."""
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def fail_late(job: AgentJob, progress: _TurnProgress) -> None:
        progress.engaged = True
        raise ExternalServiceError("Meta rejected the message")

    worker._handle = fail_late  # type: ignore[method-assign]
    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.failed_depth() == 1


async def test_the_engaged_policy_is_the_refusing_one() -> None:
    """Stated directly, so the two constants cannot drift into agreeing."""
    assert NO_RETRY.max_attempts == 1
    assert AGENT_RETRY.max_attempts > 1


async def test_a_timeout_after_the_provider_is_engaged_is_not_retried() -> None:
    """The failure most tempting to retry, and the one that duplicates a reply.

    A timed-out send may have landed. `WhatsAppClient` already refuses to retry
    it at the HTTP layer for this reason; the queue must not undo that by
    running the whole turn again.
    """
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def time_out_late(job: AgentJob, progress: _TurnProgress) -> None:
        progress.engaged = True
        raise TimeoutError

    worker._handle = time_out_late  # type: ignore[method-assign]
    await worker.run_once(wait_seconds=1)

    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.failed_depth() == 1


# -------------------------------------------------------------- other paths


async def test_a_permanent_failure_before_the_provider_is_still_terminal() -> None:
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def refuse(job: AgentJob, progress: _TurnProgress) -> None:
        raise ValidationError("that conversation is not answerable")

    worker._handle = refuse  # type: ignore[method-assign]
    await worker.run_once(wait_seconds=1)

    assert await worker.queue.failed_depth() == 1


async def test_a_malformed_job_is_dead_lettered_without_a_retry() -> None:
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await worker.queue.enqueue_body("not a job at all", now=NOW)

    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.failed_depth() == 1


async def test_a_successful_turn_leaves_no_trace_in_either_list() -> None:
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def succeed(job: AgentJob, progress: _TurnProgress) -> None:
        progress.engaged = True

    worker._handle = succeed  # type: ignore[method-assign]
    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.depth() == 0
    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.failed_depth() == 0
    assert redis.lists["agent:jobs:inflight"] == []


async def test_an_empty_queue_is_not_a_job() -> None:
    worker = build_worker(FakeQueueRedis())

    assert await worker.run_once(wait_seconds=1) is False


@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_the_budget_is_spent_and_then_the_job_stops(attempt: int) -> None:
    """Three attempts, then the dead-letter list - never a fourth."""
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def fail_early(job: AgentJob, progress: _TurnProgress) -> None:
        raise ExternalServiceError("502")

    worker._handle = fail_early  # type: ignore[method-assign]

    for _ in range(attempt):
        await worker.run_once(wait_seconds=1)
        # Skip past the backoff so the next reserve can promote it.
        for member in list(redis.zsets.get("agent:jobs:delayed", {})):
            redis.zsets["agent:jobs:delayed"][member] = 0.0

    if attempt < AGENT_RETRY.max_attempts:
        assert await worker.queue.failed_depth() == 0
    else:
        assert await worker.queue.failed_depth() == 1
        assert await worker.queue.delayed_depth() == 0


# ------------------------- a conversation that is not visible yet (WSL-02)


async def test_a_conversation_that_is_not_visible_yet_is_retried_not_buried() -> None:
    """The failure this whole section exists for.

    The webhook enqueues this job inside the transaction that created the
    conversation, and `CommittingRoute` commits after the handler returns. A
    worker blocked on `BLMOVE` can reach the conversation first, and the scoped
    repository answers that with `TenantIsolationError` - `not_found`, which
    used to be a dead letter on attempt one and a customer nobody answered.
    """
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def not_visible_yet(job: AgentJob, progress: _TurnProgress) -> None:
        raise TenantIsolationError()

    worker._handle = not_visible_yet  # type: ignore[method-assign]
    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.delayed_depth() == 1
    assert await worker.queue.failed_depth() == 0


async def test_a_conversation_that_never_existed_stops_on_the_second_look() -> None:
    """Bounded, and bounded well short of the budget.

    Three attempts are available for a transient failure; a row that is
    genuinely gone gets two, because the second miss is the answer.
    """
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def never_existed(job: AgentJob, progress: _TurnProgress) -> None:
        raise TenantIsolationError()

    worker._handle = never_existed  # type: ignore[method-assign]

    await worker.run_once(wait_seconds=1)
    for member in list(redis.zsets.get("agent:jobs:delayed", {})):
        redis.zsets["agent:jobs:delayed"][member] = 0.0
    await worker.run_once(wait_seconds=1)

    assert await worker.queue.failed_depth() == 1
    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.depth() == 0
    record = json.loads((await worker.queue.dead_letters(limit=1))[0])
    assert record["category"] == "not_found"
    assert record["attempts"] == 2


async def test_a_turn_that_engaged_the_provider_is_never_retried_for_not_found() -> None:
    """The invariant the one-shot door must not reach.

    A turn that has called OpenAI or Meta may already have answered the
    customer. A conversation deleted underneath it afterwards reports
    `not_found`, and retrying on that would send a second reply.
    """
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def vanish_after_engaging(job: AgentJob, progress: _TurnProgress) -> None:
        progress.engaged = True
        raise TenantIsolationError()

    worker._handle = vanish_after_engaging  # type: ignore[method-assign]
    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.failed_depth() == 1


async def test_the_retry_carries_the_trace_the_job_was_queued_with() -> None:
    """Attempt two is part of the story the webhook started.

    The carrier is the only thing that connects a worker's attempt to the
    request that queued it, and a retry that dropped it would leave an
    operator following a customer's message into silence.
    """
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    carrier = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
    envelope = JobEnvelope.wrap(
        AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION).encode(), now=NOW
    ).with_trace(carrier)
    # Pushed directly, because `enqueue` injects whatever carrier the current
    # process happens to hold and this test needs a known one.
    await redis.rpush("agent:jobs:pending", envelope.encode())

    async def not_visible_yet(job: AgentJob, progress: _TurnProgress) -> None:
        raise TenantIsolationError()

    worker._handle = not_visible_yet  # type: ignore[method-assign]
    await worker.run_once(wait_seconds=1)

    (scheduled,) = redis.zsets["agent:jobs:delayed"]
    assert JobEnvelope.decode(scheduled).trace == carrier


async def test_the_retry_names_the_same_workspace_and_conversation() -> None:
    """The job's identity is its payload, and the retry carries it verbatim."""
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def not_visible_yet(job: AgentJob, progress: _TurnProgress) -> None:
        raise TenantIsolationError()

    worker._handle = not_visible_yet  # type: ignore[method-assign]
    await worker.run_once(wait_seconds=1)

    (scheduled,) = redis.zsets["agent:jobs:delayed"]
    retried = AgentJob.decode(JobEnvelope.decode(scheduled).body)
    assert retried.tenant_id == TENANT
    assert retried.conversation_id == CONVERSATION


# ------------------------------------------------- where the marker actually is


def _handle_body() -> ast.AsyncFunctionDef:
    source = inspect.getsource(AgentWorker._handle)
    module = ast.parse(textwrap.dedent(source))
    function = module.body[0]
    assert isinstance(function, ast.AsyncFunctionDef)
    return function


def _line_of(node: ast.AST) -> int:
    return getattr(node, "lineno", 0)


def test_the_turn_is_marked_engaged_before_anything_leaves_the_process() -> None:
    """Read out of `_handle` itself, because the tests above cannot see it.

    Every other test in this file stubs `_handle` and drives `progress` by
    hand, which proves the *decision* - engaged means no retry - and proves
    nothing about where the real code marks it. A mutation probe that replaced
    the marker with its opposite passed this whole file, which is exactly the
    gap a stub leaves behind.

    So the placement is asserted structurally. The invariant is narrow and
    positional: the turn is marked engaged, and it is marked before the HTTP
    client that carries it to OpenAI and to Meta is built. Everything before
    that point is a transaction that rolls back; everything after it may have
    reserved an allowance, called a provider or sent a customer a message.
    """
    body = _handle_body()

    marks = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "engage"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "progress"
    ]
    assert marks, "`_handle` never marks the turn engaged"

    clients = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_http_client"
    ]
    assert clients, "`_handle` no longer builds the turn's HTTP client here"

    assert min(_line_of(node) for node in marks) < min(_line_of(node) for node in clients), (
        "the turn must be marked engaged *before* the HTTP client is built; "
        "after that line a retry can bill a second inference or send a second reply"
    )


def test_the_conversation_is_looked_up_before_the_turn_is_marked_engaged() -> None:
    """Asserted structurally, because the retry decision depends on it.

    The orchestrator loads this conversation too and refuses a missing one
    identically - but it does so *after* the mark, where the only honest policy
    is `NO_RETRY`. That is what dead-lettered a conversation that was merely
    mid-commit on attempt one. Moving the lookup back across the mark is the
    fix, and a lookup that drifted forward again would restore the defect while
    every behavioural test in this file still passed.
    """
    body = _handle_body()

    lookups = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ConversationRepository"
    ]
    assert lookups, "`_handle` no longer checks the conversation exists"

    marks = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "engage"
    ]
    assert marks, "`_handle` never marks the turn engaged"

    assert max(_line_of(node) for node in lookups) < min(_line_of(node) for node in marks), (
        "the conversation must be resolved *before* the turn is marked engaged; "
        "after that line the policy is NO_RETRY and a row that was mid-commit "
        "is dead-lettered on attempt one"
    )


async def test_marking_the_turn_engaged_persists_it_outside_the_process() -> None:
    """The in-memory flag is not enough, and this is why.

    A worker that dies takes `_TurnProgress` with it. If the fact never
    reached Redis, a recovery pass would find a reservation it could not
    classify and would have to guess about a WhatsApp message that may already
    have been sent (ADR-074).
    """
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)
    raw = await worker.queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    engaged: list[bool] = []

    async def engage_then_fail(job: AgentJob, progress: _TurnProgress) -> None:
        await progress.engage()
        engaged.append(progress.engaged)
        raise ExternalServiceError("Meta rejected the message")

    # The reservation the worker is about to hand `_handle` is the one just
    # reserved, so put it back where `run_once` will find it.
    redis.lists["agent:jobs:inflight"].remove(raw)
    redis.lists["agent:jobs:pending"].append(raw)
    await redis.hdel("agent:jobs:reservations", raw)

    worker._handle = engage_then_fail  # type: ignore[method-assign]
    await worker.run_once(wait_seconds=1)

    assert engaged == [True]
    # The turn failed after engaging, so it was dead-lettered rather than
    # retried - and the reservation is gone because the worker acknowledged it.
    assert await worker.queue.failed_depth() == 1
    assert await worker.queue.delayed_depth() == 0
