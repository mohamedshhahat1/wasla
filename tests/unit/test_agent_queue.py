"""The agent job queue.

The queue takes a raw Redis client, so these tests hand it a fake that records
the five list commands it uses.
"""

import uuid

import pytest

from app.workers.queue import AgentJob, AgentQueue, MalformedJobError

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
AGENT = uuid.UUID("33333333-3333-3333-3333-333333333333")

PENDING = "agent:jobs:pending"
INFLIGHT = "agent:jobs:inflight"
FAILED = "agent:jobs:failed"


class FakeRedis:
    def __init__(self, reserved=None):
        self.pushed = []
        self.removed = []
        self.moves = []
        self.lengths = {}
        self._reserved = reserved

    async def rpush(self, key, value):
        self.pushed.append((key, value))
        return 1

    # ASYNC109 wants asyncio.timeout, but this mirrors redis-py's own
    # signature: the fake has to accept what the queue actually passes.
    async def blmove(self, source, destination, timeout):  # noqa: ASYNC109
        self.moves.append((source, destination, timeout))
        return self._reserved

    async def lrem(self, key, count, value):
        self.removed.append((key, count, value))
        return 1

    async def llen(self, key):
        return self.lengths.get(key, 0)


def test_a_job_survives_a_round_trip():
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION, agent_id=AGENT)

    assert AgentJob.decode(job.encode()) == job


def test_a_job_without_a_chosen_agent_survives_a_round_trip():
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)

    decoded = AgentJob.decode(job.encode())

    assert decoded == job
    assert decoded.agent_id is None


def test_identical_jobs_encode_identically():
    """Releasing a job removes it by exact value, so encoding must be stable."""
    first = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)
    second = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)

    assert first.encode() == second.encode()


def test_encoded_keys_are_sorted():
    encoded = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION).encode()

    assert encoded.index("conversation_id") < encoded.index("tenant_id")


def test_text_that_is_not_json_is_malformed():
    with pytest.raises(MalformedJobError):
        AgentJob.decode("not json at all")


def test_json_that_is_not_an_object_is_malformed():
    with pytest.raises(MalformedJobError):
        AgentJob.decode("[1, 2, 3]")


def test_a_missing_identifier_is_malformed():
    with pytest.raises(MalformedJobError):
        AgentJob.decode('{"tenant_id": "11111111-1111-1111-1111-111111111111"}')


def test_an_unusable_identifier_is_malformed():
    with pytest.raises(MalformedJobError):
        AgentJob.decode('{"tenant_id": "not-a-uuid", "conversation_id": "also-not"}')


async def test_enqueue_appends_to_the_pending_list():
    redis = FakeRedis()
    queue = AgentQueue(redis)
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)

    await queue.enqueue(job)

    assert redis.pushed == [(PENDING, job.encode())]


async def test_reserving_moves_the_job_to_the_in_flight_list():
    """Popping outright would lose the job if the worker died mid-turn."""
    job = AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION)
    redis = FakeRedis(reserved=job.encode())
    queue = AgentQueue(redis)

    raw = await queue.reserve(wait_seconds=1)

    assert raw == job.encode()
    assert redis.moves == [(PENDING, INFLIGHT, 1)]


async def test_reserving_returns_nothing_when_the_queue_is_quiet():
    queue = AgentQueue(FakeRedis(reserved=None))

    assert await queue.reserve(wait_seconds=1) is None


async def test_releasing_removes_the_job_from_the_in_flight_list():
    redis = FakeRedis()
    queue = AgentQueue(redis)

    await queue.release("payload")

    assert redis.removed == [(INFLIGHT, 1, "payload")]


async def test_failing_dead_letters_the_job():
    """A failed job is the only evidence a customer went unanswered."""
    redis = FakeRedis()
    queue = AgentQueue(redis)

    await queue.fail("payload")

    assert redis.removed == [(INFLIGHT, 1, "payload")]
    assert redis.pushed == [(FAILED, "payload")]


async def test_depth_counts_only_waiting_jobs():
    redis = FakeRedis()
    redis.lengths[PENDING] = 3
    redis.lengths[FAILED] = 7
    queue = AgentQueue(redis)

    assert await queue.depth() == 3
    assert await queue.failed_depth() == 7
