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
def redis():
    return FakeQueueRedis()


@pytest.fixture
def wrapper(redis):
    return RedisWrapper(redis)


async def dead_letter_one(redis, *, namespace: str, body: str) -> None:
    queue = ReliableQueue(redis, namespace=namespace)
    await queue.enqueue_body(body, now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    await queue.dead_letter(
        raw,
        DeadLetterRecord(
            queue=namespace,
            job_type=namespace.split(":")[0],
            tenant_id=str(TENANT),
            job_id=str(JOB),
            attempts=5,
            category=FailureCategory.PROVIDER_ERROR,
            enqueued_at=JobEnvelope.decode(raw).enqueued_at,
            first_attempted_at=NOW,
            last_attempted_at=NOW,
            dead_lettered_at=NOW,
            body=body,
        ),
    )


# ------------------------------------------------------------------ status


async def test_status_reports_every_queue(wrapper, capsys):
    await ReliableQueue(wrapper.client, namespace="agent:jobs").enqueue_body("{}", now=NOW)

    assert await status(wrapper) == 0

    out = capsys.readouterr().out
    for name in ("agent", "ingestion", "media"):
        assert name in out


# ------------------------------------------------------------ dead-letters


async def test_dead_letters_prints_the_record(wrapper, redis, capsys):
    await dead_letter_one(redis, namespace="knowledge:ingestion", body='{"document_id":"x"}')

    assert await dead_letters(wrapper, queue_name="ingestion", limit=10) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed["attempts"] == 5
    assert printed["category"] == "provider_error"
    assert printed["tenant_id"] == str(TENANT)


async def test_an_empty_dead_letter_list_says_so(wrapper, capsys):
    assert await dead_letters(wrapper, queue_name="agent", limit=10) == 0

    assert "no dead-lettered jobs" in capsys.readouterr().out


async def test_an_unknown_queue_is_refused(wrapper):
    assert await dead_letters(wrapper, queue_name="nonsense", limit=10) == 2


# ----------------------------------------------------------------- replay


async def test_replaying_an_idempotent_queue_requeues_the_job(wrapper, redis):
    await dead_letter_one(redis, namespace="knowledge:ingestion", body='{"document_id":"x"}')
    queue = ReliableQueue(redis, namespace="knowledge:ingestion")

    assert await replay(wrapper, queue_name="ingestion", limit=10, force=False) == 0

    assert await queue.depth() == 1
    (queued,) = redis.lists["knowledge:ingestion:pending"]
    assert JobEnvelope.decode(queued).body == '{"document_id":"x"}'


async def test_a_replayed_job_starts_its_attempt_count_again(wrapper, redis):
    """The budget was what said it was finished; an operator has overruled that."""
    await dead_letter_one(redis, namespace="media:understanding", body='{"media_id":"x"}')

    await replay(wrapper, queue_name="media", limit=10, force=False)

    (queued,) = redis.lists["media:understanding:pending"]
    assert JobEnvelope.decode(queued).attempt == 1


async def test_the_agent_queue_is_refused_without_force(wrapper, redis, capsys):
    """An agent turn ends in a message; a replay could send a second one."""
    await dead_letter_one(redis, namespace="agent:jobs", body='{"conversation_id":"x"}')

    assert await replay(wrapper, queue_name="agent", limit=10, force=False) == 3

    assert await ReliableQueue(redis, namespace="agent:jobs").depth() == 0
    assert "not idempotent" in capsys.readouterr().err


async def test_the_agent_queue_can_be_replayed_deliberately(wrapper, redis):
    await dead_letter_one(redis, namespace="agent:jobs", body='{"conversation_id":"x"}')

    assert await replay(wrapper, queue_name="agent", limit=10, force=True) == 0

    assert await ReliableQueue(redis, namespace="agent:jobs").depth() == 1


async def test_the_record_survives_the_replay(wrapper, redis):
    """Comparing the original with a second failure is how an operator learns."""
    await dead_letter_one(redis, namespace="knowledge:ingestion", body='{"document_id":"x"}')
    queue = ReliableQueue(redis, namespace="knowledge:ingestion")

    await replay(wrapper, queue_name="ingestion", limit=10, force=False)

    assert await queue.failed_depth() == 1


async def test_replaying_nothing_is_not_an_error(wrapper, capsys):
    assert await replay(wrapper, queue_name="ingestion", limit=10, force=False) == 0

    assert "nothing to replay" in capsys.readouterr().out


async def test_an_unreadable_record_is_skipped_rather_than_fatal(wrapper, redis, capsys):
    redis.lists["knowledge:ingestion:failed"] = ["not json", '{"no":"body"}']

    assert await replay(wrapper, queue_name="ingestion", limit=10, force=False) == 0

    assert await ReliableQueue(redis, namespace="knowledge:ingestion").depth() == 0
    assert "skipping" in capsys.readouterr().out


# ------------------------------------------------------------------ the CLI


def test_the_idempotent_queues_are_the_ones_their_workers_retry():
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
def test_the_parser_accepts_the_documented_invocations(argv):
    build_parser().parse_args(argv)


def test_the_parser_refuses_a_queue_that_does_not_exist():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["replay", "nonsense"])
