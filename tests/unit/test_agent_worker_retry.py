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

from app.core.exceptions import ExternalServiceError, ValidationError
from app.workers.ai_worker import AGENT_RETRY, AgentWorker
from app.workers.queue import AgentJob, JobEnvelope
from app.workers.retry import NO_RETRY
from tests.fake_queue_redis import FakeQueueRedis

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class FakeRedisClient:
    def __init__(self, client) -> None:
        self.client = client


class Settings:
    default_plan_code = "starter"
    openai_api_key = None
    openai_embedding_model = "text-embedding-3-small"
    openai_sentiment_model = "gpt-4.1-mini"


def build_worker(redis):
    return AgentWorker(
        database=object(),  # type: ignore[arg-type]
        redis=FakeRedisClient(redis),  # type: ignore[arg-type]
        settings=Settings(),  # type: ignore[arg-type]
    )


async def enqueue(worker):
    await worker.queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=NOW)


# --------------------------------------------- before the provider is engaged


async def test_a_blip_before_the_provider_is_retried():
    """Loading a workspace and reading an allowance touch nothing outside."""
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def fail_early(job, progress):
        raise ExternalServiceError("the database was restarting")

    worker._handle = fail_early  # type: ignore[method-assign]
    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.delayed_depth() == 1
    assert await worker.queue.failed_depth() == 0


async def test_the_retried_job_carries_the_next_attempt():
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def fail_early(job, progress):
        raise ExternalServiceError("502")

    worker._handle = fail_early  # type: ignore[method-assign]
    await worker.run_once(wait_seconds=1)

    (scheduled,) = redis.zsets["agent:jobs:delayed"]
    assert JobEnvelope.decode(scheduled).attempt == 2


# ---------------------------------------------- after the provider is engaged


async def test_a_failure_after_the_provider_is_engaged_is_never_retried():
    """The invariant: a retry here could send a customer a second reply."""
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def fail_late(job, progress):
        progress.engaged = True
        raise ExternalServiceError("Meta rejected the message")

    worker._handle = fail_late  # type: ignore[method-assign]
    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.failed_depth() == 1


async def test_the_engaged_policy_is_the_refusing_one():
    """Stated directly, so the two constants cannot drift into agreeing."""
    assert NO_RETRY.max_attempts == 1
    assert AGENT_RETRY.max_attempts > 1


async def test_a_timeout_after_the_provider_is_engaged_is_not_retried():
    """The failure most tempting to retry, and the one that duplicates a reply.

    A timed-out send may have landed. `WhatsAppClient` already refuses to retry
    it at the HTTP layer for this reason; the queue must not undo that by
    running the whole turn again.
    """
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def time_out_late(job, progress):
        progress.engaged = True
        raise TimeoutError

    worker._handle = time_out_late  # type: ignore[method-assign]
    await worker.run_once(wait_seconds=1)

    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.failed_depth() == 1


# -------------------------------------------------------------- other paths


async def test_a_permanent_failure_before_the_provider_is_still_terminal():
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def refuse(job, progress):
        raise ValidationError("that conversation is not answerable")

    worker._handle = refuse  # type: ignore[method-assign]
    await worker.run_once(wait_seconds=1)

    assert await worker.queue.failed_depth() == 1


async def test_a_malformed_job_is_dead_lettered_without_a_retry():
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await worker.queue.enqueue_body("not a job at all", now=NOW)

    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.failed_depth() == 1


async def test_a_successful_turn_leaves_no_trace_in_either_list():
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def succeed(job, progress):
        progress.engaged = True

    worker._handle = succeed  # type: ignore[method-assign]
    assert await worker.run_once(wait_seconds=1) is True

    assert await worker.queue.depth() == 0
    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.failed_depth() == 0
    assert redis.lists["agent:jobs:inflight"] == []


async def test_an_empty_queue_is_not_a_job():
    worker = build_worker(FakeQueueRedis())

    assert await worker.run_once(wait_seconds=1) is False


@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_the_budget_is_spent_and_then_the_job_stops(attempt):
    """Three attempts, then the dead-letter list - never a fourth."""
    redis = FakeQueueRedis()
    worker = build_worker(redis)
    await enqueue(worker)

    async def fail_early(job, progress):
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


def test_the_turn_is_marked_engaged_before_anything_leaves_the_process():
    """Read out of `_handle` itself, because the tests above cannot see it.

    Every other test in this file stubs `_handle` and sets `progress.engaged`
    by hand, which proves the *decision* - engaged means no retry - and proves
    nothing about where the real code sets it. A mutation probe that replaced
    `progress.engaged = True` with `False` passed this whole file, which is
    exactly the gap a stub leaves behind.

    So the placement is asserted structurally. The invariant is narrow and
    positional: the marker is set, and it is set before the HTTP client that
    carries the turn to OpenAI and to Meta is built. Everything before that
    point is a transaction that rolls back; everything after it may have
    reserved an allowance, called a provider or sent a customer a message.
    """
    body = _handle_body()

    assignments = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and target.attr == "engaged"
            and isinstance(target.value, ast.Name)
            and target.value.id == "progress"
            for target in node.targets
        )
    ]
    assert assignments, "`_handle` never marks the turn engaged"

    truthy = [
        node
        for node in assignments
        if isinstance(node.value, ast.Constant) and node.value.value is True
    ]
    assert truthy, "`_handle` sets `progress.engaged` to something other than True"

    clients = [
        node
        for node in ast.walk(body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_http_client"
    ]
    assert clients, "`_handle` no longer builds the turn's HTTP client here"

    assert min(_line_of(node) for node in truthy) < min(_line_of(node) for node in clients), (
        "the turn must be marked engaged *before* the HTTP client is built; "
        "after that line a retry can bill a second inference or send a second reply"
    )
