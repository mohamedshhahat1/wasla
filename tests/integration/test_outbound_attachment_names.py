"""An outbound attachment's name is settled before anything is sent (MEDIA-06).

The audit's P4-02: a name of 301 characters, or one carrying a NUL, reached
Meta - the customer received the file - and then failed to record. The request
errored, the message stayed `pending`, and the attachment row was lost; retried
without an idempotency key, the file went out twice.

Against the real database and the real `WhatsAppClient`, over a transport that
records every call Meta would receive, so "nothing was sent" is counted rather
than assumed. Control characters are built with `chr()` so this file stays
plain text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.core.filenames import MAX_FILENAME_LENGTH, UnstorableFilenameError
from app.core.storage import LocalMediaStorage
from app.db.models.conversation import Message, MessageOrigin, MessageStatus
from app.db.models.media import MessageMedia
from app.services import messaging_service as messaging_module
from app.services.messaging_service import MessagingService
from tests import media_harness as h

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class Meta:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.upload_names: list[bytes] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.calls.append(request.url.path.rsplit("/", 1)[-1])
            if request.url.path.endswith("/media"):
                self.upload_names.append(request.content)
                return httpx.Response(200, json={"id": "meta-media-id"})
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": "201234567890"}],
                    "messages": [{"id": "wamid.out.1"}],
                },
            )

        return httpx.MockTransport(handle)


async def _ready(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> tuple[h.Scene, Meta, MessagingService]:
    where = await h.scene(db_session)
    where.conversation.last_inbound_at = datetime.now(UTC)
    await db_session.flush()
    meta = Meta()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=meta.transport()),
    )
    configured = settings.model_copy(update={"meta_access_token": "SENTINEL-OUTBOUND"})
    service = MessagingService(session=db_session, settings=configured, tenant_id=where.tenant.id)
    return where, meta, service


async def _counts(db_session: AsyncSession, where: h.Scene) -> tuple[int, int]:
    messages = (
        await db_session.execute(
            select(func.count()).select_from(Message).where(Message.tenant_id == where.tenant.id)
        )
    ).scalar_one()
    media = (
        await db_session.execute(
            select(func.count())
            .select_from(MessageMedia)
            .where(MessageMedia.tenant_id == where.tenant.id)
        )
    ).scalar_one()
    return int(messages), int(media)


@pytest.mark.parametrize(
    "filename",
    [
        "b" * (MAX_FILENAME_LENGTH - 3) + ".pdf",
        "b" * 2_000 + ".pdf",
        "contract" + chr(0) + ".pdf",
        "contract" + chr(7) + ".pdf",
    ],
)
async def test_a_name_that_cannot_be_stored_is_refused_before_meta_is_asked(
    db_session: AsyncSession,
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    where, meta, service = await _ready(db_session, monkeypatch, settings)

    with pytest.raises(UnstorableFilenameError) as raised:
        await service.send_media(
            conversation_id=where.conversation.id,
            content=PDF,
            mime_type="application/pdf",
            filename=filename,
            origin=MessageOrigin.HUMAN,
            storage=LocalMediaStorage(tmp_path),
        )

    # A 422 to the colleague, and nothing anywhere else.
    assert isinstance(raised.value, ValidationError)
    assert raised.value.status_code == 422
    assert meta.calls == []
    assert await _counts(db_session, where) == (0, 0)
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize(
    "filename",
    [
        "b" * (MAX_FILENAME_LENGTH - 4) + ".pdf",
        "عقد" + ".pdf",
        "  quote.pdf  ",
    ],
)
async def test_a_storable_name_is_sent_and_recorded_in_one_canonical_form(
    db_session: AsyncSession,
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    where, meta, service = await _ready(db_session, monkeypatch, settings)

    message = await service.send_media(
        conversation_id=where.conversation.id,
        content=PDF,
        mime_type="application/pdf",
        filename=filename,
        origin=MessageOrigin.HUMAN,
        storage=LocalMediaStorage(tmp_path),
    )

    assert message.status is MessageStatus.SENT
    assert meta.calls == ["media", "messages"]
    row = (
        await db_session.execute(select(MessageMedia).where(MessageMedia.message_id == message.id))
    ).scalar_one()
    assert row.filename == filename.strip()
    assert len(row.filename or "") <= MAX_FILENAME_LENGTH
