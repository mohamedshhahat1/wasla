"""The media worker against a real database.

The point of this file is the gate. Downloading, reading and skipping are covered
against the service; what only PostgreSQL can prove is that two files arriving on
one conversation produce exactly one agent job, because that is a statement about
row locks rather than about Python.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.core.storage import LocalMediaStorage
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
from app.integrations.whatsapp.client import DownloadedMedia, MediaDescriptor
from app.services.media_reader import ReadResult
from app.workers.media_queue import MediaJob
from app.workers.media_worker import MediaWorker

pytestmark = pytest.mark.integration

PIXEL = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class SessionHandle:
    """Hands the worker the test's own session, so its writes roll back."""

    def __init__(self, session) -> None:
        self._session = session
        self.opened = 0

    @asynccontextmanager
    async def session(self):
        self.opened += 1
        yield self._session


class FakeRedis:
    """The worker only reaches for `.client`; the queues are replaced after."""

    @property
    def client(self):
        return object()


class RecordingQueue:
    """Stands in for a Redis queue and remembers what was put on it."""

    def __init__(self) -> None:
        self.jobs: list[object] = []

    async def enqueue(self, job) -> None:
        self.jobs.append(job)


class StubWhatsApp:
    """Answers the two calls the download path makes."""

    def __init__(self, *, content: bytes = PIXEL, mime_type: str = "image/png") -> None:
        self._content = content
        self._mime_type = mime_type
        self.fetched = 0

    async def probe_media(self, media_id: str) -> MediaDescriptor:
        return MediaDescriptor(mime_type=self._mime_type, byte_size=len(self._content))

    async def fetch_media(self, media_id: str, *, max_bytes: int) -> DownloadedMedia:
        self.fetched += 1
        return DownloadedMedia(
            content=self._content,
            mime_type=self._mime_type,
            byte_size=len(self._content),
            declared_size=len(self._content),
            sha256=None,
        )


class StubReader:
    """Returns a fixed transcript without touching a provider."""

    def __init__(self, transcript: str = "A blue sofa.") -> None:
        self._transcript = transcript
        self.reads = 0

    async def read(self, *, content: bytes, mime_type: str | None) -> ReadResult:
        self.reads += 1
        return ReadResult(transcript=self._transcript, method="vision")


async def _conversation(session, *, slug="acme"):
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id="201234567890")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
    )
    session.add(conversation)
    await session.flush()
    return tenant, conversation


async def _attachment(session, *, tenant, conversation, wa_media_id="media-1"):
    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        wa_message_id=f"wamid.{wa_media_id}",
        direction=MessageDirection.INBOUND,
        kind=MessageKind.IMAGE,
        status=MessageStatus.RECEIVED,
    )
    session.add(message)
    await session.flush()

    media = MessageMedia(
        tenant_id=tenant.id,
        message_id=message.id,
        conversation_id=conversation.id,
        wa_media_id=wa_media_id,
        status=MediaStatus.PENDING,
        mime_type="image/png",
        byte_size=0,
        is_voice=False,
        attempts=0,
    )
    session.add(media)
    await session.flush()
    return media


def _worker(db_session, tmp_path, settings, *, whatsapp=None, reader=None):
    worker = MediaWorker(
        database=SessionHandle(db_session),  # type: ignore[arg-type]
        redis=FakeRedis(),  # type: ignore[arg-type]
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp_factory=lambda http: whatsapp or StubWhatsApp(),
        reader_factory=lambda http: reader or StubReader(),
    )
    worker._agents = RecordingQueue()  # type: ignore[assignment]
    return worker


async def test_a_file_is_downloaded_read_and_released(db_session, tmp_path, settings):
    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant=tenant, conversation=conversation)

    whatsapp = StubWhatsApp()
    reader = StubReader("A blue sofa with a price tag reading 4,500 EGP.")
    worker = _worker(db_session, tmp_path, settings, whatsapp=whatsapp, reader=reader)

    await worker._handle(MediaJob(tenant_id=tenant.id, media_id=media.id))

    await db_session.refresh(media)
    assert media.status is MediaStatus.READY
    assert media.transcript == "A blue sofa with a price tag reading 4,500 EGP."
    assert media.storage_key is not None
    assert media.content_hash is not None
    assert whatsapp.fetched == 1
    assert reader.reads == 1

    # The conversation is now answerable, and only now.
    assert len(worker._agents.jobs) == 1
    assert worker._agents.jobs[0].conversation_id == conversation.id


async def test_two_files_on_one_conversation_produce_one_agent_job(db_session, tmp_path, settings):
    """The reason `ConversationMediaGate` exists.

    An agent turn is not idempotent, so two jobs mean the customer is answered
    twice for one question. Both files are read; exactly one release happens,
    and it is the one that finds nothing left unread.
    """
    tenant, conversation = await _conversation(db_session)
    first = await _attachment(db_session, tenant=tenant, conversation=conversation, wa_media_id="a")
    second = await _attachment(
        db_session, tenant=tenant, conversation=conversation, wa_media_id="b"
    )

    worker = _worker(db_session, tmp_path, settings)

    await worker._handle(MediaJob(tenant_id=tenant.id, media_id=first.id))
    # The first file is read, but its sibling is still pending, so nothing is
    # released yet.
    assert worker._agents.jobs == []

    await worker._handle(MediaJob(tenant_id=tenant.id, media_id=second.id))

    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.status is MediaStatus.READY
    assert second.status is MediaStatus.READY
    assert len(worker._agents.jobs) == 1


async def test_an_oversized_file_is_skipped_and_still_releases_the_reply(
    db_session, tmp_path, settings
):
    """A decision, not a failure - and the customer is still owed an answer."""
    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant=tenant, conversation=conversation)

    huge = StubWhatsApp(content=b"x" * 16)
    worker = _worker(db_session, tmp_path, settings, whatsapp=huge)
    worker._settings = settings.model_copy(update={"media_max_bytes": 8})

    await worker._handle(MediaJob(tenant_id=tenant.id, media_id=media.id))

    await db_session.refresh(media)
    assert media.status is MediaStatus.SKIPPED
    assert media.last_error is not None
    # Never fetched: the size was known before the bytes were moved.
    assert huge.fetched == 0
    assert len(worker._agents.jobs) == 1


async def test_a_job_for_a_row_that_is_gone_does_nothing(db_session, tmp_path, settings):
    """The message was deleted, or the job outlived its workspace."""
    tenant, _ = await _conversation(db_session)
    worker = _worker(db_session, tmp_path, settings)

    import uuid as _uuid

    await worker._handle(MediaJob(tenant_id=tenant.id, media_id=_uuid.uuid4()))

    assert worker._agents.jobs == []


async def test_a_job_from_another_workspace_finds_nothing(db_session, tmp_path, settings):
    """Tenant isolation on the worker path, where no request context exists.

    The job carries a tenant id, and the repository is scoped to it. A job
    naming another workspace's file must resolve to nothing rather than read it.
    """
    acme, acme_conversation = await _conversation(db_session, slug="acme")
    globex, _ = await _conversation(db_session, slug="globex")
    media = await _attachment(db_session, tenant=acme, conversation=acme_conversation)

    worker = _worker(db_session, tmp_path, settings)
    await worker._handle(MediaJob(tenant_id=globex.id, media_id=media.id))

    await db_session.refresh(media)
    assert media.status is MediaStatus.PENDING
    assert media.storage_key is None
    assert worker._agents.jobs == []


async def test_a_second_run_over_a_read_file_does_not_pay_again(db_session, tmp_path, settings):
    """Media jobs are idempotent, unlike agent turns.

    A retry must not re-download the bytes or re-run the provider over a file
    whose transcript is already on the row.
    """
    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant=tenant, conversation=conversation)

    whatsapp = StubWhatsApp()
    reader = StubReader()
    worker = _worker(db_session, tmp_path, settings, whatsapp=whatsapp, reader=reader)

    job = MediaJob(tenant_id=tenant.id, media_id=media.id)
    await worker._handle(job)
    await worker._handle(job)

    assert whatsapp.fetched == 1
    assert reader.reads == 1


class UploadingWhatsApp:
    """A client that records an upload and then acknowledges the send."""

    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.sends: list[dict] = []

    async def upload_media(self, **kwargs) -> str:
        self.uploads.append(kwargs)
        return "uploaded-1"

    async def send_media(self, **kwargs):
        self.sends.append(kwargs)
        from app.integrations.whatsapp.client import SentMessage

        return SentMessage(message_id="wamid.out", recipient="201234567890", raw={})


async def test_an_attachment_is_uploaded_then_sent_and_recorded(
    db_session, tmp_path, settings, monkeypatch
):
    """Uploaded rather than linked.

    A link needs a publicly reachable URL for as long as Meta might fetch it;
    an upload exposes the bytes to one recipient for one send.
    """
    from datetime import UTC, datetime

    from app.services import messaging_service as messaging_module
    from app.services.messaging_service import MessagingService

    tenant, conversation = await _conversation(db_session)
    # The service window is open only if the customer has spoken.
    conversation.last_inbound_at = datetime.now(UTC)
    await db_session.flush()

    whatsapp = UploadingWhatsApp()

    class _Http:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr(messaging_module, "build_http_client", lambda: _Http())
    monkeypatch.setattr(messaging_module, "WhatsAppClient", lambda **kwargs: whatsapp)

    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)
    message = await service.send_media(
        conversation_id=conversation.id,
        content=b"%PDF-1.4 quote",
        mime_type="application/pdf",
        filename="quote.pdf",
        caption="here is the quote",
        storage=LocalMediaStorage(tmp_path),
    )

    assert message.status is MessageStatus.SENT
    assert message.kind is MessageKind.DOCUMENT
    # The caption is the text of the message, as it is on an inbound one.
    assert message.body == "here is the quote"
    assert whatsapp.uploads[0]["filename"] == "quote.pdf"
    assert whatsapp.sends[0]["media_id"] == "uploaded-1"

    from app.repositories.media_repository import MediaRepository

    media = await MediaRepository(db_session, tenant_id=tenant.id).get_for_message(message.id)
    assert media is not None
    assert media.status is MediaStatus.READY
    assert media.storage_key is not None
    assert media.byte_size == len(b"%PDF-1.4 quote")


async def test_a_hostile_filename_is_replaced_before_it_reaches_meta(
    db_session, tmp_path, settings, monkeypatch
):
    """The name travels to a third party and is shown to the recipient.

    It is never used to build a path on this side, but anything that is not
    plainly a filename is replaced rather than cleaned up.
    """
    from datetime import UTC, datetime

    from app.services import messaging_service as messaging_module
    from app.services.messaging_service import MessagingService

    tenant, conversation = await _conversation(db_session)
    conversation.last_inbound_at = datetime.now(UTC)
    await db_session.flush()

    whatsapp = UploadingWhatsApp()

    class _Http:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr(messaging_module, "build_http_client", lambda: _Http())
    monkeypatch.setattr(messaging_module, "WhatsAppClient", lambda **kwargs: whatsapp)

    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)
    await service.send_media(
        conversation_id=conversation.id,
        content=b"%PDF-1.4",
        mime_type="application/pdf",
        filename="../../etc/passwd",
    )

    assert whatsapp.uploads[0]["filename"] == "attachment.pdf"


async def test_an_attachment_outside_the_service_window_is_refused(db_session, tmp_path, settings):
    """An attachment is a free-form message, so the same rule applies as to text."""
    from app.core.exceptions import ValidationError
    from app.services.messaging_service import MessagingService

    tenant, conversation = await _conversation(db_session)
    # `last_inbound_at` stays None: the customer has never written.

    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)
    with pytest.raises(ValidationError):
        await service.send_media(
            conversation_id=conversation.id,
            content=b"x",
            mime_type="image/png",
        )


async def test_a_type_meta_will_not_accept_is_refused(db_session, tmp_path, settings):
    from datetime import UTC, datetime

    from app.core.exceptions import ValidationError
    from app.services.messaging_service import MessagingService

    tenant, conversation = await _conversation(db_session)
    conversation.last_inbound_at = datetime.now(UTC)
    await db_session.flush()

    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)
    with pytest.raises(ValidationError):
        await service.send_media(
            conversation_id=conversation.id,
            content=b"PK",
            mime_type="application/zip",
        )


async def test_a_stored_file_is_read_without_a_whatsapp_token(db_session, tmp_path, settings):
    """Found by running the container, not by the suite.

    The worker used to build a `WhatsAppClient` before it knew whether the job
    needed one, and that constructor raises when no access token is configured.
    A deployment without a token therefore failed every media job - including
    the ones whose bytes were already in the store and needed nothing from
    Meta at all.
    """
    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant=tenant, conversation=conversation)

    storage = LocalMediaStorage(tmp_path)
    media.storage_key = await storage.put(
        tenant_id=tenant.id,
        data=b"the warranty lasts two years",
        mime_type="text/plain",
    )
    media.mime_type = "text/plain"
    media.status = MediaStatus.STORED
    await db_session.flush()

    worker = MediaWorker(
        database=SessionHandle(db_session),  # type: ignore[arg-type]
        redis=FakeRedis(),  # type: ignore[arg-type]
        # No token, as an unconfigured deployment has none.
        settings=settings.model_copy(update={"meta_access_token": None}),
        storage=storage,
    )
    worker._agents = RecordingQueue()  # type: ignore[assignment]

    await worker._handle(MediaJob(tenant_id=tenant.id, media_id=media.id))

    await db_session.refresh(media)
    assert media.status is MediaStatus.READY
    assert media.transcript == "the warranty lasts two years"
    assert len(worker._agents.jobs) == 1


async def test_a_document_is_read_with_no_provider_configured(db_session, tmp_path, settings):
    """Extraction needs no OpenAI key, and must not pretend otherwise."""
    tenant, conversation = await _conversation(db_session)
    media = await _attachment(db_session, tenant=tenant, conversation=conversation)

    storage = LocalMediaStorage(tmp_path)
    media.storage_key = await storage.put(
        tenant_id=tenant.id,
        data=b"delivery takes three working days",
        mime_type="text/plain",
    )
    media.mime_type = "text/plain"
    media.status = MediaStatus.STORED
    await db_session.flush()

    worker = MediaWorker(
        database=SessionHandle(db_session),  # type: ignore[arg-type]
        redis=FakeRedis(),  # type: ignore[arg-type]
        settings=settings.model_copy(update={"meta_access_token": None, "openai_api_key": None}),
        storage=storage,
    )
    worker._agents = RecordingQueue()  # type: ignore[assignment]

    await worker._handle(MediaJob(tenant_id=tenant.id, media_id=media.id))

    await db_session.refresh(media)
    assert media.status is MediaStatus.READY
    assert media.transcript == "delivery takes three working days"
