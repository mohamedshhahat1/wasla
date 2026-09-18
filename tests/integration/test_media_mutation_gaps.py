"""Direct tests for guarantees the audit's mutations showed nothing held
(MEDIA-18: M10, M12, M13, M26).

Each property was already right in the code; what was missing was a test that
fails when it stops being right. They are tested here at the unit that owns
them, against the real database, rather than only through happy paths that
happen to pass either way.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.storage import LocalMediaStorage, build_key
from app.db.models.billing import BillingInterval, LimitKey, Plan, SubscriptionStatus
from app.db.models.media import MediaStatus, MediaStorageState, MessageMedia
from app.db.models.usage import UsageEvent, UsageEventType
from app.integrations.whatsapp.client import REQUEST_TIMEOUT_SECONDS, build_http_client
from app.repositories.billing_repository import SubscriptionRepository
from app.services.media_outcomes import MediaReason, text_for
from app.services.media_service import MediaService, content_hash
from tests import media_harness as h
from tests.fakes import as_whatsapp

pytestmark = pytest.mark.integration

PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 64
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 64


def _service(
    session: AsyncSession, where: h.Scene, settings: Settings, tmp_path: Path, **kwargs: object
) -> MediaService:
    return MediaService(
        session=session,
        tenant_id=where.tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        **kwargs,  # type: ignore[arg-type]
    )


async def _pending_intent(
    session: AsyncSession, media: MessageMedia, where: h.Scene, data: bytes
) -> str:
    """A committed intent from an earlier attempt: key, size and hash of `data`."""
    key = build_key(tenant_id=where.tenant.id, mime_type="image/png")
    media.storage_key = key
    media.storage_state = MediaStorageState.PENDING
    media.upload_started_at = datetime.now(UTC)
    media.byte_size = len(data)
    media.content_hash = content_hash(data)
    media.mime_type = "image/png"
    await session.flush()
    return key


# ------------------------------------------------------------------- M10


def test_the_media_http_client_has_a_finite_timeout_on_every_phase() -> None:
    """M10. The per-read timeouts are the floor under the total deadline."""
    client = build_http_client()
    timeout = client.timeout
    assert isinstance(timeout, httpx.Timeout)
    for phase in (timeout.connect, timeout.read, timeout.write, timeout.pool):
        assert phase == REQUEST_TIMEOUT_SECONDS


# ------------------------------------------------------------------- M12


async def test_a_retry_with_different_bytes_cannot_reuse_the_pending_key(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """M12. The earlier attempt committed key K for bytes A. A later attempt
    arriving with bytes B must not be handed K: writing B there would replace
    an object reconciliation may be verifying against A's hash."""
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    key = await _pending_intent(db_session, media, where, PNG_A)
    service = _service(db_session, where, settings, tmp_path)

    assert await service.intend(media, mime_type="image/png", data=PNG_B) is None

    await db_session.refresh(media)
    assert media.storage_key == key
    assert media.content_hash == content_hash(PNG_A)
    assert media.byte_size == len(PNG_A)
    assert media.storage_state is MediaStorageState.PENDING


async def test_a_retry_with_the_same_bytes_reuses_the_pending_key(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """The control for M12: the conflict is about different bytes, not any retry."""
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    key = await _pending_intent(db_session, media, where, PNG_A)

    reused = await _service(db_session, where, settings, tmp_path).intend(
        media, mime_type="image/png", data=PNG_A
    )

    assert reused == key


async def test_a_download_whose_bytes_conflict_with_the_intent_writes_nothing(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    await _pending_intent(db_session, media, where, PNG_A)
    whatsapp = h.StubWhatsApp(content=PNG_B, mime_type="image/png")

    outcome = await _service(
        db_session, where, settings, tmp_path, whatsapp=as_whatsapp(whatsapp)
    ).download(media)

    assert outcome.reason is MediaReason.UPLOAD_CONFLICT
    assert media.last_error == text_for(MediaReason.UPLOAD_CONFLICT)
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


# ------------------------------------------------------------------- M13


async def _storage_meters(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(UsageEvent)
                .where(
                    UsageEvent.tenant_id == tenant_id,
                    UsageEvent.event_type == UsageEventType.STORAGE_USED,
                )
            )
        ).scalar_one()
    )


@pytest.mark.parametrize(
    "settled_as",
    [MediaStorageState.STORED, MediaStorageState.MISMATCHED, MediaStorageState.ABSENT],
)
async def test_a_stale_finaliser_cannot_convert_a_row_somebody_else_settled(
    db_session: AsyncSession, tmp_path: Path, settings: Settings, settled_as: MediaStorageState
) -> None:
    """M13. Reconciliation (or a faster duplicate) settled the intent while this
    attempt was writing. Finalising anyway would meter the bytes twice, or turn
    a quarantined object into a served one."""
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    key = await _pending_intent(db_session, media, where, PNG_A)

    media.storage_state = settled_as
    if settled_as is MediaStorageState.ABSENT:
        media.storage_key = None
    await db_session.flush()
    meters = await _storage_meters(db_session, where.tenant.id)

    await _service(db_session, where, settings, tmp_path).finalize(media, key=key)

    await db_session.refresh(media)
    assert media.storage_state is settled_as
    assert await _storage_meters(db_session, where.tenant.id) == meters


async def test_a_finaliser_for_another_key_cannot_settle_the_row(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    await _pending_intent(db_session, media, where, PNG_A)
    other = build_key(tenant_id=where.tenant.id, mime_type="image/png")

    await _service(db_session, where, settings, tmp_path).finalize(media, key=other)

    await db_session.refresh(media)
    assert media.storage_state is MediaStorageState.PENDING
    assert await _storage_meters(db_session, where.tenant.id) == 0


# ------------------------------------------------------------------- M26


async def _capped(session: AsyncSession, where: h.Scene, *, limit_bytes: int) -> None:
    plan = Plan(
        code=f"cap-{uuid.uuid4().hex[:8]}",
        name="Capacity",
        price=Decimal("10.00"),
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits={LimitKey.STORAGE_BYTES.value: limit_bytes},
    )
    session.add(plan)
    await session.flush()
    now = datetime.now(UTC)
    SubscriptionRepository(session, tenant_id=where.tenant.id).create(
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now - timedelta(days=5),
        current_period_end=now + timedelta(days=25),
    )
    await session.flush()


async def test_an_inbound_file_past_the_workspaces_capacity_is_not_stored(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """M26, through the real worker. A workspace already at its storage cap
    receives another attachment: no object is written, the row says why, and
    the conversation is answered rather than held."""
    where = await h.scene(db_session)
    await _capped(db_session, where, limit_bytes=100)
    already = await h.attachment(db_session, where)
    already.storage_key = build_key(tenant_id=where.tenant.id, mime_type="image/png")
    already.storage_state = MediaStorageState.STORED
    already.status = MediaStatus.READY
    already.byte_size = 90
    await db_session.flush()

    incoming = await h.attachment(db_session, where)
    worker = h.worker(
        db_session,
        tmp_path,
        settings,
        whatsapp=h.StubWhatsApp(content=PNG_A),
        reader=h.StubReader(),
    )

    job = await h.run(worker, incoming)

    await db_session.refresh(incoming)
    assert incoming.status is MediaStatus.SKIPPED
    assert incoming.last_error == text_for(MediaReason.CAPACITY)
    assert incoming.storage_state is MediaStorageState.ABSENT
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    assert job is not None


async def test_an_inbound_file_inside_the_capacity_is_stored(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """The control: the same workspace with room left stores the file."""
    where = await h.scene(db_session)
    await _capped(db_session, where, limit_bytes=10_000)
    incoming = await h.attachment(db_session, where)
    worker = h.worker(
        db_session,
        tmp_path,
        settings,
        whatsapp=h.StubWhatsApp(content=PNG_A),
        reader=h.StubReader(),
    )

    await h.run(worker, incoming)

    await db_session.refresh(incoming)
    assert incoming.status is MediaStatus.READY
    assert incoming.storage_state is MediaStorageState.STORED
