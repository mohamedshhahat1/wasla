"""An object cannot exist without a row that named it first.

The failure these exist for was stated in ADR-078 and left open: the media
service wrote the object and *then* committed the row, so a transaction that
failed in between left an object nothing referenced. It was invisible to every
query in the system - retention starts from a row carrying a key, and there was
no such row - so nothing would ever have deleted it, and nothing could have
found it without listing the bucket.

Reproduced before it was fixed, against this same PostgreSQL and this same
MinIO:

    PUT succeeded, transaction rolled back.
    the object is readable from MinIO after the rollback
    the media row is status='pending' storage_key=None - it never heard of it
    retention's two queries return 0 due / 0 unfinished rows

Every test below is the same sequence with the protocol of ADR-087 in place,
asserting what happens instead.

**Real commits, over an engine of this file's own.** The suite's `db_session`
joins the test's transaction as a savepoint, so a `commit()` inside it is a
savepoint release that the outer rollback undoes. That is exactly the property
these tests must not have: what is being asserted is that a *committed* intent
survives a transaction that fails afterwards, and a fixture that quietly undoes
commits would make every one of them pass on the broken code.

**Synthetic media only.** A PNG header followed by a repeated string; no real
customer file, no real credential, no bucket listing anywhere.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.object_store import S3MediaStorage
from app.core.storage import MediaStorage, StorageError
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MediaStorageState, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEvent, UsageEventType
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.media_repository import PlatformMediaRepository
from app.services.media_retention_service import MediaRetentionService
from app.services.media_service import MediaService, content_hash
from app.services.media_upload_service import MediaUploadReconciler
from tests.fakes import as_whatsapp

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"a synthetic photograph" * 16
OTHER = b"%PDF-1.7\nnot the file the row describes\n%%EOF\n"

# Every age below is expressed against this instant rather than the clock. A
# grace period is a comparison, and a test that read the real clock would be
# asserting a boundary that moves while it runs.
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
GRACE = 900.0


# ------------------------------------------------------------------ the store


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


def _unreachable() -> S3MediaStorage:
    """A store that cannot be reached, which is not a store that is empty."""
    return S3MediaStorage(
        bucket="wasla-media",
        access_key_id="k",
        secret_access_key="s",
        # A port nothing listens on: a connection failure, not a 404.
        endpoint_url="http://127.0.0.1:9",
        path_style=True,
        timeout_seconds=2.0,
    )


class RecordingStore:
    """Every key ever written through it.

    How "exactly one object was created" is asserted without listing the
    bucket. A listing would answer the same question and would also be the one
    design ADR-087 forbids - so the count comes from watching the writes rather
    than from asking the store what it holds.
    """

    def __init__(self, inner: MediaStorage) -> None:
        self._inner = inner
        self.written: list[str] = []

    async def put_at(self, *, key: str, data: bytes, mime_type: str | None = None) -> None:
        self.written.append(key)
        await self._inner.put_at(key=key, data=data, mime_type=mime_type)

    async def get(self, key: str) -> bytes:
        return await self._inner.get(key)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)

    @property
    def distinct_objects(self) -> set[str]:
        return set(self.written)


# ------------------------------------------------------------------- fixtures


@dataclass(frozen=True, slots=True)
class _Descriptor:
    mime_type: str | None
    byte_size: int | None


@dataclass(frozen=True, slots=True)
class _Downloaded:
    content: bytes
    mime_type: str | None
    byte_size: int


class FakeWhatsApp:
    """Synthetic bytes instead of a customer's file from Meta."""

    def __init__(self, content: bytes = PNG) -> None:
        self._content = content
        self.fetches = 0

    async def probe_media(self, media_id: str) -> _Descriptor:
        return _Descriptor(mime_type="image/png", byte_size=len(self._content))

    async def fetch_media(self, media_id: str, *, max_bytes: int) -> _Downloaded:
        self.fetches += 1
        return _Downloaded(
            content=self._content, mime_type="image/png", byte_size=len(self._content)
        )


class InjectedFailureError(Exception):
    """Anything that can break a transaction after the object is written.

    A deadlock, a connection reset, a statement timeout, a constraint somewhere
    else in the same unit of work. Which one it is does not matter: what the
    protocol has to survive is the transaction not committing.
    """


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "jwt_secret": "media-atomicity-secret-not-for-deployment",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, over an engine of this file's own.

    A real pool rather than `NullPool`: the concurrency test runs two
    reconcilers at once, and each takes a session per claim.
    """
    engine = create_async_engine(prepared_database, pool_size=8, max_overflow=4)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def storage() -> AsyncIterator[RecordingStore]:
    yield RecordingStore(_store())


@pytest_asyncio.fixture
async def tenant_id(committing: async_sessionmaker[AsyncSession]) -> AsyncIterator[uuid.UUID]:
    """One workspace, removed afterwards along with everything under it."""
    async with committing() as session:
        tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.commit()
        created = tenant.id
    try:
        yield created
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == created))
            await session.commit()


async def _pending_media(session: AsyncSession, tenant: uuid.UUID) -> uuid.UUID:
    """A message carrying a file nobody has downloaded yet."""
    account = WhatsAppAccount(
        tenant_id=tenant,
        phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant, wa_id=f"2012{uuid.uuid4().int % 10**8:08d}")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(tenant_id=tenant, contact_id=contact.id, account_id=account.id)
    session.add(conversation)
    await session.flush()

    message = Message(
        tenant_id=tenant,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.IMAGE,
        status=MessageStatus.DELIVERED,
    )
    session.add(message)
    await session.flush()

    media = MessageMedia(
        tenant_id=tenant,
        message_id=message.id,
        conversation_id=conversation.id,
        wa_media_id=f"wamid-{uuid.uuid4().hex[:10]}",
        mime_type="image/png",
        status=MediaStatus.PENDING,
    )
    session.add(media)
    await session.commit()
    return media.id


def _service(
    session: AsyncSession,
    *,
    tenant: uuid.UUID,
    storage: MediaStorage,
    whatsapp: FakeWhatsApp | None = None,
) -> MediaService:
    return MediaService(
        session=session,
        tenant_id=tenant,
        settings=_settings(),
        storage=storage,
        whatsapp=as_whatsapp(whatsapp or FakeWhatsApp()),
    )


async def _row(session: AsyncSession, media_id: uuid.UUID) -> MessageMedia:
    row = await session.get(MessageMedia, media_id, populate_existing=True)
    assert row is not None
    return row


async def _age_intent(
    committing: async_sessionmaker[AsyncSession],
    media_id: uuid.UUID,
    *,
    seconds: float,
) -> None:
    """Move an intent's clock back, rather than waiting out a grace period."""
    async with committing() as session:
        row = await _row(session, media_id)
        row.upload_started_at = NOW - timedelta(seconds=seconds)
        await session.commit()


# ============================================== GATE 1: PUT, then the DB fails


async def test_a_failed_finalisation_leaves_an_object_reconciliation_adopts(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole of P2-D, in one sequence.

    Intent committed, object written, finalisation broken, transaction rolled
    back - and the object is still owned. Before ADR-087 the same three steps
    left an object with no row anywhere pointing at it.
    """
    async with committing() as setup:
        media_id = await _pending_media(setup, tenant_id)

    async def explode(*_: object, **__: object) -> None:
        raise InjectedFailureError()

    monkeypatch.setattr(MediaService, "finalize", explode)

    with pytest.raises(InjectedFailureError):
        async with committing() as session:
            service = _service(session, tenant=tenant_id, storage=storage)
            await service.download(await _row(session, media_id))

    # The object is in the bucket, exactly as it was before the fix.
    assert len(storage.distinct_objects) == 1
    key = storage.written[0]
    assert await storage.get(key) == PNG

    # What is different: a committed row owns it.
    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.PENDING
        assert row.storage_key == key
        assert row.content_hash == content_hash(PNG)
        assert row.byte_size == len(PNG)
        assert row.upload_started_at is not None
        # And it is not readable yet: a key is not a file.
        assert row.is_stored is False

    await _age_intent(committing, media_id, seconds=GRACE + 60)

    async with committing() as session:
        outcome = await MediaUploadReconciler(session=session, storage=storage).run(
            now=NOW, grace_seconds=GRACE, limit=10
        )

    assert outcome.finalized == 1
    assert outcome.missing == 0
    assert outcome.mismatched == 0

    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.STORED
        assert row.storage_key == key
        assert row.is_stored is True

        # Readable, which is what a colleague opening the attachment needs.
        service = _service(check, tenant=tenant_id, storage=storage)
        assert await service.read(row) == PNG

    # No second object was written to recover the first.
    assert len(storage.distinct_objects) == 1
    await storage.delete(key)


async def test_a_pending_intent_is_not_readable(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key on a row is not permission to serve what is at it.

    The download route and the media reader both gate on `is_stored`. Gating on
    `storage_key is not None` instead would serve an object during the window
    in which nothing has verified it is there.
    """
    async with committing() as setup:
        media_id = await _pending_media(setup, tenant_id)

    async def explode(*_: object, **__: object) -> None:
        raise InjectedFailureError()

    monkeypatch.setattr(MediaService, "finalize", explode)
    with pytest.raises(InjectedFailureError):
        async with committing() as session:
            await _service(session, tenant=tenant_id, storage=storage).download(
                await _row(session, media_id)
            )

    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_key is not None
        with pytest.raises(StorageError):
            await _service(check, tenant=tenant_id, storage=storage).read(row)

    await storage.delete(storage.written[0])


# ================================================ GATE 3: crash before the PUT


async def test_an_intent_whose_object_never_arrived_is_not_called_stored(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """Nothing was written, so nothing is adopted.

    The row goes back to owning no object at all, which is what lets a later
    attempt allocate a fresh key without leaking the one it never used.
    """
    async with committing() as session:
        media_id = await _pending_media(session, tenant_id)
        service = _service(session, tenant=tenant_id, storage=storage)
        key = await service.intend(await _row(session, media_id), mime_type="image/png", data=PNG)
        await session.commit()

    assert key is not None
    assert storage.written == []  # the process died here

    await _age_intent(committing, media_id, seconds=GRACE + 60)

    async with committing() as session:
        outcome = await MediaUploadReconciler(session=session, storage=storage).run(
            now=NOW, grace_seconds=GRACE, limit=10
        )

    assert outcome.missing == 1
    assert outcome.finalized == 0

    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.ABSENT
        assert row.storage_key is None
        assert row.is_stored is False


# ================================================== GATE 4: the duplicate job


async def test_the_same_job_run_twice_writes_one_object(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """A redelivered media job must not leave a spare object behind.

    The first run finalises; the second finds the row already stored and does
    nothing. What is asserted is the object count, because a retry that
    allocated a fresh key would still produce a perfectly consistent row - and
    an object nothing references.
    """
    async with committing() as setup:
        media_id = await _pending_media(setup, tenant_id)

    for _ in range(2):
        async with committing() as session:
            service = _service(session, tenant=tenant_id, storage=storage)
            await service.download(await _row(session, media_id))
            await session.commit()

    assert len(storage.distinct_objects) == 1

    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.STORED
        assert row.storage_key == storage.written[0]

    await storage.delete(storage.written[0])


async def test_a_retry_after_a_failed_write_reuses_the_committed_key(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """The intent is the idempotency key, and it survives the failure.

    First attempt: the store refuses, the row keeps its intent. Second attempt:
    the same key, so the object the first attempt might have written after all
    - a write can fail on the way back - is the one that ends up owned.
    """
    async with committing() as setup:
        media_id = await _pending_media(setup, tenant_id)

    broken = RecordingStore(_unreachable())
    async with committing() as session:
        outcome = await _service(session, tenant=tenant_id, storage=broken).download(
            await _row(session, media_id)
        )
        await session.commit()
    assert outcome.status is MediaStatus.FAILED

    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.PENDING
        first_key = row.storage_key
        assert first_key is not None

    async with committing() as session:
        await _service(session, tenant=tenant_id, storage=storage).download(
            await _row(session, media_id)
        )
        await session.commit()

    assert storage.written == [first_key]
    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_key == first_key
        assert row.storage_state is MediaStorageState.STORED

    await storage.delete(first_key)


# =============================================== GATE 5: the object is not ours


async def test_an_object_that_is_not_what_the_row_describes_is_never_adopted(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """Different bytes at our key: quarantine, do not serve, do not delete.

    The hash is recomputed over what comes back rather than compared against an
    ETag, which S3 defines as an opaque validator and which is not a SHA-256 of
    anything.
    """
    async with committing() as session:
        media_id = await _pending_media(session, tenant_id)
        key = await _service(session, tenant=tenant_id, storage=storage).intend(
            await _row(session, media_id), mime_type="image/png", data=PNG
        )
        await session.commit()

    assert key is not None
    # Somebody else's bytes, at the key this row owns.
    await storage.put_at(key=key, data=OTHER, mime_type="application/pdf")

    await _age_intent(committing, media_id, seconds=GRACE + 60)

    async with committing() as session:
        outcome = await MediaUploadReconciler(session=session, storage=storage).run(
            now=NOW, grace_seconds=GRACE, limit=10
        )

    assert outcome.mismatched == 1
    assert outcome.finalized == 0

    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.MISMATCHED
        assert row.is_stored is False
        # Not served.
        with pytest.raises(StorageError):
            await _service(check, tenant=tenant_id, storage=storage).read(row)

    # Not deleted either: it is the only evidence of how it got there.
    assert await storage.exists(key) is True
    await storage.delete(key)


async def test_a_quarantined_row_is_left_alone_by_a_later_download(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """A retry must not paper over a mismatch by writing on top of it."""
    async with committing() as session:
        media_id = await _pending_media(session, tenant_id)
        row = await _row(session, media_id)
        row.storage_key = f"{tenant_id}/2026/09/{uuid.uuid4()}.png"
        row.storage_state = MediaStorageState.MISMATCHED
        await session.commit()

    async with committing() as session:
        await _service(session, tenant=tenant_id, storage=storage).download(
            await _row(session, media_id)
        )
        await session.commit()

    assert storage.written == []
    async with committing() as check:
        assert (await _row(check, media_id)).storage_state is MediaStorageState.MISMATCHED


# =============================================== GATE 6: down is not the same as empty


async def test_a_store_that_cannot_be_reached_settles_nothing(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """The distinction the whole recovery turns on.

    An outage read as "the object is gone" would abandon every upload in
    flight during it - and for outbound media, whose bytes arrived in a request
    body that no longer exists, abandoning is final.
    """
    async with committing() as session:
        media_id = await _pending_media(session, tenant_id)
        key = await _service(session, tenant=tenant_id, storage=storage).intend(
            await _row(session, media_id), mime_type="image/png", data=PNG
        )
        await session.commit()

    assert key is not None
    await storage.put_at(key=key, data=PNG, mime_type="image/png")
    await _age_intent(committing, media_id, seconds=GRACE + 60)

    async with committing() as session:
        outcome = await MediaUploadReconciler(session=session, storage=_unreachable()).run(
            now=NOW, grace_seconds=GRACE, limit=10
        )

    assert outcome.unreachable == 1
    assert outcome.missing == 0
    assert outcome.finalized == 0

    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.PENDING
        assert row.storage_key == key

    # And when the store answers again, the same intent finalises.
    async with committing() as session:
        recovered = await MediaUploadReconciler(session=session, storage=storage).run(
            now=NOW, grace_seconds=GRACE, limit=10
        )
    assert recovered.finalized == 1

    async with committing() as check:
        assert (await _row(check, media_id)).storage_state is MediaStorageState.STORED

    await storage.delete(key)


async def test_an_intent_inside_its_grace_period_is_left_to_its_writer(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """Reconciling a live write would race the finalisation it is about to do."""
    async with committing() as session:
        media_id = await _pending_media(session, tenant_id)
        await _service(session, tenant=tenant_id, storage=storage).intend(
            await _row(session, media_id), mime_type="image/png", data=PNG
        )
        await session.commit()

    await _age_intent(committing, media_id, seconds=GRACE / 2)

    async with committing() as session:
        outcome = await MediaUploadReconciler(session=session, storage=storage).run(
            now=NOW, grace_seconds=GRACE, limit=10
        )

    assert outcome.examined == 0
    async with committing() as check:
        assert (await _row(check, media_id)).storage_state is MediaStorageState.PENDING


# ================================================= GATE 7: retention interaction


async def test_retention_will_not_claim_an_upload_that_never_finished(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """An upload in flight is old the moment its message is.

    Retention used to select every row with a key, which after ADR-087 includes
    intents whose object has not been proved to exist. Deleting one of those
    would be retention destroying a file nobody had finished writing - and the
    reconciler would then find the object missing and abandon a row whose file
    was fine a second earlier.
    """
    async with committing() as session:
        media_id = await _pending_media(session, tenant_id)
        key = await _service(session, tenant=tenant_id, storage=storage).intend(
            await _row(session, media_id), mime_type="image/png", data=PNG
        )
        await session.commit()

    assert key is not None
    await storage.put_at(key=key, data=PNG, mime_type="image/png")

    # Old enough for any retention period a deployment could set.
    async with committing() as session:
        row = await _row(session, media_id)
        row.created_at = NOW - timedelta(days=400)
        await session.commit()

    async with committing() as session:
        claimed = await MediaRetentionService(session=session, storage=storage).claim(
            now=NOW, retention_days=1, limit=100
        )

    assert all(item.id != media_id for item in claimed)
    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.PENDING
        assert row.purge_started_at is None
    assert await storage.exists(key) is True

    # Reconciliation owns it, and finishing it makes it eligible.
    await _age_intent(committing, media_id, seconds=GRACE + 60)
    async with committing() as session:
        await MediaUploadReconciler(session=session, storage=storage).run(
            now=NOW, grace_seconds=GRACE, limit=10
        )

    async with committing() as session:
        outcome = await MediaRetentionService(session=session, storage=storage).sweep(
            now=NOW, retention_days=1, limit=100
        )

    assert outcome.purged == 1
    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.PURGED
        assert row.storage_key is None
    assert await storage.exists(key) is False


async def test_a_purged_row_is_never_mistaken_for_one_that_was_never_uploaded(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """The P2-A distinction, now carried by a column rather than by two nulls.

    A purged row and a never-downloaded row are both "no key". Reading them as
    the same thing would have a replayed media job ask Meta for a handle that
    expired months ago and, if it somehow succeeded, undo the deletion the
    workspace asked for.
    """
    async with committing() as session:
        media_id = await _pending_media(session, tenant_id)
        row = await _row(session, media_id)
        row.storage_key = None
        row.purge_started_at = NOW
        row.storage_state = MediaStorageState.PURGED
        row.status = MediaStatus.READY
        row.transcript = "A blue sofa."
        await session.commit()

    whatsapp = FakeWhatsApp()
    async with committing() as session:
        await _service(session, tenant=tenant_id, storage=storage, whatsapp=whatsapp).download(
            await _row(session, media_id)
        )
        await session.commit()

    assert whatsapp.fetches == 0
    assert storage.written == []
    async with committing() as check:
        row = await _row(check, media_id)
        assert row.storage_state is MediaStorageState.PURGED
        assert row.is_purged is True
        assert row.transcript == "A blue sofa."

    # And reconciliation does not look at it either: it is not pending.
    async with committing() as session:
        outcome = await MediaUploadReconciler(session=session, storage=storage).run(
            now=NOW, grace_seconds=GRACE, limit=10
        )
    assert outcome.examined == 0


# =========================================================== metering, exactly once


async def test_recovered_storage_is_metered_once_and_only_once(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whoever finalises meters, and the transition happens once.

    The usage event used to be written in the same transaction as the object
    reference, so a rolled-back finalisation lost both together. It still does -
    which is why the recovery has to record it, and why recording it twice
    would bill a workspace for bytes it stored once.
    """
    async with committing() as setup:
        media_id = await _pending_media(setup, tenant_id)

    async def explode(*_: object, **__: object) -> None:
        raise InjectedFailureError()

    monkeypatch.setattr(MediaService, "finalize", explode)
    with pytest.raises(InjectedFailureError):
        async with committing() as session:
            await _service(session, tenant=tenant_id, storage=storage).download(
                await _row(session, media_id)
            )
    monkeypatch.undo()

    async with committing() as check:
        assert await _storage_events(check, tenant_id) == 0

    await _age_intent(committing, media_id, seconds=GRACE + 60)
    for _ in range(2):
        async with committing() as session:
            await MediaUploadReconciler(session=session, storage=storage).run(
                now=NOW, grace_seconds=GRACE, limit=10
            )

    async with committing() as check:
        assert await _storage_events(check, tenant_id) == 1

    await storage.delete(storage.written[0])


async def _storage_events(session: AsyncSession, tenant: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(UsageEvent)
        .where(
            UsageEvent.tenant_id == tenant,
            UsageEvent.event_type == UsageEventType.STORAGE_USED,
        )
    )
    return int(result.scalar_one())


# ================================================================= concurrency


async def test_two_reconcilers_settle_one_intent_once(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """Both workers look, one claims, the other skips.

    Structural rather than timed: the barrier makes both passes reach the claim
    together, and what is asserted is that the finalisations sum to one. How
    long either took on this machine is not evidence of anything.

    Without `FOR UPDATE SKIP LOCKED` the second worker blocks on the first,
    then reads the row *after* it was finalised - so it would not double-count
    here either, but it would serialise every pass behind every other and would
    verify the same object twice against the store. The assertion that catches
    a missing `SKIP LOCKED` is the read count.
    """
    async with committing() as setup:
        media_id = await _pending_media(setup, tenant_id)
        key = await _service(setup, tenant=tenant_id, storage=storage).intend(
            await _row(setup, media_id), mime_type="image/png", data=PNG
        )
        await setup.commit()

    assert key is not None
    await storage.put_at(key=key, data=PNG, mime_type="image/png")
    await _age_intent(committing, media_id, seconds=GRACE + 60)

    ready = asyncio.Barrier(2)

    async def worker() -> int:
        async with committing() as session:
            reconciler = MediaUploadReconciler(session=session, storage=storage)
            await ready.wait()
            outcome = await reconciler.run(now=NOW, grace_seconds=GRACE, limit=10)
            return outcome.finalized

    first, second = await asyncio.gather(worker(), worker())

    assert first + second == 1
    async with committing() as check:
        assert (await _row(check, media_id)).storage_state is MediaStorageState.STORED
        assert await _storage_events(check, tenant_id) == 1

    await storage.delete(key)


# ============================================================ tenant isolation


async def test_reconciliation_settles_each_row_under_its_own_workspace(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """Ownership comes from the row, never from the key's prefix.

    A tenant-first key layout is layout. If reconciliation decided whose object
    something was by reading the prefix, a key could be made to name a
    workspace it does not belong to - so the tenant on the usage event, and the
    row that is settled, both come from the row that committed the intent.
    """
    async with committing() as session:
        other = Tenant(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        session.add(other)
        await session.commit()
        other_id = other.id

    try:
        async with committing() as session:
            mine = await _pending_media(session, tenant_id)
            theirs = await _pending_media(session, other_id)

        for media_id, owner in ((mine, tenant_id), (theirs, other_id)):
            async with committing() as session:
                key = await _service(session, tenant=owner, storage=storage).intend(
                    await _row(session, media_id), mime_type="image/png", data=PNG
                )
                await session.commit()
                assert key is not None
                assert key.startswith(f"{owner}/")
                await storage.put_at(key=key, data=PNG, mime_type="image/png")
            await _age_intent(committing, media_id, seconds=GRACE + 60)

        async with committing() as session:
            outcome = await MediaUploadReconciler(session=session, storage=storage).run(
                now=NOW, grace_seconds=GRACE, limit=10
            )

        assert outcome.finalized == 2
        async with committing() as check:
            assert await _storage_events(check, tenant_id) == 1
            assert await _storage_events(check, other_id) == 1
            rows = await PlatformMediaRepository(check).claimed_but_unfinished(limit=10)
            assert rows == []

        for key in storage.distinct_objects:
            await storage.delete(key)
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == other_id))
            await session.commit()


# ================================================================ the outbound seam


async def test_an_outbound_attachment_is_owned_before_it_is_written(
    committing: async_sessionmaker[AsyncSession],
    storage: RecordingStore,
    tenant_id: uuid.UUID,
) -> None:
    """The colleague-upload path follows the same protocol.

    Asserted here rather than only through the API because the property is
    about the write order, not about the route: whatever created the row, an
    object may not exist before something committed says it should.
    """
    async with committing() as setup:
        media_id = await _pending_media(setup, tenant_id)

    async with committing() as session:
        row = await _row(session, media_id)
        service = _service(session, tenant=tenant_id, storage=storage)
        key = await service.intend(row, mime_type="image/png", data=PNG)
        await session.commit()

    assert key is not None
    # Committed before anything is in the store.
    async with committing() as check:
        stored = await _row(check, media_id)
        assert stored.storage_state is MediaStorageState.PENDING
        assert stored.storage_key == key
    assert await storage.exists(key) is False
