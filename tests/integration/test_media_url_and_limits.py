"""Outbound media by URL, scoped to one object; provider limits before staging (OMNI-040, -045).

Instagram accepts video, audio and files only by URL (Instagram Messaging API,
read 2026-10-02; the final audit's I4), and Wasla's store could not issue one.
The seam now has a storage capability - a SigV4 query-signed GET for exactly one
object, ten minutes by default, an hour at most - reached only through a grant
built from one message's own stored file in its own workspace. WhatsApp keeps
uploading.

Meta's per-type limits (image JPEG/PNG 5 MB; video MP4/3GPP 16 MB; audio 16 MB;
documents 100 MB; the final audit's M18) were not capabilities, so an over-limit
file was staged, uploaded and refused by Meta. They are checked before anything
is staged.

The URL tests run against a real S3-compatible store (MinIO), as the object-store
suite does, and skip without one.

Mutants this suite kills: M-O23 (a URL for another object or workspace) and
M-O24 (the per-type limit not checked before staging).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import MediaContent, PreparedContent
from app.channels.policy import PolicyRefusalError
from app.core.config import Settings
from app.core.storage import (
    MAX_SIGNED_URL_TTL,
    MediaUrlGrant,
    SignedUrlStorage,
    StorageError,
    build_key,
)
from app.db.models.conversation import Message, MessageOrigin
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.services import messaging_service as messaging_module
from app.services.messaging_service import MessagingService
from tests.channel_fakes import SyntheticAdapter
from tests.integration.test_object_store import _s3_or_skip
from tests.integration.test_omnichannel_operations import _customer, _number, _tenant

pytestmark = pytest.mark.integration

MB = 1024 * 1024


def _png(size: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + (zlib.crc32(kind + payload).to_bytes(4, "big"))
        )

    header = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    body = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
        + chunk(b"IEND", b"")
    )
    return body + b"\x00" * max(0, size - len(body))


def _mp4(size: int) -> bytes:
    """An `ftyp` box, then one `free` box holding the rest - a well-formed MP4 of `size` bytes."""
    brands = b"isom" + b"\x00\x00\x02\x00" + b"mp42"
    ftyp = (len(brands) + 8).to_bytes(4, "big") + b"ftyp" + brands
    padding = size - len(ftyp)
    return ftyp + padding.to_bytes(4, "big") + b"free" + b"\x00" * (padding - 8)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        meta_access_token="test-access-token",
    )


class Meta:
    """Meta's media upload and messages endpoints: uploads counted, sends recorded."""

    def __init__(self) -> None:
        self.uploads = 0
        self.bodies: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media"):
                self.uploads += 1
                return httpx.Response(200, json={"id": f"upload-{self.uploads}"})
            self.bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"messages": [{"id": f"wamid.{uuid.uuid4().hex}"}]})

        return httpx.MockTransport(handle)


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> Meta:
    fake = Meta()
    monkeypatch.setattr(
        messaging_module, "build_http_client", lambda: httpx.AsyncClient(transport=fake.transport())
    )
    return fake


# ------------------------------------------------------------ signed URLs


@pytest.fixture
def store() -> SignedUrlStorage:
    storage = _s3_or_skip()
    assert isinstance(storage, SignedUrlStorage)
    return storage


async def test_a_grant_issues_a_working_url_for_its_own_object_only(
    store: SignedUrlStorage,
) -> None:
    tenant_id = uuid.uuid4()
    key = build_key(tenant_id=tenant_id, mime_type="video/mp4")
    neighbour = build_key(tenant_id=tenant_id, mime_type="video/mp4")
    await store.put_at(key=key, data=b"ours", mime_type="video/mp4")
    await store.put_at(key=neighbour, data=b"not ours", mime_type="video/mp4")

    url = await MediaUrlGrant(store, tenant_id=tenant_id, key=key).issue()

    async with httpx.AsyncClient() as client:
        fetched = await client.get(url)
        repointed = await client.get(
            url.replace(key.rsplit("/", 1)[1], neighbour.rsplit("/", 1)[1])
        )
    assert (fetched.status_code, fetched.content) == (200, b"ours")
    assert repointed.status_code == 403


async def test_a_url_stops_working_when_it_expires(store: SignedUrlStorage) -> None:
    tenant_id = uuid.uuid4()
    key = build_key(tenant_id=tenant_id, mime_type="audio/ogg")
    await store.put_at(key=key, data=b"voice", mime_type="audio/ogg")

    url = await MediaUrlGrant(store, tenant_id=tenant_id, key=key, ttl=timedelta(seconds=1)).issue()
    await asyncio.sleep(2.5)

    async with httpx.AsyncClient() as client:
        assert (await client.get(url)).status_code == 403


async def test_another_workspaces_object_or_a_malformed_key_is_refused(
    store: SignedUrlStorage,
) -> None:
    ours, theirs = uuid.uuid4(), uuid.uuid4()
    their_key = build_key(tenant_id=theirs, mime_type="application/pdf")

    with pytest.raises(StorageError):
        MediaUrlGrant(store, tenant_id=ours, key=their_key)
    with pytest.raises(StorageError):
        MediaUrlGrant(store, tenant_id=ours, key=f"{ours}/../{their_key}")
    with pytest.raises(StorageError):
        await store.signed_url(their_key, ttl=MAX_SIGNED_URL_TTL + timedelta(seconds=1))


async def test_the_url_is_never_logged_and_never_stored(
    store: SignedUrlStorage,
    db_session: AsyncSession,
    settings: Settings,
    meta: Meta,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A WhatsApp send with its copy kept, then a grant for that copy, as a URL-only
    provider would use it: the URL appears in no log line and no row."""
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    _, conversation = await _customer(db_session, tenant, account, "201000000611")
    sent = await MessagingService(
        session=db_session, settings=settings, tenant_id=tenant.id
    ).send_media(
        conversation_id=conversation.id,
        content=_png(1_000),
        mime_type="image/png",
        storage=store,
        origin=MessageOrigin.HUMAN,
    )
    stored_key = await db_session.scalar(
        text("SELECT storage_key FROM message_media WHERE message_id = :m"), {"m": sent.id}
    )
    assert stored_key is not None
    caplog.set_level(logging.DEBUG)

    adapter = SyntheticAdapter()
    grant = MediaUrlGrant(store, tenant_id=tenant.id, key=stored_key)
    async with adapter.sender(
        session=db_session, connection=_connection_like(adapter), settings=settings
    ) as sender:
        prepared = await sender.prepare(
            MediaContent(family="video", content=b"", mime_type="video/mp4", filename="clip.mp4"),
            media_url=grant,
        )

    assert prepared.reference_kind == "url"
    assert prepared.reference is not None
    assert "X-Amz-Signature" in prepared.reference
    assert prepared.reference not in caplog.text
    assert "X-Amz-Signature" not in caplog.text
    leaked = await db_session.scalar(
        text(
            "SELECT count(*) FROM messages m LEFT JOIN message_media f ON f.message_id = m.id"
            " WHERE m.tenant_id = :t AND (coalesce(m.body, '') LIKE '%X-Amz-Signature%'"
            " OR coalesce(f.storage_key, '') LIKE '%X-Amz%'"
            " OR coalesce(f.locator, '') LIKE '%X-Amz%')"
        ),
        {"t": tenant.id},
    )
    events = await db_session.scalar(
        text(
            "SELECT count(*) FROM whatsapp_events WHERE tenant_id = :t"
            " AND payload::text LIKE '%X-Amz-Signature%'"
        ),
        {"t": tenant.id},
    )
    assert (leaked, events) == (0, 0)


def _connection_like(adapter: SyntheticAdapter) -> Any:
    from app.db.models.channel import ChannelConnection

    return ChannelConnection(id=uuid.uuid4(), tenant_id=uuid.uuid4(), channel=adapter.channel)


# -------------------------------------------------- WhatsApp keeps uploading


async def test_whatsapp_prepares_a_file_by_upload_and_sends_it_unchanged(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    _, conversation = await _customer(db_session, tenant, account, "201000000612")

    await MessagingService(session=db_session, settings=settings, tenant_id=tenant.id).send_media(
        conversation_id=conversation.id,
        content=_png(1_000),
        mime_type="image/png",
        origin=MessageOrigin.HUMAN,
    )

    (body,) = [sent for sent in meta.bodies if sent.get("type") == "image"]
    assert body == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "201000000612",
        "type": "image",
        "image": {"id": "upload-1"},
    }
    assert meta.uploads == 1
    assert isinstance(PreparedContent, type)


# ------------------------------------------------------- per-type limits


async def _whatsapp_send(
    session: AsyncSession, settings: Settings, content: bytes, mime: str, phone: str
) -> tuple[Message | PolicyRefusalError, int]:
    tenant = await _tenant(session)
    account = await _number(session, tenant)
    _, conversation = await _customer(session, tenant, account, phone)
    before = await session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )
    try:
        result: Message | PolicyRefusalError = await MessagingService(
            session=session, settings=settings, tenant_id=tenant.id
        ).send_media(
            conversation_id=conversation.id,
            content=content,
            mime_type=mime,
            origin=MessageOrigin.HUMAN,
        )
    except PolicyRefusalError as refused:
        result = refused
    after = await session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )
    return result, int((after or 0) - (before or 0))


async def test_a_6_mb_png_is_refused_before_anything_is_staged(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    result, staged = await _whatsapp_send(
        db_session, settings, _png(6 * MB), "image/png", "201000000613"
    )

    assert isinstance(result, PolicyRefusalError)
    assert "at most 5 MB" in str(result)
    assert staged == 0
    assert (meta.uploads, meta.bodies) == (0, []), "nothing was uploaded or sent"


async def test_a_16_mb_mp4_goes_and_a_17_mb_one_is_refused_before_staging(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    accepted, staged = await _whatsapp_send(
        db_session, settings, _mp4(16 * MB), "video/mp4", "201000000614"
    )
    assert isinstance(accepted, Message) and staged == 1
    calls = (meta.uploads, len(meta.bodies))

    refused, staged_again = await _whatsapp_send(
        db_session, settings, _mp4(17 * MB), "video/mp4", "201000000615"
    )

    assert isinstance(refused, PolicyRefusalError)
    assert staged_again == 0
    assert (meta.uploads, len(meta.bodies)) == calls


def test_an_inbound_whatsapp_handle_records_its_seven_day_expiry() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PN"},
                            "messages": [
                                {
                                    "from": "201000000616",
                                    "id": "wamid.media",
                                    "type": "image",
                                    "timestamp": "1790000000",
                                    "image": {"id": "handle-1", "mime_type": "image/jpeg"},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }

    (event,) = WhatsAppAdapter().parse(payload).events

    (attachment,) = event.attachments
    assert attachment.expires_at is not None
    remaining = attachment.expires_at - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < remaining <= timedelta(days=7)
