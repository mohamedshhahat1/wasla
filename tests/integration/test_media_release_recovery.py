"""A media release the queue refused is delayed, never lost.

The residual the Media remediation left: once a conversation's last attachment
became terminal, the only record that an agent turn was owed was a job pushed
to Redis *after* the commit. If Redis refused that push - or the process died
before it - the file was final, nothing was unresolved, and nothing anywhere
said a customer was waiting. The requeue path healed itself; the release path
did not.

The release now owes the turn durably, in the transaction that made the file
terminal (`AgentTurnRepository.owe`), and `MediaRecoveryWorker` republishes any
owed turn no agent worker has taken up. Everything here runs against a real
PostgreSQL, a real Redis and the production media worker, recovery sweep and
agent worker; only OpenAI and Meta are faked, and the refused enqueue is a real
connection to a Redis that is not there.

Time is moved by backdating rows, never by handing a worker a clock from the
future: an owed turn's lease is compared against the agent worker's own clock
when it adopts the turn, so a stamp written from a future `now` would be a
turn nobody may adopt yet - which is a property of the test, not of the system.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import select, text, update

from app.core.metrics import MetricsRegistry
from app.core.storage import LocalMediaStorage
from app.db.models.agent_turn import AgentTurn, AgentTurnState, TurnOutcome
from app.db.models.media import MAX_ATTEMPTS, MediaStatus, MessageMedia
from app.db.session import Database
from app.repositories.agent_turn_repository import MEDIA_RELEASE_HOLDER, OwedReleaseSweep
from app.services.media_horizons import claim_lease, release_horizon
from app.services.metrics_service import MetricsService
from app.workers import media_recovery as media_recovery_module
from app.workers.media_queue import MediaJob, MediaQueue
from app.workers.media_recovery import MediaRecoveryWorker
from app.workers.media_worker import MediaWorker
from app.workers.queue import AgentJob, AgentQueue, JobEnvelope
from tests.fakes import as_media_reader, as_whatsapp
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    Workspace,
    ai_providers,  # noqa: F401 - fixture
    ai_turns,  # noqa: F401 - fixture
)
from tests.media_harness import FakeRedis, StubReader, StubWhatsApp

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------ harness


class _Client:
    def __init__(self, client: Redis) -> None:
        self.client = client


@dataclass
class RefusingQueue:
    """An agent queue on a Redis that refuses, and a record that it was asked.

    The refusal is real - a connection to a port nothing listens on, or to a
    socket that never answers - so the failure is the one redis-py raises in
    production, not an exception a test invented. `attempts` and `errors` are
    what makes the test non-vacuous: a release that skipped the enqueue would
    leave both empty.
    """

    queue: AgentQueue
    attempts: int = 0
    errors: list[BaseException] = field(default_factory=list)

    async def enqueue(self, job: AgentJob) -> None:
        self.attempts += 1
        try:
            await self.queue.enqueue(job)
        except BaseException as error:
            self.errors.append(error)
            raise


async def _unreachable_redis() -> Redis:
    """A port with no listener: connection refused."""
    client: Redis = Redis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.5, socket_timeout=0.5, retry=None
    )
    return client


@pytest_asyncio.fixture
async def silent_redis() -> AsyncIterator[Redis]:
    """A socket that accepts and never answers: the command times out."""
    held: list[asyncio.StreamWriter] = []

    async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)
        await reader.read(-1)

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = Redis.from_url(
        f"redis://127.0.0.1:{port}/0", socket_connect_timeout=0.5, socket_timeout=0.3, retry=None
    )
    try:
        yield client
    finally:
        await client.aclose()
        for writer in held:
            writer.close()
        server.close()
        await server.wait_closed()


@dataclass
class Scene:
    runner: TurnRunner
    workspace: Workspace
    storage: LocalMediaStorage
    recorded: list[dict[str, int]] = field(default_factory=list)

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.workspace.tenant_id

    def queue(self) -> AgentQueue:
        return AgentQueue(
            self.runner.redis, namespace=self.runner.namespace, visibility_timeout_seconds=60
        )

    def recovery(
        self, *, agents: Any = None, database: Database | None = None
    ) -> MediaRecoveryWorker:
        """A fresh production recovery worker: nothing carried in memory."""
        built = MediaRecoveryWorker(
            database=database or self.runner.database,
            redis=_Client(self.runner.redis),  # type: ignore[arg-type]
            settings=self.runner.settings,
            storage=self.storage,
        )
        built._agents = agents if agents is not None else self.queue()
        built._media_queue = MediaQueue(
            self.runner.redis, namespace=f"{self.runner.namespace}:media"
        )
        return built

    async def conversation(self, text_: str = "Here is the photo") -> tuple[uuid.UUID, uuid.UUID]:
        return_value = await self.runner.write(self.workspace, [text_], wa_id=_wa_id())
        conversation_id, (message_id,) = return_value
        return conversation_id, message_id

    async def attach(
        self, conversation_id: uuid.UUID, message_id: uuid.UUID, **fields: Any
    ) -> uuid.UUID:
        async with self.runner.database.session() as session:
            media = MessageMedia(
                tenant_id=self.tenant_id,
                message_id=message_id,
                conversation_id=conversation_id,
                wa_media_id=f"media-{uuid.uuid4().hex[:10]}",
                mime_type="image/png",
                byte_size=0,
                is_voice=False,
                status=fields.pop("status", MediaStatus.PENDING),
                attempts=fields.pop("attempts", 0),
                **fields,
            )
            session.add(media)
            await session.flush()
            return media.id

    async def exhausted(self) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        """A conversation whose one file is stranded with no attempts left.

        The recovery sweep will give it up (`FAILED`) and release the turn.
        """
        conversation_id, message_id = await self.conversation()
        expired = datetime.now(UTC) - claim_lease(self.runner.settings) - timedelta(seconds=5)
        media_id = await self.attach(
            conversation_id,
            message_id,
            status=MediaStatus.DOWNLOADING,
            claim_id=uuid.uuid4(),
            claimed_at=expired,
            attempts=MAX_ATTEMPTS,
        )
        return conversation_id, message_id, media_id

    async def media(self, media_id: uuid.UUID) -> MessageMedia:
        async with self.runner.database.session() as session:
            row = await session.get(MessageMedia, media_id)
            assert row is not None
            return row

    async def turns(self) -> list[AgentTurn]:
        async with self.runner.database.session() as session:
            rows = await session.scalars(
                select(AgentTurn).where(AgentTurn.tenant_id == self.tenant_id)
            )
            return list(rows)

    async def age_obligations(self) -> None:
        """Move this workspace's owed turns past the release horizon."""
        past = datetime.now(UTC) - release_horizon(self.runner.settings) - timedelta(seconds=5)
        async with self.runner.database.session() as session:
            await session.execute(
                update(AgentTurn)
                .where(
                    AgentTurn.tenant_id == self.tenant_id,
                    AgentTurn.claimed_by == MEDIA_RELEASE_HOLDER,
                )
                .values(claim_expires_at=past, created_at=past)
            )

    async def queued(self) -> list[AgentJob]:
        """This workspace's jobs waiting on the agent queue."""
        raw: list[str] = await cast(
            "Awaitable[list[str]]",
            self.runner.redis.lrange(f"{self.runner.namespace}:pending", 0, -1),
        )
        jobs = [AgentJob.decode(JobEnvelope.decode(item).body) for item in raw]
        return [job for job in jobs if job.tenant_id == self.tenant_id]

    async def owed(self) -> int:
        """This workspace's owed turns, read through the production sweep's backlog."""
        async with self.runner.database.session() as session:
            count, _ = await OwedReleaseSweep(session).backlog(
                older_than=datetime.now(UTC) + timedelta(days=1), now=datetime.now(UTC)
            )
            mine = await session.scalar(
                select(text("count(*)"))
                .select_from(AgentTurn)
                .where(
                    AgentTurn.tenant_id == self.tenant_id,
                    AgentTurn.state == AgentTurnState.CLAIMED,
                    AgentTurn.claimed_by == MEDIA_RELEASE_HOLDER,
                )
            )
            assert count >= int(mine or 0)
            return int(mine or 0)

    async def unanswered(self) -> int:
        """The invariant, written from the schema rather than from the fix.

        Conversations of this workspace whose attachments are all terminal,
        none unresolved, and for which no agent turn at or after the newest
        attachment's message has been taken up by an agent worker (engaged or
        completed). Each is a customer owed a reply nobody is giving.
        """
        async with self.runner.database.session() as session:
            found = await session.scalar(
                text("""
                    SELECT count(*) FROM (
                      SELECT m.conversation_id, max(msg.created_at) AS newest
                      FROM message_media m
                      JOIN messages msg ON msg.id = m.message_id
                      WHERE m.tenant_id = :tenant
                      GROUP BY m.conversation_id
                      HAVING bool_and(m.status IN ('ready', 'skipped', 'failed'))
                    ) settled
                    WHERE NOT EXISTS (
                      SELECT 1 FROM agent_turns t
                      JOIN messages trig ON trig.id = t.trigger_message_id
                      WHERE t.tenant_id = :tenant
                        AND t.conversation_id = settled.conversation_id
                        AND t.state IN ('engaged', 'completed')
                        AND trig.created_at >= settled.newest
                    )
                    """),
                {"tenant": self.tenant_id},
            )
            return int(found or 0)


def _wa_id() -> str:
    return f"2015{uuid.uuid4().int % 10**8:08d}"


@pytest_asyncio.fixture
async def scene(
    ai_turns: TurnRunner,  # noqa: F811
    ai_providers: FakeProviders,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Scene]:
    try:
        await ai_turns.redis.ping()
    except Exception:  # pragma: no cover - Redis is not running
        pytest.skip("No Redis reachable.")
    workspace = await ai_turns.workspace()
    built = Scene(runner=ai_turns, workspace=workspace, storage=LocalMediaStorage(tmp_path))

    async def record(**counts: int) -> None:
        built.recorded.append(counts)

    monkeypatch.setattr(media_recovery_module, "record_media_recovery", record)
    yield built


def _total(scene: Scene, outcome: str) -> int:
    return sum(pass_.get(outcome, 0) for pass_ in scene.recorded)


# ------------------------------------------------------- the failure itself


@pytest.mark.parametrize("failure", ["connection_refused", "timeout"])
async def test_a_refused_release_is_owed_durably_and_answered_once_redis_returns(
    scene: Scene,
    ai_providers: FakeProviders,  # noqa: F811
    silent_redis: Redis,
    failure: str,
) -> None:
    """The residual, reproduced and closed.

    The recovery sweep gives a stranded file up and releases its conversation;
    Redis refuses the agent job at exactly that point. The file stays final,
    nobody is answered yet, the turn is owed in PostgreSQL, and once Redis is
    back one recovery pass republishes it: one turn, one reply, however many
    passes follow."""
    conversation_id, message_id, media_id = await scene.exhausted()

    down = await _unreachable_redis() if failure == "connection_refused" else silent_redis
    refusing = RefusingQueue(AgentQueue(down, namespace=scene.runner.namespace))
    try:
        await scene.recovery(agents=refusing).run_once()
    finally:
        if failure == "connection_refused":
            await down.aclose()

    # The enqueue was really attempted, and really failed the way Redis fails.
    assert refusing.attempts == 1
    assert len(refusing.errors) == 1
    # Windows can report an unreachable loopback port as a connect timeout.
    # Both errors exercise the same failed-enqueue recovery path.
    expected = (
        (RedisConnectionError, RedisTimeoutError)
        if failure == "connection_refused"
        else RedisTimeoutError
    )
    assert isinstance(refusing.errors[0], expected), refusing.errors[0]
    assert isinstance(refusing.errors[0], RedisError)
    assert _total(scene, "release_failed") == 1
    assert _total(scene, "release_recovered") == 0

    # The file's outcome committed and is final.
    row = await scene.media(media_id)
    assert row.status is MediaStatus.FAILED
    assert row.claim_id is None
    # Nobody has been answered, and nothing is queued.
    assert ai_providers.sends == []
    assert await scene.runner.outbound(scene.tenant_id) == []
    assert await scene.queued() == []
    # The obligation is durable: one turn, owed, keyed on the message.
    turns = await scene.turns()
    assert [(t.state, t.claimed_by, t.trigger_message_id) for t in turns] == [
        (AgentTurnState.CLAIMED, MEDIA_RELEASE_HOLDER, message_id)
    ]
    assert await scene.owed() == 1
    assert await scene.unanswered() == 1

    # A pass inside the horizon leaves it alone: its job may merely be queued.
    await scene.recovery().run_once()
    assert await scene.queued() == []
    assert (await scene.media(media_id)).status is MediaStatus.FAILED

    # Redis is back and the horizon has passed. One pass republishes it once.
    await scene.age_obligations()
    await scene.recovery().run_once()
    queued = await scene.queued()
    assert queued == [
        AgentJob(
            tenant_id=scene.tenant_id,
            conversation_id=conversation_id,
            trigger_message_id=message_id,
        )
    ]
    assert _total(scene, "release_recovered") == 1

    # The republished job is the turn: processed once, replied once.
    assert await scene.runner.drain() == 1
    assert len(ai_providers.sends) == 1
    assert len(await scene.runner.outbound(scene.tenant_id)) == 1
    (turn,) = await scene.turns()
    assert turn.state is AgentTurnState.COMPLETED
    assert turn.outcome is TurnOutcome.REPLIED
    assert turn.claimed_by != MEDIA_RELEASE_HOLDER
    assert await scene.owed() == 0
    assert await scene.unanswered() == 0

    # Recovery again, and again, however late: nothing more is owed.
    await scene.age_obligations()
    for _ in range(3):
        await scene.recovery().run_once()
    assert await scene.queued() == []
    assert await scene.runner.drain() == 0
    assert len(ai_providers.sends) == 1
    assert len(await scene.turns()) == 1
    assert (await scene.media(media_id)).status is MediaStatus.FAILED


async def test_a_republished_release_whose_original_also_arrives_is_still_one_reply(
    scene: Scene,
    ai_providers: FakeProviders,  # noqa: F811
) -> None:
    """The republish is at-least-once, so it may race an original that was
    only slow. Both envelopes name one trigger, which is one turn."""
    conversation_id, message_id, _ = await scene.exhausted()
    await scene.recovery().run_once()
    assert len(await scene.queued()) == 1

    # The original is still queued; the sweep republishes as if it were lost.
    await scene.age_obligations()
    await scene.recovery().run_once()
    assert len(await scene.queued()) == 2

    assert await scene.runner.race(2) >= 1
    await scene.runner.drain()
    assert len(ai_providers.sends) == 1
    (turn,) = await scene.turns()
    assert turn.state is AgentTurnState.COMPLETED
    assert turn.trigger_message_id == message_id
    assert turn.conversation_id == conversation_id


# ------------------------------------------------------------ crash window


class _Crash(BaseException):
    """A process dying: not an `Exception`, so nothing on the way out catches it."""


async def test_a_worker_that_dies_between_its_commit_and_its_enqueue_is_recovered(
    scene: Scene,
    ai_providers: FakeProviders,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The media worker reads the file, commits it `READY` with the turn owed,
    and dies before the agent job reaches Redis. No flag in memory survives;
    a fresh recovery worker republishes the turn and the customer is answered
    once."""
    conversation_id, message_id = await scene.conversation()
    media_id = await scene.attach(conversation_id, message_id)
    reader = StubReader()
    worker = MediaWorker(
        database=scene.runner.database,
        redis=FakeRedis(),  # type: ignore[arg-type]
        settings=scene.runner.settings,
        storage=scene.storage,
        whatsapp_factory=lambda http: as_whatsapp(StubWhatsApp()),
        reader_factory=lambda http: as_media_reader(reader),
    )

    # `_handle` commits and returns the job `_attempt` would enqueue next;
    # dropping it here is the process dying at that line.
    released = await worker._handle(MediaJob(tenant_id=scene.tenant_id, media_id=media_id))
    assert released is not None
    del worker

    assert (await scene.media(media_id)).status is MediaStatus.READY
    assert reader.reads == 1
    assert await scene.queued() == []
    assert ai_providers.sends == []
    assert await scene.owed() == 1

    await scene.age_obligations()
    await scene.recovery().run_once()
    assert len(await scene.queued()) == 1
    assert await scene.runner.drain() == 1
    assert len(ai_providers.sends) == 1
    assert await scene.unanswered() == 0
    assert reader.reads == 1


async def test_a_recovery_pass_that_dies_before_publishing_is_finished_by_the_next(
    scene: Scene,
    ai_providers: FakeProviders,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both of the recovery worker's own publish points: the release it made,
    and the republish of an owed turn. Each commits first; dying after the
    commit leaves the turn owed, and a later pass - by a new process - ends it
    with one reply."""
    _, _, media_id = await scene.exhausted()

    async def die(self: MediaRecoveryWorker, job: AgentJob) -> bool:
        raise _Crash()

    # Scoped, so undoing it cannot also undo the fake providers.
    with monkeypatch.context() as crashing:
        crashing.setattr(MediaRecoveryWorker, "_publish", die)
        with pytest.raises(_Crash):
            await scene.recovery().run_once()
        assert (await scene.media(media_id)).status is MediaStatus.FAILED
        assert await scene.owed() == 1

        # Now the republish itself dies after stamping.
        await scene.age_obligations()
        with pytest.raises(_Crash):
            await scene.recovery().run_once()
        assert await scene.queued() == []
        assert await scene.owed() == 1

    await scene.age_obligations()
    await scene.recovery().run_once()
    assert await scene.runner.drain() == 1
    assert len(ai_providers.sends) == 1
    assert await scene.owed() == 0
    assert await scene.unanswered() == 0


# ------------------------------------------------------------- concurrency


async def test_two_recovery_workers_racing_one_owed_release_publish_it_once(
    scene: Scene,
    ai_providers: FakeProviders,  # noqa: F811
) -> None:
    """Two sweeps, each on a connection pool of its own, reach the same owed
    turn at once. Exactly one publishes it; a later pass inside the horizon
    publishes nothing; and the turn is answered once."""
    await scene.exhausted()
    down = await _unreachable_redis()
    try:
        await scene.recovery(agents=AgentQueue(down, namespace=scene.runner.namespace)).run_once()
    finally:
        await down.aclose()
    assert await scene.owed() == 1
    await scene.age_obligations()

    pools = [Database(scene.runner.settings) for _ in range(2)]
    try:
        await asyncio.gather(*(scene.recovery(database=pool).run_once() for pool in pools))
    finally:
        for pool in pools:
            await pool.dispose()
    assert len(await scene.queued()) == 1

    # Stamped on publish, so the next pass does not publish it again.
    await scene.recovery().run_once()
    assert len(await scene.queued()) == 1

    assert await scene.runner.race(2) == 1
    assert len(ai_providers.sends) == 1
    (turn,) = await scene.turns()
    assert turn.state is AgentTurnState.COMPLETED
    assert await scene.unanswered() == 0


# ---------------------------------------------------------------- invariant


async def test_no_settled_conversation_is_left_owed_once_recovery_has_run(
    scene: Scene,
    ai_providers: FakeProviders,  # noqa: F811
) -> None:
    """The sweep invariant, presence first.

    Three conversations whose release Redis refused - the population the
    invariant measures - plus one released normally. Before recovery the
    invariant counts the three; after Redis returns and recovery and the agent
    worker have run, it counts none, and every one was answered once."""
    for _ in range(3):
        await scene.exhausted()
    down = await _unreachable_redis()
    refusing = RefusingQueue(AgentQueue(down, namespace=scene.runner.namespace))
    try:
        await scene.recovery(agents=refusing).run_once()
    finally:
        await down.aclose()
    assert refusing.attempts == 3
    assert len(refusing.errors) == 3

    await scene.exhausted()
    await scene.recovery().run_once()

    # Presence: the invariant sees all four settled, and the three refused
    # ones as owed and unanswered. The fourth is queued but not yet answered.
    assert await scene.owed() == 4
    assert await scene.unanswered() == 4
    await scene.runner.drain()
    assert await scene.unanswered() == 3
    assert await scene.owed() == 3

    await scene.age_obligations()
    await scene.recovery().run_once()
    await scene.runner.drain()

    assert await scene.owed() == 0
    assert await scene.unanswered() == 0
    assert len(ai_providers.sends) == 4
    turns = await scene.turns()
    assert len(turns) == 4
    assert {turn.state for turn in turns} == {AgentTurnState.COMPLETED}
    async with scene.runner.database.session() as session:
        statuses = await session.scalars(
            select(MessageMedia.status).where(MessageMedia.tenant_id == scene.tenant_id)
        )
        assert set(statuses) == {MediaStatus.FAILED}


# ------------------------------------------------------------ observability


async def test_an_owed_release_nobody_takes_up_is_visible_and_clears_once_answered(
    scene: Scene,
    ai_providers: FakeProviders,  # noqa: F811
) -> None:
    """`wasla_media_release_owed` counts owed turns past the horizon - the
    reading `MediaReleaseOwed` alerts on - and is measured from the release,
    so a sweep republishing into a Redis that still refuses does not reset
    it. Once the turn is answered it reads zero."""
    await scene.exhausted()
    for _ in range(2):
        down = await _unreachable_redis()
        try:
            await scene.age_obligations()
            await scene.recovery(
                agents=AgentQueue(down, namespace=scene.runner.namespace)
            ).run_once()
        finally:
            await down.aclose()
    # Both the release and a republish were refused and counted.
    assert _total(scene, "release_failed") == 2
    assert _total(scene, "release_recovered") == 0

    exposition = await MetricsService(
        None,
        registry=MetricsRegistry(),
        database=scene.runner.database,
        settings=scene.runner.settings,
    ).render()
    owed = _gauge(exposition, "wasla_media_release_owed")
    age = _gauge(exposition, "wasla_media_release_owed_oldest_age_seconds")
    assert owed is not None and owed >= 1.0
    assert age is not None and age > release_horizon(scene.runner.settings).total_seconds()

    await scene.age_obligations()
    await scene.recovery().run_once()
    await scene.runner.drain()
    assert len(ai_providers.sends) == 1
    assert await scene.owed() == 0


def _gauge(exposition: str, name: str) -> float | None:
    for line in exposition.splitlines():
        if line.startswith((f"{name} ", f"{name}{{}} ")):
            return float(line.rsplit(" ", 1)[1])
    return None
