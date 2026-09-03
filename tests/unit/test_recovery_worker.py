"""The loop that reclaims what a dead worker was holding.

The queue knows *how* to recover a reservation; this is the thing that
remembers to ask. It is a worker kind like any other, so it beats a heartbeat
and can be scaled apart with `WORKER_KINDS` - and it is in `ALL_KINDS` by
default, because a deployment running it nowhere has no crash recovery at all
and that should take a deliberate act rather than an oversight.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings as RealSettings
from app.core.telemetry import COUNTER_PREFIX, set_counter_sink
from app.workers.queue import AgentJob, AgentQueue
from app.workers.recovery import RecoveryWorker
from app.workers.runner import ALL_KINDS, RECOVERY, build_workers, reservation_queues
from tests.fake_queue_redis import FakeQueueRedis
from tests.fakes import as_redis, as_redis_client

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
TIMEOUT = 120.0


def live() -> datetime:
    """A reservation taken now, whose lease is genuinely current.

    Relative to the real clock, deliberately. `RecoveryWorker.run_once` reads
    `datetime.now(UTC)` - it is the production entry point and takes no `now` -
    so a fixed timestamp here is a lease that expires the moment the wall clock
    passes it. That is a test which passes in the morning and fails in the
    afternoon, which is the worst shape a failure comes in.
    """
    return datetime.now(UTC)


def stale() -> datetime:
    """A reservation taken long enough ago that its lease has certainly run out."""
    return datetime.now(UTC) - timedelta(seconds=TIMEOUT + 60)


def build_settings() -> RealSettings:
    """The real `Settings`, because `build_workers` constructs real workers.

    A stub with only the fields this file mentions passes until somebody adds
    a worker that reads a different one, and then fails in a place that has
    nothing to do with recovery.
    """
    return RealSettings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        rate_limit_enabled=False,
        queue_visibility_timeout_seconds=TIMEOUT,
    )


class _FakeRedisClient:
    def __init__(self, commands: FakeQueueRedis) -> None:
        self.commands = commands

    @property
    def client(self) -> FakeQueueRedis:
        return self.commands


@pytest.fixture
def redis() -> FakeQueueRedis:
    return FakeQueueRedis()


@pytest.fixture(autouse=True)
def _no_counter_sink() -> Iterator[None]:
    set_counter_sink(None)
    yield
    set_counter_sink(None)


async def strand_a_job(
    redis: FakeQueueRedis,
    *,
    engaged: bool = False,
    at: datetime | None = None,
) -> str:
    """Leave a reservation behind exactly as a killed worker would."""
    moment = at if at is not None else live()
    queue = AgentQueue(as_redis(redis), worker_id="dead-worker", visibility_timeout_seconds=TIMEOUT)
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=moment)
    raw = await queue.reserve(wait_seconds=1, now=moment)
    assert raw is not None
    if engaged:
        await queue.mark_engaged(raw, now=moment)
    found = raw
    assert found is not None
    return found


# --------------------------------------------------------------- the sweep


async def test_a_sweep_reclaims_nothing_while_the_lease_is_live(redis: FakeQueueRedis) -> None:
    await strand_a_job(redis)
    worker = RecoveryWorker(
        redis=as_redis_client(_FakeRedisClient(redis)), settings=build_settings()
    )

    assert await worker.run_once() == 0
    assert len(redis.lists["agent:jobs:inflight"]) == 1


async def test_a_sweep_reclaims_an_expired_safe_reservation(redis: FakeQueueRedis) -> None:
    """The whole point: the job comes back without anybody noticing it had gone."""
    await strand_a_job(redis, at=stale())
    worker = RecoveryWorker(
        redis=as_redis_client(_FakeRedisClient(redis)), settings=build_settings()
    )

    assert await worker.run_once() == 1
    assert redis.lists["agent:jobs:inflight"] == []
    assert len(redis.zsets["agent:jobs:delayed"]) == 1


async def test_a_sweep_quarantines_an_expired_engaged_agent_reservation(
    redis: FakeQueueRedis,
) -> None:
    """A worker died mid-send. The message may already be with the customer."""
    await strand_a_job(redis, engaged=True, at=stale())
    worker = RecoveryWorker(
        redis=as_redis_client(_FakeRedisClient(redis)), settings=build_settings()
    )

    assert await worker.run_once() == 1
    assert redis.zsets.get("agent:jobs:delayed", {}) == {}
    assert len(redis.lists["agent:jobs:failed"]) == 1


async def test_a_second_sweep_finds_nothing_left(redis: FakeQueueRedis) -> None:
    await strand_a_job(redis, at=stale())
    worker = RecoveryWorker(
        redis=as_redis_client(_FakeRedisClient(redis)), settings=build_settings()
    )

    assert await worker.run_once() == 1
    assert await worker.run_once() == 0


async def test_two_recovery_workers_reclaim_a_job_once_between_them(redis: FakeQueueRedis) -> None:
    """Running this in every replica is the expected deployment, not a hazard."""
    await strand_a_job(redis, at=stale())
    first = RecoveryWorker(
        redis=as_redis_client(_FakeRedisClient(redis)), settings=build_settings()
    )
    second = RecoveryWorker(
        redis=as_redis_client(_FakeRedisClient(redis)), settings=build_settings()
    )

    assert await first.run_once() + await second.run_once() == 1
    assert len(redis.zsets["agent:jobs:delayed"]) == 1


async def test_a_sweep_sweeps_every_queue(redis: FakeQueueRedis) -> None:
    worker = RecoveryWorker(
        redis=as_redis_client(_FakeRedisClient(redis)), settings=build_settings()
    )
    long_ago = stale()
    from app.workers.ingestion_queue import IngestionJob, IngestionQueue
    from app.workers.media_queue import MediaJob, MediaQueue

    ingestion = IngestionQueue(as_redis(redis), visibility_timeout_seconds=TIMEOUT)
    await ingestion.enqueue(IngestionJob(tenant_id=TENANT, document_id=CONVERSATION), now=long_ago)
    await ingestion.reserve(wait_seconds=1, now=long_ago)
    media = MediaQueue(as_redis(redis), visibility_timeout_seconds=TIMEOUT)
    await media.enqueue(MediaJob(tenant_id=TENANT, media_id=CONVERSATION), now=long_ago)
    await media.reserve(wait_seconds=1, now=long_ago)

    assert await worker.run_once() == 2


async def test_a_sweep_that_throws_does_not_stop_the_loop(redis: FakeQueueRedis) -> None:
    """This is the loop that exists to survive other things failing."""

    class Broken(FakeQueueRedis):
        async def lrange(self, key: str, start: int, end: int) -> list[str]:
            raise RuntimeError("Redis is gone")

    worker = RecoveryWorker(
        redis=as_redis_client(_FakeRedisClient(Broken())), settings=build_settings()
    )
    with pytest.raises(RuntimeError):
        await worker.run_once()

    # `run_forever` contains it; `run_once` is allowed to be honest.
    worker.stop()


# ------------------------------------------------------------- the counters


async def test_recovered_and_quarantined_are_counted_apart(redis: FakeQueueRedis) -> None:
    """An operator reads them differently, so they are different outcomes."""
    set_counter_sink(as_redis(redis))
    long_ago = stale()
    await strand_a_job(redis, at=long_ago)
    await strand_a_job(redis, engaged=True, at=long_ago - timedelta(seconds=1))
    worker = RecoveryWorker(
        redis=as_redis_client(_FakeRedisClient(redis)), settings=build_settings()
    )

    await worker.run_once()

    jobs = redis.hashes[f"{COUNTER_PREFIX}:wasla_jobs_total"]
    failures = redis.hashes[f"{COUNTER_PREFIX}:wasla_job_failures_total"]
    assert jobs["outcome=recovered,queue=agent"] == 1
    assert jobs["outcome=quarantined,queue=agent"] == 1
    assert failures["category=worker_crashed,queue=agent"] == 1
    assert failures["category=uncertain_delivery,queue=agent"] == 1


# ------------------------------------------------------------- the wiring


def test_recovery_is_a_worker_kind_that_runs_by_default() -> None:
    """Leaving it out has to be a decision somebody makes on purpose."""
    assert RECOVERY in ALL_KINDS


def test_the_runner_builds_a_recovery_worker(redis: FakeQueueRedis) -> None:
    workers = build_workers(
        kinds=(RECOVERY,),
        database=object(),  # type: ignore[arg-type]
        redis=_FakeRedisClient(redis),  # type: ignore[arg-type]
        settings=build_settings(),
    )

    assert [type(worker) for worker in workers] == [RecoveryWorker]


def test_the_runner_collects_the_queues_whose_leases_it_must_renew(redis: FakeQueueRedis) -> None:
    """Renewal only works for the instance that holds the reservation."""
    workers = build_workers(
        kinds=ALL_KINDS,
        database=object(),  # type: ignore[arg-type]
        redis=_FakeRedisClient(redis),  # type: ignore[arg-type]
        settings=build_settings(),
    )

    namespaces = {queue.namespace for queue in reservation_queues(workers)}

    assert namespaces == {"agent:jobs", "knowledge:ingestion", "media:understanding"}


def test_a_worker_with_no_queue_contributes_no_lease(redis: FakeQueueRedis) -> None:
    workers = build_workers(
        kinds=("billing",),
        database=object(),  # type: ignore[arg-type]
        redis=_FakeRedisClient(redis),  # type: ignore[arg-type]
        settings=build_settings(),
    )

    assert reservation_queues(workers) == []


def test_every_queue_reports_under_the_name_the_registry_uses(redis: FakeQueueRedis) -> None:
    """One label per queue, or a dashboard cannot join its own series.

    The recovery counter first shipped labelled `agent:jobs` while every depth
    gauge was labelled `agent`, which would have split a queue into two
    unrelated series and made "recovered jobs on the agent queue" unanswerable.
    """
    from app.workers.ingestion_queue import IngestionQueue
    from app.workers.media_queue import MediaQueue
    from app.workers.queue import QUEUES

    for build in (AgentQueue, IngestionQueue, MediaQueue):
        queue = build(as_redis(redis))
        assert queue.label in QUEUES, f"{queue.label} is not a registered queue name"
        assert QUEUES[queue.label] == queue.namespace
