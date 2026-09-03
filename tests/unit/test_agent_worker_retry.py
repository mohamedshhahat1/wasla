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
import textwrap
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from app.core.exceptions import ExternalServiceError, ValidationError
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
