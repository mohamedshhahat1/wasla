"""What a claim guarantees, at the unit that owns it (MEDIA-03, MEDIA-11).

The claim replaced the row lock that used to be held across the whole Meta
download. It has to do the lock's two jobs without holding anything: keep a
second attempt from doing the work again, and keep an attempt that has lost the
file from writing over whoever holds it now. And it carries the attempt budget,
so a file that kills its worker every time is given up on rather than tried for
ever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.storage import LocalMediaStorage
from app.db.models.media import MAX_ATTEMPTS, MediaStatus, MessageMedia
from app.services.media_horizons import claim_lease
from app.services.media_outcomes import MediaReason, text_for
from app.services.media_service import MediaService
from tests import media_harness as h
from tests.fakes import as_media_reader, as_whatsapp

pytestmark = pytest.mark.integration


def _service(
    session: AsyncSession, where: h.Scene, settings: Settings, tmp_path: Path, whatsapp: object
) -> MediaService:
    return MediaService(
        session=session,
        tenant_id=where.tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=as_whatsapp(whatsapp),
    )


def _objects(tmp_path: Path) -> list[Path]:
    return [path for path in tmp_path.rglob("*") if path.is_file()]


async def test_an_attempt_that_lost_its_claim_writes_nothing(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """While this attempt was on the wire, another took the file over - the
    recovery sweep decided it was dead and a new attempt claimed it. Coming
    back, it must not record, write or finish anything."""
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    usurper = uuid.uuid4()

    async def taken_over() -> None:
        row = await db_session.get(MessageMedia, media.id)
        assert row is not None
        row.claim_id = usurper
        row.claimed_at = datetime.now(UTC)
        await db_session.flush()

    whatsapp = h.StubWhatsApp(before_fetch=taken_over)
    outcome = await _service(db_session, where, settings, tmp_path, whatsapp).download(media)

    assert whatsapp.fetches == 1
    assert outcome.deferred
    await db_session.refresh(media)
    assert media.claim_id == usurper
    assert media.status is MediaStatus.DOWNLOADING
    assert media.storage_key is None
    assert media.last_error is None
    assert _objects(tmp_path) == []


async def test_a_reader_that_lost_its_claim_records_no_result(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    service = _service(db_session, where, settings, tmp_path, h.StubWhatsApp())
    assert (await service.download(media)).status is MediaStatus.STORED
    usurper = uuid.uuid4()

    class TakenOverWhileReading:
        async def read(self, *, content: bytes, mime_type: str | None) -> object:
            row = await db_session.get(MessageMedia, media.id)
            assert row is not None
            row.claim_id = usurper
            await db_session.flush()
            return await h.StubReader().read(content=content, mime_type=mime_type)

    outcome = await service.understand(media, reader=as_media_reader(TakenOverWhileReading()))

    assert outcome.deferred
    await db_session.refresh(media)
    assert media.status is MediaStatus.STORED
    assert media.transcript is None
    assert media.claim_id == usurper


async def test_a_live_claim_held_elsewhere_makes_a_second_attempt_stand_aside(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    media.claim_id = uuid.uuid4()
    media.claimed_at = datetime.now(UTC)
    media.status = MediaStatus.DOWNLOADING
    media.attempts = 1
    await db_session.flush()
    whatsapp = h.StubWhatsApp()

    outcome = await _service(db_session, where, settings, tmp_path, whatsapp).download(media)

    assert outcome.deferred
    assert (whatsapp.probes, whatsapp.fetches) == (0, 0)
    await db_session.refresh(media)
    assert media.attempts == 1


async def test_an_expired_claim_is_taken_over_and_counted(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    media.claim_id = uuid.uuid4()
    media.claimed_at = datetime.now(UTC) - claim_lease(settings) - claim_lease(settings)
    media.status = MediaStatus.DOWNLOADING
    media.attempts = 1
    await db_session.flush()

    outcome = await _service(db_session, where, settings, tmp_path, h.StubWhatsApp()).download(
        media
    )

    assert outcome.status is MediaStatus.STORED
    await db_session.refresh(media)
    assert media.attempts == 2


async def test_a_file_that_has_used_every_attempt_is_given_up_without_another(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """Three attempts that never finished - a file that kills its worker every
    time - and the next claim ends it rather than trying a fourth."""
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    media.attempts = MAX_ATTEMPTS
    await db_session.flush()
    whatsapp = h.StubWhatsApp()

    outcome = await _service(db_session, where, settings, tmp_path, whatsapp).download(media)

    assert outcome.reason is MediaReason.ABANDONED
    assert media.last_error == text_for(MediaReason.ABANDONED)
    assert (whatsapp.probes, whatsapp.fetches) == (0, 0)
