"""A purged workspace's files converge to gone, in a real object store (MEDIA-07).

Against real PostgreSQL and real MinIO, with presence proven before absence:
every "the object is gone" below follows a "the object was there".

The audit's two windows, and the crash inside each:

- **P3-04** - the store refuses the deletes. Before: the workspace was marked
  purged, the next pass purged nothing, the files stayed, and no row anywhere
  named them. Now the purge's own commit records each key, a refused delete
  leaves its record, and a later pass finishes it.
- **P3-05** - an upload's intent committed, the purge ran, and the upload's
  object landed afterwards as an orphan. Now the writer removes its own object
  when it finds its row gone, and the purge's record of that key waits out the
  upload grace so even a writer that dies first is cleaned up behind.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.object_store import S3MediaStorage
from app.core.storage import StorageError, build_key
from app.db.models.audit import AuditLog
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MediaStorageState, MessageMedia
from app.db.models.media_purge import MediaPurgeObject
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.integrations.whatsapp.client import DownloadedMedia, MediaDescriptor
from app.repositories.media_purge_repository import MediaPurgeLedger
from app.services.media_service import MediaService
from app.services.workspace_purge_service import WorkspacePurgeService
from app.workers.purge_worker import PurgeWorker
from tests.fakes import as_whatsapp

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"a purged photograph" * 16
GRACE = timedelta(seconds=900)


def _store() -> S3MediaStorage:
    endpoint = os.environ.get("TEST_S3_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("No object store configured; set TEST_S3_ENDPOINT_URL to run these.")
    return S3MediaStorage(
        bucket=os.environ.get("TEST_S3_BUCKET", "wasla-media"),
        access_key_id=os.environ.get("TEST_S3_ACCESS_KEY_ID", ""),
        secret_access_key=os.environ.get("TEST_S3_SECRET_ACCESS_KEY", ""),
        endpoint_url=endpoint,
        path_style=True,
    )


class RefusingDeletes:
    """The real store, except that it will not delete anything."""

    def __init__(self, inner: S3MediaStorage) -> None:
        self._inner = inner
        self.refused = 0

    async def put_at(self, *, key: str, data: bytes, mime_type: str | None = None) -> None:
        await self._inner.put_at(key=key, data=data, mime_type=mime_type)

    async def get(self, key: str) -> bytes:
        return await self._inner.get(key)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)

    async def delete(self, key: str) -> None:
        self.refused += 1
        raise StorageError()


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        jwt_secret="purge-objects-secret-not-for-deployment",
        media_upload_grace_seconds=GRACE.total_seconds(),
    )


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, pool_size=6, max_overflow=2)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    instance = Database(_settings(prepared_database))
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest_asyncio.fixture
async def doomed(
    committing: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A workspace with one conversation, deleted, its retention not yet run out."""
    async with committing() as session:
        tenant = Tenant(name="Doomed", slug=f"doomed-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        account = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"pn-{uuid.uuid4().hex[:10]}",
            waba_id="555000111",
            display_phone_number="+201000000000",
        )
        contact = Contact(tenant_id=tenant.id, wa_id=f"2012{uuid.uuid4().int % 10**8:08d}")
        session.add_all([account, contact])
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, contact_id=contact.id, account_id=account.id
        )
        session.add(conversation)
        await session.commit()
        created = (tenant.id, conversation.id)
    try:
        yield created
    finally:
        async with committing() as session:
            await session.execute(
                delete(MediaPurgeObject).where(MediaPurgeObject.tenant_id == created[0])
            )
            # The purge's audit entry is filed with no tenant, on purpose, so
            # it outlives the workspace; this file's own entries are removed
            # so they cannot be read as some other test's purge.
            await session.execute(delete(AuditLog).where(AuditLog.target_id == created[0]))
            await session.execute(delete(Tenant).where(Tenant.id == created[0]))
            await session.commit()


async def _stored_file(
    committing: async_sessionmaker[AsyncSession],
    store: S3MediaStorage,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> str:
    """A file written to MinIO and recorded as stored, as a finished download is."""
    key = build_key(tenant_id=tenant_id, mime_type="image/png")
    await store.put_at(key=key, data=PNG, mime_type="image/png")
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
        session.add(
            MessageMedia(
                tenant_id=tenant_id,
                message_id=message.id,
                conversation_id=conversation_id,
                wa_media_id=f"media-{uuid.uuid4().hex[:8]}",
                mime_type="image/png",
                status=MediaStatus.READY,
                storage_state=MediaStorageState.STORED,
                storage_key=key,
                byte_size=len(PNG),
            )
        )
        await session.commit()
    return key


async def _make_due(
    committing: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, now: datetime
) -> None:
    async with committing() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.deleted_at = now - timedelta(days=31)
        tenant.purge_due_at = now - timedelta(days=1)
        await session.commit()


async def _ledger(committing: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> int:
    async with committing() as session:
        return await MediaPurgeLedger(session).outstanding(tenant_id)


async def _purged_at(
    committing: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> datetime | None:
    async with committing() as session:
        return (
            await session.execute(select(Tenant.purged_at).where(Tenant.id == tenant_id))
        ).scalar_one()


# ---------------------------------------------------------- P3-04: refused


async def test_a_purge_whose_deletes_are_refused_keeps_owing_them_until_they_land(
    committing: async_sessionmaker[AsyncSession],
    database: Database,
    doomed: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, conversation_id = doomed
    store = _store()
    keys = [await _stored_file(committing, store, tenant_id, conversation_id) for _ in range(2)]
    now = datetime.now(UTC)
    await _make_due(committing, tenant_id, now)
    assert [await store.exists(key) for key in keys] == [True, True]

    refusing = RefusingDeletes(store)
    settings = _settings("unused")
    first = await PurgeWorker(database=database, settings=settings, storage=refusing).run_once(
        now=now
    )

    # Purged in the database - and, visibly, not finished in the store.
    assert first.purged == 1
    assert (first.objects_deleted, first.objects_failed, first.objects_pending) == (0, 2, 2)
    assert await _purged_at(committing, tenant_id) is not None
    assert await _ledger(committing, tenant_id) == 2
    assert [await store.exists(key) for key in keys] == [True, True]

    second = await PurgeWorker(database=database, settings=settings, storage=store).run_once(
        now=now + timedelta(minutes=5)
    )

    assert second.purged == 0
    assert (second.objects_deleted, second.objects_failed, second.objects_pending) == (2, 0, 0)
    assert [await store.exists(key) for key in keys] == [False, False]
    assert await _ledger(committing, tenant_id) == 0


async def test_a_process_that_dies_after_the_purge_commit_leaves_the_deletes_owed(
    committing: async_sessionmaker[AsyncSession],
    database: Database,
    doomed: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The purge committed and the process died before a single delete. The
    keys are in the ledger, so the next process finishes the job."""
    tenant_id, conversation_id = doomed
    store = _store()
    key = await _stored_file(committing, store, tenant_id, conversation_id)
    now = datetime.now(UTC)
    await _make_due(committing, tenant_id, now)

    async with committing() as session:
        service = WorkspacePurgeService(session, in_flight_grace=GRACE)
        (tenant,) = await service.claim_due(now=now, limit=1)
        outcome = await service.purge(tenant, now=now)
        await session.commit()
    assert outcome.objects_recorded == 1
    async with committing() as session:
        rows = (
            await session.execute(
                select(func.count())
                .select_from(MessageMedia)
                .where(MessageMedia.tenant_id == tenant_id)
            )
        ).scalar_one()
    assert rows == 0
    assert await store.exists(key) is True
    assert await _ledger(committing, tenant_id) == 1

    finished = await PurgeWorker(
        database=database, settings=_settings("unused"), storage=store
    ).run_once(now=now)

    assert finished.objects_deleted == 1
    assert await store.exists(key) is False
    assert await _ledger(committing, tenant_id) == 0


# -------------------------------------------------------- P3-05: the race


class PausedStore:
    """The real store, with the object write held at a barrier the test opens."""

    def __init__(self, inner: S3MediaStorage) -> None:
        self._inner = inner
        self.at_write = asyncio.Event()
        self.may_write = asyncio.Event()
        self.written: list[str] = []
        self.present_after_write: list[bool] = []

    async def put_at(self, *, key: str, data: bytes, mime_type: str | None = None) -> None:
        self.at_write.set()
        await asyncio.wait_for(self.may_write.wait(), timeout=30)
        await self._inner.put_at(key=key, data=data, mime_type=mime_type)
        self.written.append(key)
        self.present_after_write.append(await self._inner.exists(key))

    async def get(self, key: str) -> bytes:
        return await self._inner.get(key)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)


class Meta:
    async def probe_media(self, media_id: str) -> MediaDescriptor:
        return MediaDescriptor(mime_type="image/png", byte_size=len(PNG))

    async def fetch_media(self, media_id: str, *, max_bytes: int) -> DownloadedMedia:
        return DownloadedMedia(
            content=PNG, mime_type="image/png", byte_size=len(PNG), declared_size=None, sha256=None
        )


async def _pending_attachment(
    committing: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, conversation_id: uuid.UUID
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
            wa_media_id=f"media-{uuid.uuid4().hex[:8]}",
            mime_type="image/png",
            status=MediaStatus.PENDING,
        )
        session.add(media)
        await session.commit()
        return media.id


async def _download_with(
    committing: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    media_id: uuid.UUID,
    store: Any,
    settings: Settings,
) -> Any:
    async with committing() as session:
        media = await session.get(MessageMedia, media_id)
        assert media is not None
        service = MediaService(
            session=session,
            tenant_id=tenant_id,
            settings=settings,
            storage=store,
            whatsapp=as_whatsapp(Meta()),
        )
        outcome = await service.download(media)
        await session.commit()
        return outcome


async def _purge_now(
    committing: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, now: datetime
) -> None:
    async with committing() as session:
        service = WorkspacePurgeService(session, in_flight_grace=GRACE)
        (tenant,) = await service.claim_due(now=now, limit=1)
        await service.purge(tenant, now=now)
        await session.commit()


async def test_a_write_landing_after_the_purge_leaves_no_object_behind(
    committing: async_sessionmaker[AsyncSession],
    database: Database,
    doomed: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """P3-05, with real concurrency. The intent commits, the writer is parked
    before its PUT, the purge commits, the writer resumes and its PUT lands.
    The object demonstrably existed - and is gone before the writer returns,
    removed by the writer itself, with the purge's record still standing
    behind it for the grace period."""
    tenant_id, conversation_id = doomed
    media_id = await _pending_attachment(committing, tenant_id, conversation_id)
    settings = _settings("unused")
    store = PausedStore(_store())

    writing = asyncio.create_task(_download_with(committing, tenant_id, media_id, store, settings))
    await asyncio.wait_for(store.at_write.wait(), timeout=30)

    # The intent is committed while the writer waits - that is the window.
    async with committing() as session:
        intent = await session.get(MessageMedia, media_id)
        assert intent is not None
        assert intent.storage_state is MediaStorageState.PENDING
        key = intent.storage_key
        assert key is not None

    now = datetime.now(UTC)
    await _make_due(committing, tenant_id, now)
    await _purge_now(committing, tenant_id, now)
    assert await _ledger(committing, tenant_id) == 1

    store.may_write.set()
    outcome = await asyncio.wait_for(writing, timeout=30)

    assert outcome.deferred
    assert store.written == [key]
    assert store.present_after_write == [True]
    assert await store.exists(key) is False

    # The ledger row waits out the grace, then settles.
    early = await PurgeWorker(database=database, settings=settings, storage=store).run_once(
        now=now + GRACE / 2
    )
    assert early.objects_deleted == 0 and await _ledger(committing, tenant_id) == 1
    late = await PurgeWorker(database=database, settings=settings, storage=store).run_once(
        now=now + GRACE + timedelta(seconds=1)
    )
    assert late.objects_deleted == 1 and await _ledger(committing, tenant_id) == 0


async def test_a_writer_that_dies_after_its_late_write_is_cleaned_up_behind(
    committing: async_sessionmaker[AsyncSession],
    database: Database,
    doomed: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worst point: the late PUT lands and the writer dies before it can
    remove it. The purge's record of the key, held past the grace, is what
    still converges - and it does not delete early, before a live writer
    could have finished."""
    tenant_id, conversation_id = doomed
    media_id = await _pending_attachment(committing, tenant_id, conversation_id)
    settings = _settings("unused")
    store = PausedStore(_store())

    async def dies(*_: object, **__: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(MediaService, "_discard_orphan", dies)
    writing = asyncio.create_task(_download_with(committing, tenant_id, media_id, store, settings))
    await asyncio.wait_for(store.at_write.wait(), timeout=30)
    async with committing() as session:
        intent = await session.get(MessageMedia, media_id)
        assert intent is not None and intent.storage_key is not None
        key = intent.storage_key

    now = datetime.now(UTC)
    await _make_due(committing, tenant_id, now)
    await _purge_now(committing, tenant_id, now)
    store.may_write.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(writing, timeout=30)

    assert await store.exists(key) is True
    early = await PurgeWorker(database=database, settings=settings, storage=store).run_once(
        now=now + GRACE / 2
    )
    assert early.objects_pending == 1
    assert await store.exists(key) is True

    late = await PurgeWorker(database=database, settings=settings, storage=store).run_once(
        now=now + GRACE + timedelta(seconds=1)
    )
    assert late.objects_deleted == 1
    assert await store.exists(key) is False
    assert await _ledger(committing, tenant_id) == 0
