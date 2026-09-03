"""Retention: the file goes, the record stays, and a failure is recoverable.

Nothing ever deleted a stored file. `MediaStorage.delete` existed and no caller
invoked it, so the volume grew monotonically - a disk-full incident with no
warning that takes the API and the worker down together, because they share it
(BUG-006).

Every test here names a fixed instant. A sweep is a comparison against a clock,
and a test that read the real one would be asserting a boundary that moves while
it runs - which on Windows in particular is a test that fails roughly one time
in three and never on CI.

The failure cases matter more than the happy path, because removing an object
and clearing the column that points at it are writes to two systems that no
transaction spans. What is asserted is that **every** interruption leaves a
state the next pass finishes.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.object_store import S3MediaStorage
from app.core.storage import LocalMediaStorage, MediaStorage, StorageError
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services.media_retention_service import MediaRetentionService, purge_reason

pytestmark = pytest.mark.integration

# A fixed instant, and every age below is expressed relative to it.
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
RETENTION_DAYS = 30
PNG = b"\x89PNG\r\n\x1a\n" + b"a stored photograph" * 8


@pytest.fixture(params=["local", "s3"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> MediaStorage:
    if request.param == "local":
        return LocalMediaStorage(tmp_path)
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


class RefusingStore:
    """A store that will not delete, which is the failure the sweep is built for."""

    def __init__(self, inner: MediaStorage) -> None:
        self._inner = inner
        self.delete_attempts = 0

    async def put(self, *, tenant_id: uuid.UUID, data: bytes, mime_type: str | None = None) -> str:
        return await self._inner.put(tenant_id=tenant_id, data=data, mime_type=mime_type)

    async def get(self, key: str) -> bytes:
        return await self._inner.get(key)

    async def delete(self, key: str) -> None:
        self.delete_attempts += 1
        raise StorageError()


class CountingStore:
    """Records deletions so idempotence can be asserted rather than assumed."""

    def __init__(self, inner: MediaStorage) -> None:
        self._inner = inner
        self.deleted: list[str] = []

    async def put(self, *, tenant_id: uuid.UUID, data: bytes, mime_type: str | None = None) -> str:
        return await self._inner.put(tenant_id=tenant_id, data=data, mime_type=mime_type)

    async def get(self, key: str) -> bytes:
        return await self._inner.get(key)

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        await self._inner.delete(key)


async def _tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _stored_file(
    session: AsyncSession,
    tenant: Tenant,
    storage: MediaStorage,
    *,
    age_days: float,
    transcript: str = "A blue sofa.",
) -> MessageMedia:
    """One attachment, stored, with its row aged by hand.

    `created_at` is set explicitly rather than by waiting: the column has a
    server default, so the row is flushed and then moved, which is the only way
    to place a file on either side of a boundary in a test that takes
    milliseconds.
    """
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"2012{uuid.uuid4().int % 10**8:08d}")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(tenant_id=tenant.id, contact_id=contact.id, account_id=account.id)
    session.add(conversation)
    await session.flush()

    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.IMAGE,
        status=MessageStatus.DELIVERED,
    )
    session.add(message)
    await session.flush()

    key = await storage.put(tenant_id=tenant.id, data=PNG, mime_type="image/png")
    assert key is not None
    media = MessageMedia(
        tenant_id=tenant.id,
        message_id=message.id,
        conversation_id=conversation.id,
        mime_type="image/png",
        status=MediaStatus.READY,
        storage_key=key,
        byte_size=len(PNG),
        transcript=transcript,
    )
    session.add(media)
    await session.flush()

    media.created_at = NOW - timedelta(days=age_days)
    await session.flush()
    return media


def _service(session: AsyncSession, storage: MediaStorage) -> MediaRetentionService:
    return MediaRetentionService(session=session, storage=storage)


# ================================================================ eligibility


async def test_a_file_past_its_retention_is_removed(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    tenant = await _tenant(db_session)
    media = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 1)
    key = media.storage_key
    assert key is not None

    outcome = await _service(db_session, storage).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=100
    )

    assert outcome.purged == 1
    assert outcome.failed == 0
    assert media.storage_key is None
    with pytest.raises(StorageError):
        await storage.get(key)


async def test_a_recent_file_is_untouched(db_session: AsyncSession, storage: MediaStorage) -> None:
    """The other half, and the one a wrong comparison breaks silently."""
    tenant = await _tenant(db_session)
    media = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS - 1)
    key = media.storage_key
    assert key is not None

    outcome = await _service(db_session, storage).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=100
    )

    assert outcome.claimed == 0
    assert media.storage_key == key
    assert media.purge_started_at is None
    assert await storage.get(key) == PNG


async def test_old_and_recent_files_in_one_pass(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """Both together, because a sweep that took everything would pass both above."""
    tenant = await _tenant(db_session)
    old = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 5)
    recent = await _stored_file(db_session, tenant, storage, age_days=1)
    recent_key = recent.storage_key
    assert recent_key is not None

    outcome = await _service(db_session, storage).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=100
    )

    assert outcome.purged == 1
    assert old.storage_key is None
    assert recent.storage_key == recent_key
    assert await storage.get(recent_key) == PNG


async def test_a_file_exactly_at_the_boundary_is_kept(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """A retention of thirty days means thirty days, not twenty-nine and a bit."""
    tenant = await _tenant(db_session)
    media = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS)

    outcome = await _service(db_session, storage).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=100
    )

    assert outcome.claimed == 0
    assert media.storage_key is not None


async def test_retention_of_zero_deletes_nothing(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """The default. A deployment that has not chosen a period keeps everything."""
    tenant = await _tenant(db_session)
    media = await _stored_file(db_session, tenant, storage, age_days=3650)

    outcome = await _service(db_session, storage).sweep(now=NOW, retention_days=0, limit=100)

    assert outcome == type(outcome)()
    assert media.storage_key is not None


async def test_the_batch_bounds_one_pass(db_session: AsyncSession, storage: MediaStorage) -> None:
    """A deployment with a backlog takes several passes, not one huge transaction."""
    tenant = await _tenant(db_session)
    for _ in range(3):
        await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 10)

    first = await _service(db_session, storage).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=2
    )
    assert first.purged == 2

    second = await _service(db_session, storage).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=2
    )
    assert second.purged == 1


# =========================================================== the record stays


async def test_the_transcript_survives_the_file(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """Retention removes the bytes, never the record of the conversation.

    `transcript` is what the agent was shown and what a colleague reading the
    thread sees. Deleting it would rewrite what happened.
    """
    tenant = await _tenant(db_session)
    media = await _stored_file(
        db_session, tenant, storage, age_days=RETENTION_DAYS + 1, transcript="A price list."
    )

    await _service(db_session, storage).sweep(now=NOW, retention_days=RETENTION_DAYS, limit=100)

    assert media.transcript == "A price list."
    assert media.mime_type == "image/png"
    assert media.byte_size == len(PNG)


async def test_a_purged_row_can_explain_itself(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """ "Removed by retention" and "the store is broken" are different sentences."""
    tenant = await _tenant(db_session)
    media = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 1)

    assert media.is_purged is False
    assert purge_reason(media) is None

    await _service(db_session, storage).sweep(now=NOW, retention_days=RETENTION_DAYS, limit=100)

    purged: bool = media.is_purged
    assert purged is True
    assert "retention" in (purge_reason(media) or "")


# ==================================================== failure and idempotence


async def test_a_store_that_refuses_leaves_the_row_recoverable(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """The failure this whole design exists for.

    The claim is committed before the object is touched, so a refused deletion
    leaves a row that says what it was doing - claimed, key intact - rather than
    a row pointing confidently at a file that may or may not be there.
    """
    tenant = await _tenant(db_session)
    media = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 1)
    key = media.storage_key
    assert key is not None
    refusing = RefusingStore(storage)

    outcome = await _service(db_session, refusing).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=100
    )

    assert outcome.failed == 1
    assert outcome.purged == 0
    assert media.storage_key == key, "the key was cleared for an object still in the store"
    assert media.purge_started_at is not None
    assert await storage.get(key) == PNG


async def test_the_next_pass_finishes_what_a_refusal_left(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    tenant = await _tenant(db_session)
    media = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 1)
    key = media.storage_key
    assert key is not None

    await _service(db_session, RefusingStore(storage)).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=100
    )
    recovered = await _service(db_session, storage).sweep(
        now=NOW + timedelta(days=1), retention_days=RETENTION_DAYS, limit=100
    )

    assert recovered.purged == 1
    assert recovered.already_claimed == 1
    assert media.storage_key is None
    with pytest.raises(StorageError):
        await storage.get(key)


async def test_a_claimed_row_is_finished_even_when_retention_is_raised(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """The case the age query cannot see.

    Raise the retention period after a failed pass and the ordinary sweep stops
    selecting the rows it left half-done. Without reconciliation they stay
    claimed for ever with their objects still in the store, and nothing anywhere
    is looking for them.
    """
    tenant = await _tenant(db_session)
    media = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 1)
    key = media.storage_key
    assert key is not None

    await _service(db_session, RefusingStore(storage)).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=100
    )
    assert media.storage_key == key

    # A far longer period: this file is no longer due by age.
    later = await _service(db_session, storage).sweep(now=NOW, retention_days=3650, limit=100)
    assert later.claimed == 0
    assert media.storage_key == key

    finished = await _service(db_session, storage).reconcile(limit=100)

    assert finished == 1
    assert media.storage_key is None


async def test_purging_twice_is_not_an_error(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """A retried pass must be able to delete the same object again."""
    tenant = await _tenant(db_session)
    media = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 1)
    counting = CountingStore(storage)
    service = _service(db_session, counting)

    await service.sweep(now=NOW, retention_days=RETENTION_DAYS, limit=100)
    assert len(counting.deleted) == 1

    # The row is terminal now, so a second purge touches the store at all.
    assert await service.purge(media) is True
    assert len(counting.deleted) == 1

    again = await service.sweep(now=NOW, retention_days=RETENTION_DAYS, limit=100)
    assert again.claimed == 0


async def test_a_partly_failed_pass_still_removes_what_it_can(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """One store failure must not strand every other workspace's file behind it."""
    tenant = await _tenant(db_session)
    first = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 2)
    second = await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 1)

    class RefusesTheFirst(CountingStore):
        async def delete(self, key: str) -> None:
            if key == first.storage_key:
                raise StorageError()
            await super().delete(key)

    outcome = await _service(db_session, RefusesTheFirst(storage)).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=100
    )

    assert outcome.purged == 1
    assert outcome.failed == 1
    assert first.storage_key is not None
    assert second.storage_key is None


async def test_pending_counts_only_unfinished_claims(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """The number an operator alerts on.

    A store refusing deletions is otherwise invisible: rows are claimed, the
    sweep reports itself as having run, and the volume does not shrink.
    """
    tenant = await _tenant(db_session)
    await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 1)
    service = _service(db_session, storage)

    assert await service.pending_count() == 0

    await _service(db_session, RefusingStore(storage)).sweep(
        now=NOW, retention_days=RETENTION_DAYS, limit=100
    )
    assert await service.pending_count() == 1

    await service.reconcile(limit=100)
    assert await service.pending_count() == 0


# ================================================ retention meets the worker


async def test_a_replayed_media_job_does_not_undo_a_purge(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """A null `storage_key` used to mean one thing and now means two.

    Before retention it meant "not downloaded yet". It now also means "the file
    was removed", and a media job replayed after a purge would otherwise ask
    Meta for a handle that expired months ago, fail, and flip a READY row with
    a good transcript to FAILED - destroying the record of what the file said
    to re-fetch a file the workspace asked to have deleted.
    """
    from app.core.config import Settings
    from app.services.media_service import MediaService

    tenant = await _tenant(db_session)
    media = await _stored_file(
        db_session, tenant, storage, age_days=RETENTION_DAYS + 1, transcript="A price list."
    )
    media.wa_media_id = "meta-handle-long-expired"
    await db_session.flush()

    await _service(db_session, storage).sweep(now=NOW, retention_days=RETENTION_DAYS, limit=100)
    assert media.is_purged

    class ExplodingWhatsApp:
        async def probe_media(self, media_id: str) -> None:
            raise AssertionError("a purged file was re-fetched from Meta")

        async def fetch_media(self, media_id: str, *, max_bytes: int) -> None:
            raise AssertionError("a purged file was re-fetched from Meta")

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=Settings(
            _env_file=None,
            environment="test",
            log_format="console",
            log_level="WARNING",
            cors_origins=[],
        ),
        storage=storage,
        whatsapp=ExplodingWhatsApp(),  # type: ignore[arg-type]
    )

    outcome = await service.download(media)

    assert outcome.status is MediaStatus.READY
    assert media.status is MediaStatus.READY
    assert media.transcript == "A price list."
    assert media.storage_key is None


async def test_reading_a_purged_file_is_not_attempted(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """The transcript is the answer, and the bytes are gone anyway."""
    from app.core.config import Settings
    from app.services.media_service import MediaService

    tenant = await _tenant(db_session)
    media = await _stored_file(
        db_session, tenant, storage, age_days=RETENTION_DAYS + 1, transcript="A price list."
    )
    await _service(db_session, storage).sweep(now=NOW, retention_days=RETENTION_DAYS, limit=100)

    class ExplodingReader:
        async def read(self, *, content: bytes, mime_type: str | None) -> None:
            raise AssertionError("a purged file was handed to the reader")

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=Settings(
            _env_file=None,
            environment="test",
            log_format="console",
            log_level="WARNING",
            cors_origins=[],
        ),
        storage=storage,
    )

    outcome = await service.understand(media, reader=ExplodingReader())  # type: ignore[arg-type]

    assert outcome.status is MediaStatus.READY
    assert media.transcript == "A price list."


async def test_a_sweep_with_no_clock_of_its_own_counts_honestly(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """`sweep()` without a `now` must stamp and compare the same instant.

    Reading the clock twice - once to claim and once to count - makes every row
    look like one an earlier pass had left behind, which is precisely the number
    an operator is told to alert on.
    """
    tenant = await _tenant(db_session)
    await _stored_file(db_session, tenant, storage, age_days=RETENTION_DAYS + 1)

    outcome = await _service(db_session, storage).sweep(retention_days=RETENTION_DAYS, limit=100)

    assert outcome.claimed == 1
    assert outcome.purged == 1
    assert outcome.already_claimed == 0, "a freshly claimed row was counted as resumed"


# ============================================================ still isolated


async def test_a_sweep_never_touches_a_file_it_was_not_due_to(
    db_session: AsyncSession, storage: MediaStorage
) -> None:
    """The sweep runs across every workspace, so it must select by date alone.

    A bug that widened the query would delete another workspace's current
    attachments, and it would look exactly like a working sweep.
    """
    keeping = await _tenant(db_session)
    expiring = await _tenant(db_session)
    theirs = await _stored_file(db_session, keeping, storage, age_days=1)
    ours = await _stored_file(db_session, expiring, storage, age_days=RETENTION_DAYS + 1)
    theirs_key = theirs.storage_key
    assert theirs_key is not None

    await _service(db_session, storage).sweep(now=NOW, retention_days=RETENTION_DAYS, limit=100)

    assert ours.storage_key is None
    assert theirs.storage_key == theirs_key
    assert await storage.get(theirs_key) == PNG
