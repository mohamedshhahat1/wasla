"""What an attempt at a file holds while it waits, and what finishes it if it
never comes back (MEDIA-03, MEDIA-11).

Real commits against a real PostgreSQL, a real Redis queue and the production
worker, recovery sweep and release gate. The suite's rollback fixture cannot be
used here: "the claim is visible to another attempt" and "no connection is held
while Meta is asked" are both statements about what has *committed*.

**Why the pool is one connection.** A generous pool proves nothing about
MEDIA-11: a download holding its connection would still leave others free. With
exactly one, the probe below can only open a session of its own while Meta is
being asked if the worker gave its connection back first - so the observation
is structural rather than a timing threshold.
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
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import QueuePool

from app.core.config import Settings
from app.core.storage import LocalMediaStorage
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.media import MAX_ATTEMPTS, MediaStatus, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import (
    WhatsAppAccount,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)
from app.db.session import Database
from app.integrations.whatsapp.client import DownloadedMedia, MediaDescriptor
from app.services.media_horizons import claim_lease, unclaimed_horizon
from app.services.media_outcomes import MediaReason, text_for
from app.services.media_reader import ReadResult
from app.services.media_service import MediaService
from app.workers import media_worker as media_worker_module
from app.workers.media_queue import MediaJob, MediaQueue
from app.workers.media_recovery import MediaRecoveryWorker
from app.workers.media_worker import MediaWorker
from app.workers.queue import AgentQueue
from app.workers.retry import RetryPolicy
from tests.fakes import as_media_reader, as_whatsapp

pytestmark = pytest.mark.integration

REDIS_URL = "redis://localhost:6379/9"
PIXEL = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class _RedisClient:
    def __init__(self, client: Redis) -> None:
        self.client = client


def _settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "database_url": database_url,
        "redis_url": REDIS_URL,
        "jwt_secret": "media-recovery-secret-not-for-deployment",
        "database_pool_size": 1,
        "database_max_overflow": 0,
        "database_pool_timeout": 5,
    }
    values.update(overrides)
    return Settings(**values)


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover - Redis is not running
        await client.aclose()
        pytest.skip("No Redis reachable.")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, pool_size=8, max_overflow=4)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def one_connection(prepared_database: str) -> AsyncIterator[Database]:
    database = Database(_settings(prepared_database))
    try:
        yield database
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def workspace(
    committing: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    async with committing() as session:
        tenant = Tenant(name="Recovery", slug=f"recovery-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        account = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
            waba_id="555000111",
            display_phone_number="+201000000000",
        )
        session.add(account)
        await session.flush()
        created = (tenant.id, account.id)
        await session.commit()
    try:
        yield created
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == created[0]))
            await session.commit()


async def _conversation(
    committing: async_sessionmaker[AsyncSession], workspace: tuple[uuid.UUID, uuid.UUID]
) -> uuid.UUID:
    tenant_id, account_id = workspace
    async with committing() as session:
        contact = Contact(tenant_id=tenant_id, wa_id=f"2012{uuid.uuid4().int % 10**8:08d}")
        session.add(contact)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant_id, contact_id=contact.id, account_id=account_id
        )
        session.add(conversation)
        await session.commit()
        return conversation.id


async def _attachment(
    committing: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    **fields: Any,
) -> uuid.UUID:
    async with committing() as session:
        message = Message(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            wa_message_id=f"wamid.{uuid.uuid4().hex}",
            direction=MessageDirection.INBOUND,
            kind=MessageKind.IMAGE,
            status=MessageStatus.RECEIVED,
            origin=MessageOrigin.CUSTOMER,
        )
        session.add(message)
        await session.flush()
        media = MessageMedia(
            tenant_id=tenant_id,
            message_id=message.id,
            conversation_id=conversation_id,
            wa_media_id=f"media-{uuid.uuid4().hex[:10]}",
            mime_type="image/png",
            status=fields.pop("status", MediaStatus.PENDING),
            **fields,
        )
        session.add(media)
        await session.commit()
        return media.id


async def _row(committing: async_sessionmaker[AsyncSession], media_id: uuid.UUID) -> MessageMedia:
    async with committing() as session:
        row = await session.get(MessageMedia, media_id)
        assert row is not None
        return row


class Stub:
    """Meta and the reader, with hooks that run while each is being asked."""

    def __init__(self) -> None:
        self.probes = 0
        self.fetches = 0
        self.reads = 0
        self.during_probe: Any = None
        self.during_fetch: Any = None
        self.during_read: Any = None

    async def probe_media(self, media_id: str) -> MediaDescriptor:
        self.probes += 1
        if self.during_probe is not None:
            await self.during_probe()
        return MediaDescriptor(mime_type="image/png", byte_size=len(PIXEL))

    async def fetch_media(self, media_id: str, *, max_bytes: int) -> DownloadedMedia:
        self.fetches += 1
        if self.during_fetch is not None:
            await self.during_fetch()
        return DownloadedMedia(
            content=PIXEL,
            mime_type="image/png",
            byte_size=len(PIXEL),
            declared_size=len(PIXEL),
            sha256=None,
        )

    async def read(self, *, content: bytes, mime_type: str | None) -> ReadResult:
        self.reads += 1
        if self.during_read is not None:
            await self.during_read()
        return ReadResult(transcript="A blue sofa.", method="vision")


def _worker(database: Database, redis: Redis, tmp_path: Path, stub: Stub) -> MediaWorker:
    return MediaWorker(
        database=database,
        redis=_RedisClient(redis),  # type: ignore[arg-type]
        settings=_settings("unused"),
        storage=LocalMediaStorage(tmp_path),
        whatsapp_factory=lambda http: as_whatsapp(stub),
        reader_factory=lambda http: as_media_reader(stub),
    )


def _pool(database: Database) -> QueuePool:
    pool = database.engine.pool
    assert isinstance(pool, QueuePool)
    return pool


# ------------------------------------------------ nothing held across the wire


async def test_no_connection_is_held_while_meta_or_the_reader_is_asked(
    redis: Redis,
    one_connection: Database,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """MEDIA-11. P7-01a found `idle in transaction` and a granted row lock on
    `message_media` for the whole Meta fetch. With a pool of one, each probe
    below opens a session *on the worker's own pool* while the worker is inside
    the network call - reachable only if the worker gave its connection back -
    and reads the claim the worker committed before it went out."""
    tenant_id, _ = workspace
    conversation_id = await _conversation(committing, workspace)
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)
    stub = Stub()
    seen: dict[str, tuple[int, MediaStatus, bool]] = {}

    def observe(stage: str) -> Any:
        async def during() -> None:
            checked_out = _pool(one_connection).checkedout()
            async with one_connection.session() as session:
                row = (
                    await session.execute(
                        select(MessageMedia.status, MessageMedia.claim_id).where(
                            MessageMedia.id == media_id
                        )
                    )
                ).one()
            seen[stage] = (checked_out, row.status, row.claim_id is not None)

        return during

    stub.during_probe = observe("descriptor")
    stub.during_fetch = observe("download")
    stub.during_read = observe("read")

    worker = _worker(one_connection, redis, tmp_path, stub)
    await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))
    assert await worker.run_once(wait_seconds=1) is True

    assert seen["descriptor"] == (0, MediaStatus.DOWNLOADING, True)
    assert seen["download"] == (0, MediaStatus.DOWNLOADING, True)
    assert seen["read"] == (0, MediaStatus.STORED, True)
    row = await _row(committing, media_id)
    assert row.status is MediaStatus.READY
    assert row.claim_id is None
    assert await AgentQueue(redis).depth() == 1


# ------------------------------------------------------ one file, two attempts


async def test_a_duplicate_attempt_stands_aside_while_the_first_holds_the_file(
    redis: Redis,
    prepared_database: str,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """P7-01, with the lock gone. Two workers, two connections, one file, and
    the second demonstrably running while the first is inside the download.

    Before the remediation the second waited on a row lock for the whole
    download and then downloaded again. Now it meets the committed claim,
    stands aside at once, and releases nothing: one download, one read, one
    object, one turn."""
    tenant_id, _ = workspace
    conversation_id = await _conversation(committing, workspace)
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)

    first_db = Database(_settings(prepared_database))
    second_db = Database(_settings(prepared_database))
    first_stub, second_stub = Stub(), Stub()
    inside_download = asyncio.Event()
    second_finished = asyncio.Event()

    async def hold() -> None:
        inside_download.set()
        await asyncio.wait_for(second_finished.wait(), timeout=30)

    first_stub.during_fetch = hold
    first = _worker(first_db, redis, tmp_path, first_stub)
    second = _worker(second_db, redis, tmp_path, second_stub)
    job = MediaJob(tenant_id=tenant_id, media_id=media_id)

    try:
        running = asyncio.create_task(first._handle(job))
        await asyncio.wait_for(inside_download.wait(), timeout=30)
        # The overlap, proven rather than assumed: the first is parked inside
        # Meta's download right now, holding nothing but its claim.
        second_result = await asyncio.wait_for(second._handle(job), timeout=10)
        second_finished.set()
        first_result = await asyncio.wait_for(running, timeout=30)
    finally:
        second_finished.set()
        await first_db.dispose()
        await second_db.dispose()

    assert second_result is None
    assert second_stub.probes == 0 and second_stub.fetches == 0 and second_stub.reads == 0
    assert first_stub.fetches == 1 and first_stub.reads == 1
    assert first_result is not None
    row = await _row(committing, media_id)
    assert row.status is MediaStatus.READY
    assert row.attempts == 1
    assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 1


# -------------------------------------------- a worker that dies holding a file


async def test_a_worker_killed_after_its_claim_is_recovered_and_the_file_read(
    redis: Redis,
    prepared_database: str,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """The crash window MEDIA-11 opens and MEDIA-03 closes. The claim is
    committed, then the worker dies mid-download: the row is `downloading`
    with a claim nobody honours. Inside the claim lease the sweep leaves it
    alone; past it, the sweep puts it back, and the next attempt finishes it."""
    tenant_id, _ = workspace
    conversation_id = await _conversation(committing, workspace)
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)
    database = Database(_settings(prepared_database))
    stub = Stub()

    async def killed() -> None:
        raise asyncio.CancelledError

    stub.during_fetch = killed
    try:
        with pytest.raises(asyncio.CancelledError):
            await _worker(database, redis, tmp_path, stub)._handle(
                MediaJob(tenant_id=tenant_id, media_id=media_id)
            )

        stranded = await _row(committing, media_id)
        assert stranded.status is MediaStatus.DOWNLOADING
        assert stranded.claim_id is not None and stranded.claimed_at is not None
        claimed_at = stranded.claimed_at

        settings = _settings(prepared_database)
        recovery = MediaRecoveryWorker(
            database=database,
            redis=_RedisClient(redis),  # type: ignore[arg-type]
            settings=settings,
            storage=LocalMediaStorage(tmp_path),
        )
        media_queue = MediaQueue(redis)

        inside = await recovery.run_once(now=claimed_at + claim_lease(settings) / 2)
        assert inside.requeued == [] and inside.abandoned == 0
        assert await media_queue.depth() == 0

        past = await recovery.run_once(
            now=claimed_at + claim_lease(settings) + timedelta(seconds=1)
        )
        assert [job.media_id for job in past.requeued] == [media_id]
        assert await media_queue.depth() == 1
        reset = await _row(committing, media_id)
        assert reset.status is MediaStatus.PENDING
        assert reset.claim_id is None
        assert reset.attempts == 1

        stub.during_fetch = None
        retry = _worker(database, redis, tmp_path, stub)
        assert await retry.run_once(wait_seconds=1) is True
    finally:
        await database.dispose()

    row = await _row(committing, media_id)
    assert row.status is MediaStatus.READY
    assert row.attempts == 2
    assert await AgentQueue(redis).depth() == 1


# ----------------------------------------------- a job that is dead-lettered


async def test_a_dead_lettered_job_gives_its_file_up_and_releases_the_turn(
    redis: Redis,
    prepared_database: str,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal-on-exhaustion, forced by a failure that is not the parser's:
    an unexpected error after the claim, on the queue's last attempt. The job
    is dead-lettered, and the file does not stay unresolved behind it."""
    tenant_id, _ = workspace
    conversation_id = await _conversation(committing, workspace)
    media_id = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversation_id)
    database = Database(_settings(prepared_database))
    stub = Stub()
    monkeypatch.setattr(
        media_worker_module,
        "IDEMPOTENT_RETRY",
        RetryPolicy(max_attempts=1, base_seconds=1.0, max_seconds=1.0),
    )

    async def escape(*_: object, **__: object) -> Any:
        raise RuntimeError("an unexpected failure outside every boundary")

    # Past the reader's own boundary: the attempt itself fails after its
    # claim committed, which is what a dead-lettered job leaves behind.
    original = MediaService.understand
    monkeypatch.setattr(MediaService, "understand", escape)
    try:
        worker = _worker(database, redis, tmp_path, stub)
        await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))
        assert await worker.run_once(wait_seconds=1) is True
        assert len(await worker.queue.dead_letters()) == 1

        row = await _row(committing, media_id)
        assert row.status is MediaStatus.FAILED
        assert row.last_error == text_for(MediaReason.ABANDONED)
        assert row.claim_id is None
        assert await AgentQueue(redis).depth() == 1

        # Terminalising again - a second dead letter, a sweep - writes nothing.
        monkeypatch.setattr(MediaService, "understand", original)
        await worker._abandon(MediaJob(tenant_id=tenant_id, media_id=media_id))
        again = await _row(committing, media_id)
        assert again.status is MediaStatus.FAILED
        assert again.updated_at == row.updated_at
        assert await AgentQueue(redis).depth() == 1
    finally:
        await database.dispose()


# -------------------------------------------------------------- the sweep


async def test_the_sweep_finishes_only_what_nothing_else_will(
    redis: Redis,
    prepared_database: str,
    committing: async_sessionmaker[AsyncSession],
    workspace: tuple[uuid.UUID, uuid.UUID],
    tmp_path: Path,
) -> None:
    """Six files, each in a different state, one pass.

    Presence first: every one of them is unresolved before the pass. After it,
    exactly the stranded ones have moved - requeued while they have attempts
    left, given up on once they do not - and the given-up one's conversation
    is released."""
    tenant_id, account_id = workspace
    settings = _settings(prepared_database)
    now = datetime.now(UTC)
    expired = now - claim_lease(settings) - timedelta(seconds=5)
    long_ago = now - unclaimed_horizon(settings) - timedelta(seconds=5)

    conversations = [await _conversation(committing, workspace) for _ in range(6)]
    live = await _attachment(
        committing,
        tenant_id=tenant_id,
        conversation_id=conversations[0],
        status=MediaStatus.DOWNLOADING,
        claim_id=uuid.uuid4(),
        claimed_at=now,
        attempts=1,
    )
    crashed = await _attachment(
        committing,
        tenant_id=tenant_id,
        conversation_id=conversations[1],
        status=MediaStatus.DOWNLOADING,
        claim_id=uuid.uuid4(),
        claimed_at=expired,
        attempts=1,
    )
    exhausted = await _attachment(
        committing,
        tenant_id=tenant_id,
        conversation_id=conversations[2],
        status=MediaStatus.DOWNLOADING,
        claim_id=uuid.uuid4(),
        claimed_at=expired,
        attempts=MAX_ATTEMPTS,
    )
    fresh = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversations[3])
    lost = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversations[4])
    owed = await _attachment(committing, tenant_id=tenant_id, conversation_id=conversations[5])

    async with committing() as session:
        await session.execute(
            update(MessageMedia)
            .where(MessageMedia.id.in_([lost, owed]))
            .values(created_at=long_ago)
        )
        # `owed`'s media job never reached the queue: its inbound event is
        # still `received`, which is inbound recovery's to finish.
        owed_message = (
            await session.execute(
                select(Message.wa_message_id)
                .join(MessageMedia, MessageMedia.message_id == Message.id)
                .where(MessageMedia.id == owed)
            )
        ).scalar_one()
        session.add(
            WhatsAppEvent(
                tenant_id=tenant_id,
                account_id=account_id,
                event_id=owed_message,
                kind=WhatsAppEventKind.MESSAGE,
                state=WhatsAppEventState.RECEIVED,
                payload={},
                received_at=long_ago,
            )
        )
        await session.commit()

    everything = [live, crashed, exhausted, fresh, lost, owed]
    for media_id in everything:
        assert not (await _row(committing, media_id)).is_resolved

    database = Database(_settings(prepared_database))
    try:
        recovery = MediaRecoveryWorker(
            database=database,
            redis=_RedisClient(redis),  # type: ignore[arg-type]
            settings=settings,
            storage=LocalMediaStorage(tmp_path),
        )
        outcome = await recovery.run_once(now=now)
    finally:
        await database.dispose()

    assert sorted(job.media_id for job in outcome.requeued) == sorted([crashed, lost])
    assert outcome.abandoned == 1
    assert await MediaQueue(redis).depth() == 2

    given_up = await _row(committing, exhausted)
    assert given_up.status is MediaStatus.FAILED
    assert given_up.last_error == text_for(MediaReason.ABANDONED)
    released = [release.conversation_id for release in outcome.releases]
    assert released == [conversations[2]]
    assert await AgentQueue(redis).depth() == 1

    assert (await _row(committing, crashed)).status is MediaStatus.PENDING
    assert (await _row(committing, lost)).attempts == 1
    for untouched in (live, fresh, owed):
        row = await _row(committing, untouched)
        assert not row.is_resolved
        assert row.last_error is None
    assert (await _row(committing, live)).status is MediaStatus.DOWNLOADING
