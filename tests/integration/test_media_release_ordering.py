"""When an agent turn may exist, relative to the transcript it will read.

The media worker used to queue the agent turn from *inside* the transaction
that wrote the transcript, and `Database.session` commits after the handler
returns. A worker blocked on `BLMOVE` could therefore take the job before the
transcript was visible to anybody else.

Reproduced against this same PostgreSQL and this same Redis before anything was
changed, with the media worker's transaction held open across the observation
rather than raced against it:

    media worker transaction         : OPEN, uncommitted
    agent jobs already queued        : 1
    conversation visible to the agent: 1 row
    transcript visible to the agent  : None
    agent context it would build     : How much for this?
                                       [image, not yet read]

    media worker transaction         : COMMITTED
    agent context it would build     : How much for this?
                                       [image] A blue three-seat sofa ...

    RESULT: the agent turn was released while the transcript was invisible.

Unlike ADR-089's race the conversation exists, so nothing answers `not_found`,
nothing retries, and nothing is dead-lettered. The turn simply answers a
photograph it has not seen — which is why a retry could not have fixed it and
the ordering had to.

**Barriers, not sleeps.** The transaction is held open explicitly at the point
the decision is made, so the ordering asserted here is the one the test
arranges.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.memory import build_window
from app.core.config import Settings
from app.core.exceptions import ExternalServiceError
from app.core.storage import LocalMediaStorage
from app.db.models.agent import Agent, AgentStatus
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.integrations.openai.client import ResponsesClient
from app.integrations.whatsapp.client import DownloadedMedia, MediaDescriptor
from app.repositories.conversation_repository import MessageRepository
from app.repositories.media_repository import MediaRepository
from app.services.media_reader import ReadResult, SilentRecordingError
from app.services.messaging_service import MessagingService
from app.workers.ai_worker import AgentWorker
from app.workers.dispatch import SUCCEEDED
from app.workers.media_queue import MediaJob, MediaQueue
from app.workers.media_worker import MediaWorker
from app.workers.queue import AgentJob, AgentQueue, JobEnvelope
from app.workers.retry import IDEMPOTENT_RETRY
from tests.fakes import as_media_reader, as_whatsapp

pytestmark = pytest.mark.integration

# A database of this file's own, so a run cannot disturb whatever else is using
# this Redis.
REDIS_URL = "redis://localhost:6379/14"

PIXEL = b"\x89PNG\r\n\x1a\n" + b"0" * 64
TRANSCRIPT = "A blue three-seat sofa with a wooden frame."

# Far past any backoff the media policy can produce, so promoting a retry is a
# decision rather than a race with the clock.
WELL_PAST_THE_BACKOFF = timedelta(seconds=300)

# The reaper schedules the retry from *its* clock, so promoting it needs one
# later still. Two steps rather than one large number, so which clock is being
# moved stays legible.
AFTER_THE_REQUEUE = 2 * WELL_PAST_THE_BACKOFF


class _RedisClient:
    """What a worker asks a `RedisClient` for, and nothing else."""

    def __init__(self, client: Redis) -> None:
        self.client = client


class StubWhatsApp:
    """Answers the two calls the download path makes, without a network."""

    def __init__(self) -> None:
        self.fetched = 0

    async def probe_media(self, media_id: str) -> MediaDescriptor:
        return MediaDescriptor(mime_type="image/png", byte_size=len(PIXEL))

    async def fetch_media(self, media_id: str, *, max_bytes: int) -> DownloadedMedia:
        self.fetched += 1
        return DownloadedMedia(
            content=PIXEL,
            mime_type="image/png",
            byte_size=len(PIXEL),
            declared_size=len(PIXEL),
            sha256=None,
        )


class StubReader:
    """A fixed transcript, or a refusal, without touching a provider."""

    def __init__(self, transcript: str = TRANSCRIPT, *, error: Exception | None = None) -> None:
        self._transcript = transcript
        self._error = error
        self.reads = 0

    async def read(self, *, content: bytes, mime_type: str | None) -> ReadResult:
        self.reads += 1
        if self._error is not None:
            raise self._error
        return ReadResult(transcript=self._transcript, method="vision")


class HeldWorker(MediaWorker):
    """Stops with its transaction open, at the moment the decision is made.

    Overriding `_release_conversation` rather than patching the session is what
    puts the barrier exactly where the question is: everything the transaction
    will write has been written, the gate is held, and nothing has committed.
    """

    def __init__(self, *arguments: Any, **keywords: Any) -> None:
        super().__init__(*arguments, **keywords)
        self.at_the_boundary = asyncio.Event()
        self.may_commit = asyncio.Event()

    async def _release_conversation(
        self,
        *,
        session: AsyncSession,
        job: MediaJob,
        media: MessageMedia,
    ) -> AgentJob | None:
        decided = await super()._release_conversation(session=session, job=job, media=media)
        self.at_the_boundary.set()
        await self.may_commit.wait()
        return decided


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        redis_url=REDIS_URL,
        jwt_secret="media-ordering-secret-not-for-deployment",
        openai_api_key="sk-no-request-is-ever-made-from-this-file",
    )


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover - Redis is not running
        await client.aclose()
        pytest.skip("No Redis reachable; these need one to observe a real queue.")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, on a pool of their own."""
    engine = create_async_engine(prepared_database, pool_size=8, max_overflow=4)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    """The worker's own pool, so its transaction is not the observer's."""
    instance = Database(_settings(prepared_database))
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest_asyncio.fixture
async def workspace(
    committing: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A committed conversation, exactly as the webhook leaves one."""
    async with committing() as session:
        tenant = Tenant(name="Ordering", slug=f"ordering-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        account = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
            waba_id="555000111",
            display_phone_number="+201000000000",
        )
        contact = Contact(tenant_id=tenant.id, wa_id=f"2012{uuid.uuid4().int % 10**8:08d}")
        agent = Agent(
            tenant_id=tenant.id,
            name="Sales",
            model="gpt-4.1-mini",
            system_prompt="You answer questions about furniture.",
            status=AgentStatus.ACTIVE,
            is_default=True,
        )
        session.add_all([account, contact, agent])
        await session.flush()

        conversation = Conversation(
            tenant_id=tenant.id,
            contact_id=contact.id,
            account_id=account.id,
        )
        session.add(conversation)
        await session.commit()
        created = (tenant.id, conversation.id)

    try:
        yield created
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == created[0]))
            await session.commit()


async def _attachment(
    committing: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    caption: str = "How much for this?",
) -> uuid.UUID:
    """One inbound photograph, committed, waiting to be read."""
    async with committing() as session:
        message = Message(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            direction=MessageDirection.INBOUND,
            kind=MessageKind.IMAGE,
            status=MessageStatus.RECEIVED,
            body=caption,
        )
        session.add(message)
        await session.flush()

        media = MessageMedia(
            tenant_id=tenant_id,
            message_id=message.id,
            conversation_id=conversation_id,
            wa_media_id=f"wamid-{uuid.uuid4().hex[:10]}",
            mime_type="image/png",
            status=MediaStatus.PENDING,
        )
        session.add(media)
        await session.commit()
        return media.id


def _worker(
    database: Database,
    redis: Redis,
    tmp_path: Path,
    *,
    reader: StubReader | None = None,
    kind: type[MediaWorker] = MediaWorker,
) -> MediaWorker:
    return kind(
        database=database,
        redis=_RedisClient(redis),  # type: ignore[arg-type]
        settings=_settings(""),
        storage=LocalMediaStorage(tmp_path),
        whatsapp_factory=lambda http: as_whatsapp(StubWhatsApp()),
        reader_factory=lambda http: as_media_reader(reader or StubReader()),
    )


async def _agent_reads(
    committing: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> str:
    """The context an agent turn starting now would build, in its own session."""
    async with committing() as session:
        messages = await MessageRepository(session, tenant_id=tenant_id).list_for_conversation(
            conversation_id=conversation_id,
            limit=20,
        )
        media = await MediaRepository(session, tenant_id=tenant_id).map_for_messages(
            [message.id for message in messages]
        )
        window = build_window(messages, media=media, message_limit=20, token_budget=4000)
        return "\n".join(turn.text for turn in window.turns)


async def _transcripts(
    committing: async_sessionmaker[AsyncSession],
    conversation_id: uuid.UUID,
) -> list[str | None]:
    async with committing() as session:
        rows = await session.execute(
            select(MessageMedia.transcript).where(
                MessageMedia.conversation_id == conversation_id,
            )
        )
        return list(rows.scalars().all())


# ------------------------------------------------------------------ ordering


async def test_the_agent_turn_is_not_queued_before_the_transcript_commits(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """The finding itself: no job may name a transcript nobody else can read."""
    tenant_id, conversation_id = workspace
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)

    worker = _worker(database, redis, tmp_path, kind=HeldWorker)
    assert isinstance(worker, HeldWorker)
    await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))

    running = asyncio.create_task(worker.run_once(wait_seconds=1))
    try:
        await asyncio.wait_for(worker.at_the_boundary.wait(), timeout=30)

        agents = AgentQueue(redis)
        assert await agents.depth() == 0, "an agent turn was queued from inside the transaction"
        assert await _transcripts(committing, conversation_id) == [None]
    finally:
        worker.may_commit.set()
        await running

    assert await _transcripts(committing, conversation_id) == [TRANSCRIPT]
    assert await AgentQueue(redis).depth() == 1


async def test_the_agent_context_carries_the_committed_transcript(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """What the turn actually reads, not merely that a row exists.

    The degraded answer is the harm: `[image, not yet read]` is a valid,
    committed, entirely wrong context, and no retry policy can tell it from a
    good one.
    """
    tenant_id, conversation_id = workspace
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)

    worker = _worker(database, redis, tmp_path)
    await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))
    assert await worker.run_once(wait_seconds=1) is True

    context = await _agent_reads(committing, tenant_id=tenant_id, conversation_id=conversation_id)
    assert TRANSCRIPT in context
    assert "not yet read" not in context


async def test_a_crash_after_the_commit_does_not_lose_the_agent_turn(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """The window the new order opens, and what closes it.

    Committing the transcript before queueing the turn means a process that
    stops in between has read a file nobody will answer. Nothing new was built
    for that: the media job is still reserved, so the lease expires, the reaper
    requeues it, and the retry finds everything resolved and queues the turn.
    """
    tenant_id, conversation_id = workspace
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)

    worker = _worker(database, redis, tmp_path)
    await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))

    raw = await worker.queue.reserve(wait_seconds=1)
    assert raw is not None
    # The worker's own transaction, run to completion and committed - and then
    # the process stops, before the release and the enqueue it owed.
    assert await worker._handle(MediaJob(tenant_id=tenant_id, media_id=media_id)) is not None
    assert await _transcripts(committing, conversation_id) == [TRANSCRIPT]
    assert await AgentQueue(redis).depth() == 0

    recovered = await worker.queue.recover_expired(
        policy=IDEMPOTENT_RETRY,
        now=datetime.now(UTC) + WELL_PAST_THE_BACKOFF,
    )
    assert [outcome.action for outcome in recovered] == ["requeued"]
    assert await worker.queue.promote_due(now=datetime.now(UTC) + AFTER_THE_REQUEUE) == 1

    replacement = _worker(database, redis, tmp_path)
    assert await replacement.run_once(wait_seconds=1) is True

    assert await AgentQueue(redis).depth() == 1
    assert await worker.queue.failed_depth() == 0


async def test_two_attachments_queue_one_agent_turn(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """The gate still decides, now that the queueing happens outside it.

    Only the worker that finishes last can see nothing outstanding, because a
    sibling cannot commit its own row without waiting at the gate first - so
    exactly one caller is handed a turn even though both queue after
    committing.
    """
    tenant_id, conversation_id = workspace
    first = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)
    second = await _attachment(
        committing,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        caption="And this one?",
    )

    worker = _worker(database, redis, tmp_path)
    await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=first))
    await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=second))

    assert await worker.run_once(wait_seconds=1) is True
    assert await AgentQueue(redis).depth() == 0

    assert await worker.run_once(wait_seconds=1) is True
    assert await AgentQueue(redis).depth() == 1

    context = await _agent_reads(committing, tenant_id=tenant_id, conversation_id=conversation_id)
    assert context.count(TRANSCRIPT) == 2


async def test_a_reaped_media_worker_does_not_queue_a_second_agent_turn(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """Two live workers on one file, which is what a reclaimed lease produces.

    The slow worker finishes after a reaper has already given its job to
    somebody else. Its release finds nothing to remove - `_claim_inflight` can
    succeed for one caller only - so it declines to queue a turn the
    replacement is about to queue itself.
    """
    tenant_id, conversation_id = workspace
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)

    slow = _worker(database, redis, tmp_path)
    await slow.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))

    raw = await slow.queue.reserve(wait_seconds=1)
    assert raw is not None

    # The reaper decides this worker is gone and hands the job on.
    recovered = await slow.queue.recover_expired(
        policy=IDEMPOTENT_RETRY,
        now=datetime.now(UTC) + WELL_PAST_THE_BACKOFF,
    )
    assert [outcome.action for outcome in recovered] == ["requeued"]
    await slow.queue.promote_due(now=datetime.now(UTC) + AFTER_THE_REQUEUE)

    # The replacement runs to completion and queues the one turn.
    replacement = _worker(database, redis, tmp_path)
    assert await replacement.run_once(wait_seconds=1) is True
    assert await AgentQueue(redis).depth() == 1

    # Only now does the original finish, through its own attempt path rather
    # than a hand-driven one, because the branch under test is in `_attempt`.
    assert await slow._attempt(raw, JobEnvelope.decode(raw)) == SUCCEEDED
    assert await AgentQueue(redis).depth() == 1


async def test_a_file_that_cannot_be_read_still_queues_the_turn(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """Unchanged: a customer is owed an answer even about an unreadable file.

    `SKIPPED` and `FAILED` both count as resolved, so the conversation is
    released and the agent says it could not open the photograph rather than
    pretending none arrived.
    """
    tenant_id, conversation_id = workspace
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)

    reader = StubReader(error=SilentRecordingError())
    worker = _worker(database, redis, tmp_path, reader=reader)
    await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))

    assert await worker.run_once(wait_seconds=1) is True
    assert await AgentQueue(redis).depth() == 1
    assert await _transcripts(committing, conversation_id) == [None]

    context = await _agent_reads(committing, tenant_id=tenant_id, conversation_id=conversation_id)
    assert "unreadable" in context


@pytest.mark.parametrize(
    ("mode", "reaches_the_provider"),
    [(ConversationMode.HUMAN, False), (ConversationMode.AI, True)],
)
async def test_a_human_takeover_while_a_file_is_read_stops_the_reply(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: ConversationMode,
    reaches_the_provider: bool,
) -> None:
    """Releasing a conversation asks for a turn; it does not authorise one.

    A colleague who takes over while the photograph is being read must not be
    answered over. Queueing outside the transaction must not turn that refusal
    into a race, so the turn is queued either way and the orchestrator decides
    - which is why both modes are driven here: an assertion that nothing was
    sent means nothing unless the same setup sends when it should.
    """
    tenant_id, conversation_id = workspace
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)

    worker = _worker(database, redis, tmp_path)
    await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))

    async with committing() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.mode = mode
        await session.commit()

    assert await worker.run_once(wait_seconds=1) is True
    assert await AgentQueue(redis).depth() == 1

    asked: list[object] = []
    sent: list[str] = []

    async def respond(self: object, **keywords: object) -> object:
        asked.append(keywords)
        raise ExternalServiceError("no provider is reachable from this file")

    async def send(self: object, *, conversation_id: uuid.UUID, body: str) -> None:
        sent.append(body)

    monkeypatch.setattr(ResponsesClient, "respond", respond)
    monkeypatch.setattr(MessagingService, "send_text", send)

    agents = AgentWorker(
        database=database,
        redis=_RedisClient(redis),  # type: ignore[arg-type]
        settings=_settings(""),
    )
    assert await agents.run_once(wait_seconds=1) is True

    assert bool(asked) is reaches_the_provider
    assert sent == []


async def test_the_media_queue_is_the_one_this_worker_reserves_from(
    redis: Redis,
    database: Database,
    tmp_path: Path,
) -> None:
    """A guard on the fixture, not on the product.

    Every assertion above about "the agent queue is empty" is worthless if the
    two queues happened to share a namespace.
    """
    worker = _worker(database, redis, tmp_path)
    assert isinstance(worker.queue, MediaQueue)
    assert worker.queue.namespace != AgentQueue(redis).namespace
