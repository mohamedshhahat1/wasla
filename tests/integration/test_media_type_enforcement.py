"""SEC-09 at the two boundaries, against PostgreSQL and a mocked Meta.

`tests/unit/test_media_types.py` proves the detector. This proves the *system*
uses it: that a spoofed file is refused where it enters, that what reaches Meta
and what lands in the database are the canonical type rather than the claim, and
that the same rule applies to a file arriving from Meta as to one a colleague
uploads.

Nothing here stubs `MessagingService` or `MediaService`. The real services run,
with only the HTTP boundary faked, because a stub of the thing under test would
assert the stub.
"""

from __future__ import annotations

import io
import zipfile
import zlib
from datetime import UTC, datetime

import httpx
import pytest

from app.core.config import Settings
from app.core.media_types import MediaTypeError
from app.core.storage import LocalMediaStorage
from app.db.models.conversation import Contact, Conversation, MessageKind, MessageStatus
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp.client import DownloadedMedia, MediaDescriptor
from app.services import messaging_service as messaging_module
from app.services.media_service import MediaService
from app.services.messaging_service import MessagingService

pytestmark = pytest.mark.integration

PHONE_NUMBER_ID = "109876543210"
WABA_ID = "555000111"
DISPLAY_NUMBER = "+201000000000"
CUSTOMER = "201234567890"
WAMID = "wamid.media.sent"


def png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + zlib.crc32(kind + payload).to_bytes(4, "big")
        )

    header = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
        + chunk(b"IEND", b"")
    )


PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<html><body><script>alert(document.cookie)</script></body></html>"
OGG = b"OggS\x00\x02" + b"\x00" * 24 + b"OpusHead"


def archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("payload.exe", "MZ")
    return buffer.getvalue()


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


class Recorder:
    """Captures the outbound request and answers as Meta would."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path.endswith("/media"):
                return httpx.Response(200, json={"id": "meta-media-id"})
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": CUSTOMER}],
                    "messages": [{"id": WAMID}],
                },
            )

        return httpx.MockTransport(handle)

    @property
    def upload(self) -> httpx.Request:
        return next(r for r in self.requests if r.url.path.endswith("/media"))


@pytest.fixture
def meta(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=recorder.transport()),
    )
    return recorder


async def _conversation(session):
    tenant = Tenant(name="Acme", slug="acme-media-types")
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=PHONE_NUMBER_ID,
        waba_id=WABA_ID,
        display_phone_number=DISPLAY_NUMBER,
    )
    contact = Contact(tenant_id=tenant.id, wa_id=CUSTOMER)
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        last_inbound_at=datetime.now(UTC),
    )
    session.add(conversation)
    await session.flush()
    return tenant, conversation


# ================================================ outbound: a colleague uploads


@pytest.mark.parametrize(
    ("claimed", "content", "label"),
    [
        ("image/jpeg", PDF, "the hard gate: a PDF sent as a photograph"),
        ("image/png", HTML, "an HTML page sent as an image"),
        ("image/svg+xml", SVG, "the type the image/* wildcard used to admit"),
        ("audio/mpeg", PDF, "a PDF sent as a recording"),
        ("application/pdf", png(), "an image sent as a document"),
        ("application/zip", archive(), "an archive, which is not a supported type"),
    ],
)
async def test_a_spoofed_upload_is_refused_and_nothing_is_sent(
    db_session, settings, meta, claimed, content, label
):
    """Refused before Meta is called, so no message row and no stored file."""
    tenant, conversation = await _conversation(db_session)
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    with pytest.raises(MediaTypeError):
        await service.send_media(
            conversation_id=conversation.id,
            content=content,
            mime_type=claimed,
            filename="photo.jpg",
        )

    assert meta.requests == [], f"{label}: a refused file still reached Meta"


async def test_the_filename_extension_decides_nothing(db_session, settings, meta, tmp_path):
    """`photo.jpg` is a string from a request body and carries no authority.

    These bytes are a PDF, so the file is perfectly acceptable - as a *document*.
    The property under test is that the `.jpg` never makes it an image: the kind
    on the message, the type Meta is told and the type recorded all come from
    the bytes, and the name is carried only for the recipient to read.
    """
    tenant, conversation = await _conversation(db_session)
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    message = await service.send_media(
        conversation_id=conversation.id,
        content=PDF,
        mime_type=None,
        filename="photo.jpg",
        storage=LocalMediaStorage(tmp_path),
    )

    assert message.kind is MessageKind.DOCUMENT
    assert b"application/pdf" in meta.upload.content
    assert b"image/jpeg" not in meta.upload.content


async def test_an_extension_cannot_rescue_a_contradicted_claim(db_session, settings, meta):
    """The same file, now announced as the thing its name suggests. Refused."""
    tenant, conversation = await _conversation(db_session)
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    with pytest.raises(MediaTypeError):
        await service.send_media(
            conversation_id=conversation.id,
            content=PDF,
            mime_type="image/jpeg",
            filename="photo.jpg",
        )

    assert meta.requests == []


async def test_a_genuine_image_is_still_sent(db_session, settings, meta, tmp_path):
    tenant, conversation = await _conversation(db_session)
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    message = await service.send_media(
        conversation_id=conversation.id,
        content=png(),
        mime_type="image/png",
        filename="sofa.png",
        storage=LocalMediaStorage(tmp_path),
    )

    assert message.kind is MessageKind.IMAGE
    assert message.status is MessageStatus.SENT


async def test_meta_is_told_the_canonical_type_not_the_callers(
    db_session, settings, meta, tmp_path
):
    """A caller writing an odd spelling must not have it forwarded verbatim.

    Meta renders an attachment by what it is told it is, so passing the claim
    through would let a mislabelled file be mislabelled to the customer too.
    """
    tenant, conversation = await _conversation(db_session)
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    await service.send_media(
        conversation_id=conversation.id,
        content=png(),
        mime_type="IMAGE/PNG; charset=binary",
        filename="sofa.png",
        storage=LocalMediaStorage(tmp_path),
    )

    body = meta.upload.content
    assert b"image/png" in body
    assert b"charset=binary" not in body


async def test_the_stored_row_carries_the_detected_type(db_session, settings, meta, tmp_path):
    """What is served back later comes from this column."""
    tenant, conversation = await _conversation(db_session)
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    message = await service.send_media(
        conversation_id=conversation.id,
        content=png(),
        # Absent, which is not a conflict - the bytes decide on their own.
        mime_type=None,
        filename="sofa.png",
        storage=LocalMediaStorage(tmp_path),
    )
    await db_session.flush()

    row = await db_session.get(MessageMedia, (await _media_id(db_session, message.id)))
    assert row is not None
    assert row.mime_type == "image/png"
    assert row.storage_key is not None


async def _media_id(session, message_id):
    from sqlalchemy import select

    return (
        await session.execute(select(MessageMedia.id).where(MessageMedia.message_id == message_id))
    ).scalar_one()


# ============================================ inbound: a file arrives from Meta


class StubWhatsApp:
    """Answers the two calls the download path makes, with whatever it is given."""

    def __init__(self, *, content: bytes, claimed: str) -> None:
        self._content = content
        self._claimed = claimed
        self.fetched = 0

    async def probe_media(self, media_id: str) -> MediaDescriptor:
        return MediaDescriptor(mime_type=self._claimed, byte_size=len(self._content))

    async def fetch_media(self, media_id: str, *, max_bytes: int) -> DownloadedMedia:
        self.fetched += 1
        return DownloadedMedia(
            content=self._content,
            mime_type=self._claimed,
            byte_size=len(self._content),
            declared_size=len(self._content),
            sha256=None,
        )


async def _attachment(session, tenant, conversation, *, mime_type):
    from app.db.models.conversation import Message, MessageDirection

    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.IMAGE,
        status=MessageStatus.DELIVERED,
    )
    session.add(message)
    await session.flush()

    media = MessageMedia(
        tenant_id=tenant.id,
        message_id=message.id,
        conversation_id=conversation.id,
        wa_media_id="meta-handle-1",
        mime_type=mime_type,
        status=MediaStatus.PENDING,
    )
    session.add(media)
    await session.flush()
    return media


@pytest.mark.parametrize(
    ("claimed", "content"),
    [
        ("image/jpeg", PDF),
        ("image/png", HTML),
        ("image/png", b"not an image at all, just words"),
        ("audio/ogg", png()),
        ("application/pdf", png()),
    ],
)
async def test_an_inbound_file_that_contradicts_metas_claim_is_skipped(
    db_session, settings, tmp_path, claimed, content
):
    """Meta's descriptor is a claim about a file, not the file.

    Skipped rather than failed: no retry turns these bytes into the announced
    type, and a retry loop against that is what the two states exist to avoid.
    """
    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant, conversation, mime_type=claimed)

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=StubWhatsApp(content=content, claimed=claimed),
    )
    outcome = await service.download(media)

    assert outcome.status is MediaStatus.SKIPPED
    assert media.storage_key is None
    assert list(tmp_path.rglob("*.*")) == [], "a refused file was written to the store anyway"


async def test_a_skipped_mismatch_never_reaches_the_reader(db_session, settings, tmp_path):
    """The reader is what sends an image to a vision model. It must not run."""
    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant, conversation, mime_type="image/jpeg")

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=StubWhatsApp(content=PDF, claimed="image/jpeg"),
    )
    await service.download(media)

    class ExplodingReader:
        async def read(self, *, content: bytes, mime_type: str | None):
            raise AssertionError("a mismatched file was handed to the reader")

    outcome = await service.understand(media, reader=ExplodingReader())  # type: ignore[arg-type]
    assert outcome.status is MediaStatus.SKIPPED


async def test_an_honest_inbound_file_is_stored_under_its_detected_type(
    db_session, settings, tmp_path
):
    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant, conversation, mime_type="audio/ogg")

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=StubWhatsApp(content=OGG, claimed="audio/ogg"),
    )
    outcome = await service.download(media)

    assert outcome.status is MediaStatus.STORED
    assert media.mime_type == "audio/ogg"
    assert media.storage_key is not None


async def test_the_refusal_message_does_not_repeat_the_bytes(db_session, settings, tmp_path):
    """A row's `last_error` is read by a colleague and stored in the database."""
    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant, conversation, mime_type="image/jpeg")

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=StubWhatsApp(content=PDF + b"secret-marker-9f2a", claimed="image/jpeg"),
    )
    await service.download(media)

    assert media.last_error is not None
    assert "secret-marker" not in media.last_error
    assert "%PDF" not in media.last_error


async def test_an_oversized_download_is_abandoned_rather_than_held(db_session, settings, tmp_path):
    """The cap is enforced while reading, and the outcome is a decision not a failure."""
    from app.integrations.whatsapp.client import MediaTooLargeError

    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant, conversation, mime_type="image/png")

    class Oversized(StubWhatsApp):
        async def probe_media(self, media_id: str) -> MediaDescriptor:
            # Under-declares, which is the case a post-hoc check cannot catch.
            return MediaDescriptor(mime_type="image/png", byte_size=8)

        async def fetch_media(self, media_id: str, *, max_bytes: int) -> DownloadedMedia:
            raise MediaTooLargeError()

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=Oversized(content=png(), claimed="image/png"),
    )
    outcome = await service.download(media)

    assert outcome.status is MediaStatus.SKIPPED
    assert media.storage_key is None
