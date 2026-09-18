"""Two attachments on one conversation, finishing at the same moment, release
exactly one agent turn (MEDIA-18: M21).

The audit found the release tests passed with `ConversationMediaGate.lock`
removed: they ran the two files one after the other, which never builds the
race the lock exists for. This does. Two workers, two database pools, one
conversation; a barrier brings both to the release decision together, and each
then waits for the other to have counted before it commits.

With the lock, the second worker cannot count until the first has committed, so
it sees the first file resolved and releases the one turn - and while it waits,
PostgreSQL reports its backend waiting on a lock, which is observed rather than
assumed. Without the lock, both count while the other's file is still
uncommitted, both see a sibling unresolved, and nobody answers the customer.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.workers.media_queue import MediaJob
from app.workers.media_worker import MediaWorker
from app.workers.queue import AgentJob
from tests import media_harness as h
from tests.fakes import as_media_reader, as_whatsapp

pytestmark = pytest.mark.integration

# How long a worker that has counted waits for its peer to count too. With the
# lock the peer cannot, so this is the window in which the contention is
# observed; without it the peer counts at once.
PEER_WAIT_SECONDS = 2.0


class RacingWorker(MediaWorker):
    """Holds its release decision open until both workers have reached it."""

    def __init__(self, *arguments: Any, barrier: asyncio.Barrier, **keywords: Any) -> None:
        super().__init__(*arguments, **keywords)
        self.barrier = barrier
        self.counted = asyncio.Event()
        self.peer: RacingWorker | None = None

    async def _release_conversation(
        self, *, session: AsyncSession, job: MediaJob, media: MessageMedia
    ) -> AgentJob | None:
        await asyncio.wait_for(self.barrier.wait(), timeout=30)
        decided = await super()._release_conversation(session=session, job=job, media=media)
        self.counted.set()
        assert self.peer is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.peer.counted.wait(), timeout=PEER_WAIT_SECONDS)
        return decided


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, pool_size=4, max_overflow=2)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _two_attachments(
    committing: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    async with committing() as session:
        tenant = Tenant(name="Race", slug=f"race-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        account = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"pn-{uuid.uuid4().hex[:8]}",
            waba_id="555000111",
            display_phone_number="+201000000000",
        )
        contact = Contact(tenant_id=tenant.id, wa_id="201234567890")
        session.add_all([account, contact])
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, contact_id=contact.id, account_id=account.id
        )
        session.add(conversation)
        await session.flush()
        media_ids: list[uuid.UUID] = []
        for _ in range(2):
            message = Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                wa_message_id=f"wamid.{uuid.uuid4().hex}",
                direction=MessageDirection.INBOUND,
                kind=MessageKind.IMAGE,
                status=MessageStatus.RECEIVED,
                origin=MessageOrigin.CUSTOMER,
            )
            session.add(message)
            await session.flush()
            media = MessageMedia(
                tenant_id=tenant.id,
                message_id=message.id,
                conversation_id=conversation.id,
                wa_media_id=f"media-{uuid.uuid4().hex[:8]}",
                mime_type="image/png",
                status=MediaStatus.PENDING,
            )
            session.add(media)
            await session.flush()
            media_ids.append(media.id)
        await session.commit()
        return tenant.id, conversation.id, media_ids


async def test_two_files_finishing_together_release_exactly_one_turn(
    committing: async_sessionmaker[AsyncSession],
    prepared_database: str,
    tmp_path: Path,
    settings: Settings,
) -> None:
    tenant_id, conversation_id, (first_id, second_id) = await _two_attachments(committing)
    configured = Settings(
        _env_file=None, environment="test", database_url=prepared_database, database_pool_size=2
    )
    first_db, second_db = Database(configured), Database(configured)
    barrier = asyncio.Barrier(2)

    def racer(database: Database) -> RacingWorker:
        worker = RacingWorker(
            database=database,
            redis=h.FakeRedis(),
            settings=configured,
            storage=LocalMediaStorage(tmp_path),
            whatsapp_factory=lambda http: as_whatsapp(h.StubWhatsApp()),
            reader_factory=lambda http: as_media_reader(h.StubReader()),
            barrier=barrier,
        )
        return worker

    first, second = racer(first_db), racer(second_db)
    first.peer, second.peer = second, first

    waits: list[int] = []
    observing = True

    async def observe_lock_waits() -> None:
        engine = create_async_engine(prepared_database)
        try:
            while observing:
                async with engine.connect() as connection:
                    waiting = (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname = current_database() "
                                "AND wait_event_type = 'Lock' "
                                "AND query ILIKE '%FROM conversations%FOR UPDATE%'"
                            )
                        )
                    ).scalar_one()
                waits.append(int(waiting))
                await asyncio.sleep(0.05)
        finally:
            await engine.dispose()

    observer = asyncio.create_task(observe_lock_waits())
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                first._handle(MediaJob(tenant_id=tenant_id, media_id=first_id)),
                second._handle(MediaJob(tenant_id=tenant_id, media_id=second_id)),
            ),
            timeout=60,
        )
    finally:
        observing = False
        await observer
        await first_db.dispose()
        await second_db.dispose()
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.commit()

    released = [job for job in results if job is not None]
    assert len(released) == 1
    assert released[0].conversation_id == conversation_id
    # The gate was genuinely contended: one worker's backend sat waiting on
    # the conversation row lock while the other held it.
    assert max(waits) >= 1
