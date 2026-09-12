"""The operator's way into a dead-letter list, and back out of one.

The interesting property is the refusal. Replaying an ingestion job costs an
embedding call and changes nothing anybody sees; replaying an agent job can
send a customer a second answer to a question that already has one, so the
command will not do it without being told twice.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest

from app.workers.queue import DeadLetterRecord, JobEnvelope, ReliableQueue
from app.workers.queues import IDEMPOTENT_QUEUES, build_parser, dead_letters, replay, status
from app.workers.retry import FailureCategory
from tests.fake_queue_redis import FakeQueueRedis
from tests.fakes import as_redis, as_redis_client

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
JOB = uuid.UUID("22222222-2222-2222-2222-222222222222")


class RedisWrapper:
    """Mirrors `RedisClient`: the command reaches for `.client`."""

    def __init__(self, commands: FakeQueueRedis) -> None:
        self.commands = commands

    @property
    def client(self) -> FakeQueueRedis:
        return self.commands


@pytest.fixture
def redis() -> FakeQueueRedis:
    return FakeQueueRedis()


@pytest.fixture
def wrapper(redis: FakeQueueRedis) -> RedisWrapper:
    return RedisWrapper(redis)


async def dead_letter_one(
    redis: FakeQueueRedis,
    *,
    namespace: str,
    body: str,
    category: FailureCategory = FailureCategory.PROVIDER_ERROR,
    job_id: str = str(JOB),
) -> None:
    queue = ReliableQueue(as_redis(redis), namespace=namespace)
    await queue.enqueue_body(body, now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None
    await queue.dead_letter(
        raw,
        DeadLetterRecord(
            queue=namespace,
            job_type=namespace.split(":")[0],
            tenant_id=str(TENANT),
            job_id=job_id,
            attempts=5,
            category=category,
            enqueued_at=JobEnvelope.decode(raw).enqueued_at,
            first_attempted_at=NOW,
            last_attempted_at=NOW,
            dead_lettered_at=NOW,
            body=body,
        ),
    )


# ------------------------------------------------------------------ status


async def test_status_reports_every_queue(
    wrapper: RedisWrapper, capsys: pytest.CaptureFixture[str]
) -> None:
    await ReliableQueue(as_redis(wrapper.client), namespace="agent:jobs").enqueue_body(
        "{}", now=NOW
    )

    assert await status(as_redis_client(wrapper)) == 0

    out = capsys.readouterr().out
    for name in ("agent", "ingestion", "media"):
        assert name in out


# ------------------------------------------------------------ dead-letters


async def test_dead_letters_prints_the_record(
    wrapper: RedisWrapper, redis: FakeQueueRedis, capsys: pytest.CaptureFixture[str]
) -> None:
    await dead_letter_one(redis, namespace="knowledge:ingestion", body='{"document_id":"x"}')

    assert await dead_letters(as_redis_client(wrapper), queue_name="ingestion", limit=10) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["attempts"] == 5
    assert printed["category"] == "provider_error"
    assert printed["tenant_id"] == str(TENANT)


async def test_an_empty_dead_letter_list_says_so(
    wrapper: RedisWrapper, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await dead_letters(as_redis_client(wrapper), queue_name="agent", limit=10) == 0

    assert "no dead-lettered jobs" in capsys.readouterr().out


async def test_an_unknown_queue_is_refused(wrapper: RedisWrapper) -> None:
    assert await dead_letters(as_redis_client(wrapper), queue_name="nonsense", limit=10) == 2


# ----------------------------------------------------------------- replay


async def test_replaying_an_idempotent_queue_requeues_the_job(
    wrapper: RedisWrapper, redis: FakeQueueRedis
) -> None:
    await dead_letter_one(redis, namespace="knowledge:ingestion", body='{"document_id":"x"}')
    queue = ReliableQueue(as_redis(redis), namespace="knowledge:ingestion")

    assert (
        await replay(as_redis_client(wrapper), queue_name="ingestion", limit=10, force=False) == 0
    )

    assert await queue.depth() == 1
    (queued,) = redis.lists["knowledge:ingestion:pending"]
    assert JobEnvelope.decode(queued).body == '{"document_id":"x"}'


async def test_a_replayed_job_starts_its_attempt_count_again(
    wrapper: RedisWrapper, redis: FakeQueueRedis
) -> None:
    """The budget was what said it was finished; an operator has overruled that."""
    await dead_letter_one(redis, namespace="media:understanding", body='{"media_id":"x"}')

    await replay(as_redis_client(wrapper), queue_name="media", limit=10, force=False)

    (queued,) = redis.lists["media:understanding:pending"]
    assert JobEnvelope.decode(queued).attempt == 1


async def test_the_agent_queue_is_refused_without_force(
    wrapper: RedisWrapper, redis: FakeQueueRedis, capsys: pytest.CaptureFixture[str]
) -> None:
    """An agent turn ends in a message; a replay could send a second one."""
    await dead_letter_one(redis, namespace="agent:jobs", body='{"conversation_id":"x"}')

    assert await replay(as_redis_client(wrapper), queue_name="agent", limit=10, force=False) == 3

    assert await ReliableQueue(as_redis(redis), namespace="agent:jobs").depth() == 0
    assert "not idempotent" in capsys.readouterr().err


async def test_the_agent_queue_can_be_replayed_deliberately(
    wrapper: RedisWrapper, redis: FakeQueueRedis
) -> None:
    await dead_letter_one(redis, namespace="agent:jobs", body='{"conversation_id":"x"}')

    assert await replay(as_redis_client(wrapper), queue_name="agent", limit=10, force=True) == 0

    assert await ReliableQueue(as_redis(redis), namespace="agent:jobs").depth() == 1


async def test_the_record_survives_the_replay(wrapper: RedisWrapper, redis: FakeQueueRedis) -> None:
    """Comparing the original with a second failure is how an operator learns."""
    await dead_letter_one(redis, namespace="knowledge:ingestion", body='{"document_id":"x"}')
    queue = ReliableQueue(as_redis(redis), namespace="knowledge:ingestion")

    await replay(as_redis_client(wrapper), queue_name="ingestion", limit=10, force=False)

    assert await queue.failed_depth() == 1


async def test_replaying_nothing_is_not_an_error(
    wrapper: RedisWrapper, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        await replay(as_redis_client(wrapper), queue_name="ingestion", limit=10, force=False) == 0
    )

    assert "nothing to replay" in capsys.readouterr().out


async def test_an_unreadable_record_is_skipped_rather_than_fatal(
    wrapper: RedisWrapper, redis: FakeQueueRedis, capsys: pytest.CaptureFixture[str]
) -> None:
    redis.lists["knowledge:ingestion:failed"] = ["not json", '{"no":"body"}']

    assert (
        await replay(as_redis_client(wrapper), queue_name="ingestion", limit=10, force=False) == 0
    )

    assert await ReliableQueue(as_redis(redis), namespace="knowledge:ingestion").depth() == 0
    assert "skipping" in capsys.readouterr().out


# ------------------------------------------------------------------ the CLI


def test_the_idempotent_queues_are_the_ones_their_workers_retry() -> None:
    """Stated here so the two lists cannot drift apart silently."""
    assert {"ingestion", "media"} == IDEMPOTENT_QUEUES
    assert "agent" not in IDEMPOTENT_QUEUES


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["status"], id="status"),
        pytest.param(["dead-letters", "agent"], id="dead-letters"),
        pytest.param(["replay", "ingestion", "--force"], id="replay"),
    ],
)
def test_the_parser_accepts_the_documented_invocations(argv: list[str]) -> None:
    build_parser().parse_args(argv)


def test_the_parser_refuses_a_queue_that_does_not_exist() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["replay", "nonsense"])


# ------------------------------------------- per-record replay safety, WQ-06


async def _mixed_list(redis: FakeQueueRedis) -> None:
    """The list a provider outage actually produces: ordinary and uncertain.

    Three records, with the uncertain one in the middle so it cannot be skipped
    by an off-by-one at either end of the batch.
    """
    await dead_letter_one(
        redis,
        namespace="agent:jobs",
        body='{"conversation_id":"first"}',
        category=FailureCategory.PROVIDER_ERROR,
        job_id="job-first",
    )
    await dead_letter_one(
        redis,
        namespace="agent:jobs",
        body='{"conversation_id":"uncertain"}',
        category=FailureCategory.UNCERTAIN_DELIVERY,
        job_id="job-uncertain",
    )
    await dead_letter_one(
        redis,
        namespace="agent:jobs",
        body='{"conversation_id":"third"}',
        category=FailureCategory.PROVIDER_ERROR,
        job_id="job-third",
    )


def _requeued(redis: FakeQueueRedis) -> list[str]:
    """The payloads now waiting on the agent queue."""
    return [JobEnvelope.decode(entry).body for entry in redis.lists.get("agent:jobs:pending", [])]


async def test_the_mixed_list_really_is_mixed(redis: FakeQueueRedis) -> None:
    """Non-vacuity for everything below.

    If the fixture produced three ordinary records, "the uncertain one was not
    replayed" would be true of a list containing no uncertain one, and every
    test in this section would pass while proving nothing.
    """
    await _mixed_list(redis)

    categories = [json.loads(entry)["category"] for entry in redis.lists["agent:jobs:failed"]]
    assert categories.count(str(FailureCategory.UNCERTAIN_DELIVERY)) == 1
    assert categories.count(str(FailureCategory.PROVIDER_ERROR)) == 2


async def test_force_replays_the_ordinary_failures_and_protects_the_uncertain_one(
    wrapper: RedisWrapper, redis: FakeQueueRedis, capsys: pytest.CaptureFixture[str]
) -> None:
    """The finding, inverted.

    `--force` used to take the whole batch, so an operator recovering from a
    provider outage - which is exactly when the list is long and mixed - sent a
    second copy of a reply the customer may already have had. The queue gate was
    never the wrong idea; it was the wrong *granularity* (WQ-06).
    """
    await _mixed_list(redis)

    assert await replay(as_redis_client(wrapper), queue_name="agent", limit=10, force=True) == 0

    assert sorted(_requeued(redis)) == [
        '{"conversation_id":"first"}',
        '{"conversation_id":"third"}',
    ]
    output = capsys.readouterr().out
    assert "PROTECTED" in output
    assert "1 left alone as uncertain_delivery" in output


async def test_an_uncertain_record_needs_a_second_deliberate_flag(
    wrapper: RedisWrapper, redis: FakeQueueRedis
) -> None:
    """Two decisions, asked separately, because they are about different risks.

    `--force` says this queue may be replayed at all. `--include-uncertain`
    says this record may be, knowing the customer might already have it. One
    keystroke should not answer both.
    """
    await _mixed_list(redis)

    assert (
        await replay(
            as_redis_client(wrapper),
            queue_name="agent",
            limit=10,
            force=True,
            include_uncertain=True,
        )
        == 0
    )

    assert '{"conversation_id":"uncertain"}' in _requeued(redis)


async def test_one_record_can_be_replayed_on_its_own(
    wrapper: RedisWrapper, redis: FakeQueueRedis
) -> None:
    """The narrow action an operator who has read one conversation wants.

    Without it the only way to recover a single job was to replay a batch, which
    is how a careful decision about one conversation became a careless one about
    twenty.
    """
    await _mixed_list(redis)

    assert (
        await replay(
            as_redis_client(wrapper),
            queue_name="agent",
            limit=10,
            force=True,
            job_id="job-third",
        )
        == 0
    )

    assert _requeued(redis) == ['{"conversation_id":"third"}']


async def test_a_targeted_uncertain_replay_is_still_refused_without_the_flag(
    wrapper: RedisWrapper, redis: FakeQueueRedis
) -> None:
    """Naming the record is not the same as accepting what replaying it means."""
    await _mixed_list(redis)

    assert (
        await replay(
            as_redis_client(wrapper),
            queue_name="agent",
            limit=10,
            force=True,
            job_id="job-uncertain",
        )
        == 0
    )

    assert _requeued(redis) == []


async def test_a_dry_run_changes_nothing(
    wrapper: RedisWrapper, redis: FakeQueueRedis, capsys: pytest.CaptureFixture[str]
) -> None:
    """Look first.

    The recovery procedure now starts with a command that cannot make anything
    worse - including on the agent queue, where a dry run needs no `--force`
    because it does nothing.
    """
    await _mixed_list(redis)

    assert (
        await replay(
            as_redis_client(wrapper),
            queue_name="agent",
            limit=10,
            force=False,
            dry_run=True,
        )
        == 0
    )

    assert _requeued(redis) == []
    output = capsys.readouterr().out
    assert "would re-queue 2 job(s)" in output
    assert "PROTECTED" in output


async def test_the_printed_summary_carries_no_customer_content(
    wrapper: RedisWrapper, redis: FakeQueueRedis, capsys: pytest.CaptureFixture[str]
) -> None:
    """It is printed into a terminal scrollback and a shell history.

    Identifiers, a category, a count and an age - the same rule the dead-letter
    record itself follows, restated where the output is produced.
    """
    await _mixed_list(redis)

    await replay(as_redis_client(wrapper), queue_name="agent", limit=10, force=False, dry_run=True)

    output = capsys.readouterr().out
    assert str(TENANT) in output
    assert "job-uncertain" in output
    # The payload bodies are identifiers only in production, but the summary
    # must not print them even so: the guarantee belongs here, not upstream.
    assert "conversation_id" not in output
