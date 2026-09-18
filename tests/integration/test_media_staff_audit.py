"""A colleague opening or sending a customer's file leaves an audit entry
(MEDIA-17, PD-MEDIA-06).

Against the real database, because what matters is the row that lands in
`audit_logs`: which action, which actor, which workspace, and - as much - what
it does *not* carry. The filename, caption and object key used here are
sentinels, and none of them may appear anywhere in the entry.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.core.storage import LocalMediaStorage
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.conversation import MessageOrigin
from app.db.models.media import MediaStorageState
from app.db.models.user import User
from app.services import messaging_service as messaging_module
from app.services.media_service import MediaService
from app.services.messaging_service import MessagingService
from tests import media_harness as h

pytestmark = pytest.mark.integration

FILENAME = "SENTINEL-FILENAME-contract-9d1.pdf"
CAPTION = "SENTINEL-CAPTION-3b7"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


async def _colleague(session: AsyncSession) -> User:
    user = User(email=f"colleague-{uuid.uuid4().hex[:6]}@example.com", is_active=True)
    session.add(user)
    await session.flush()
    return user


async def _entries(session: AsyncSession, tenant_id: uuid.UUID) -> list[AuditLog]:
    return list(
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.tenant_id == tenant_id,
                    AuditLog.action.in_([AuditAction.MEDIA_DOWNLOADED, AuditAction.MEDIA_SENT]),
                )
            )
        ).scalars()
    )


def _everything(entry: AuditLog) -> str:
    return json.dumps(
        {
            "meta": entry.meta,
            "target_label": entry.target_label,
            "target_type": entry.target_type,
        },
        default=str,
    )


async def test_a_colleague_opening_a_file_is_audited_with_identifiers_only(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where, filename=FILENAME)
    media.storage_key = f"{where.tenant.id}/2026/09/{uuid.uuid4()}.pdf"
    media.storage_state = MediaStorageState.STORED
    await db_session.flush()
    colleague = await _colleague(db_session)
    service = MediaService(
        session=db_session,
        tenant_id=where.tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
    )

    service.record_colleague_download(media, actor=colleague)
    await db_session.flush()

    (entry,) = await _entries(db_session, where.tenant.id)
    assert entry.action is AuditAction.MEDIA_DOWNLOADED
    assert entry.actor_id == colleague.id
    assert entry.actor_kind is AuditActorKind.USER
    assert entry.target_id == media.id
    assert entry.meta == {
        "conversation_id": str(media.conversation_id),
        "message_id": str(media.message_id),
    }
    recorded = _everything(entry)
    assert FILENAME not in recorded
    assert media.storage_key not in recorded


async def test_another_workspaces_file_cannot_be_opened_or_audited(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """The lookup refuses before anything is recorded, in either trail."""
    mine = await h.scene(db_session, slug="mine")
    theirs = await h.scene(db_session, slug="theirs")
    their_file = await h.attachment(db_session, theirs, filename=FILENAME)
    service = MediaService(
        session=db_session,
        tenant_id=mine.tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
    )

    with pytest.raises(NotFoundError):
        await service.get(their_file.id)

    assert await _entries(db_session, mine.tenant.id) == []
    assert await _entries(db_session, theirs.tenant.id) == []


def _meta_transport(status: int = 200) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "meta-media-id"})
        if status != 200:
            return httpx.Response(status, json={"error": {"code": 100}})
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"wa_id": "201234567890"}],
                "messages": [{"id": f"wamid.out.{uuid.uuid4().hex[:6]}"}],
            },
        )

    return httpx.MockTransport(handle)


async def _send(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    *,
    origin: MessageOrigin = MessageOrigin.HUMAN,
    status: int = 200,
) -> tuple[h.Scene, User]:
    where = await h.scene(db_session)
    where.conversation.last_inbound_at = datetime.now(UTC)
    colleague = await _colleague(db_session)
    await db_session.flush()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=_meta_transport(status)),
    )
    service = MessagingService(
        session=db_session,
        settings=settings.model_copy(update={"meta_access_token": "SENTINEL-OUT"}),
        tenant_id=where.tenant.id,
    )
    await service.send_media(
        conversation_id=where.conversation.id,
        content=PDF,
        mime_type="application/pdf",
        filename=FILENAME,
        caption=CAPTION,
        origin=origin,
        sent_by_id=colleague.id,
        storage=LocalMediaStorage(tmp_path),
    )
    return where, colleague


async def test_a_colleague_sending_a_file_is_audited_with_identifiers_only(
    db_session: AsyncSession,
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    where, colleague = await _send(db_session, monkeypatch, settings, tmp_path)

    (entry,) = await _entries(db_session, where.tenant.id)
    assert entry.action is AuditAction.MEDIA_SENT
    assert entry.actor_id == colleague.id
    assert set(entry.meta or {}) == {"conversation_id", "message_id", "media_id"}
    recorded = _everything(entry)
    assert FILENAME not in recorded
    assert CAPTION not in recorded


async def test_a_send_meta_refused_delivered_nothing_and_is_not_audited(
    db_session: AsyncSession,
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    where, _ = await _send(db_session, monkeypatch, settings, tmp_path, status=400)

    assert await _entries(db_session, where.tenant.id) == []


async def test_the_media_worker_reading_a_file_is_not_a_colleague_access(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)

    await h.run(h.worker(db_session, tmp_path, settings, reader=h.StubReader()), media)

    assert await _entries(db_session, where.tenant.id) == []
