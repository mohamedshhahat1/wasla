"""Scaffolding for driving the real `MediaWorker` against a real database.

Shared by the media remediation suites so each test reads as the scenario it is
about - a poison file, a failing descriptor, a suspended workspace - rather than
as forty lines of rows. The worker, the service, the reader and the release
gate are always the production ones; only the two network edges (Meta, and the
queue the agent job lands on) are stood in for.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.storage import LocalMediaStorage, MediaStorage
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp.client import DownloadedMedia, MediaDescriptor
from app.workers.media_queue import MediaJob
from app.workers.media_worker import MediaWorker
from app.workers.queue import AgentJob
from tests.fake_queue_redis import FakeQueueRedis
from tests.fakes import as_media_reader, as_whatsapp

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
TEXT = b"the warranty lasts two years"


class SessionHandle:
    """Hands the worker the test's own session, so its writes roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.opened = 0

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        self.opened += 1
        yield self._session


class FakeRedis:
    """The worker only reaches for `.client`; the queues are replaced after."""

    @property
    def client(self) -> FakeQueueRedis:
        return FakeQueueRedis()


class RecordingQueue:
    """Stands in for the agent queue and remembers what was put on it."""

    def __init__(self) -> None:
        self.jobs: list[AgentJob] = []

    async def enqueue(self, job: AgentJob) -> None:
        self.jobs.append(job)


@dataclass
class StubWhatsApp:
    """The two calls a download makes, scriptable per test.

    `probe` and `fetch` may be replaced with coroutines that raise, to stand in
    for whatever Meta answered. Every call is counted, so a test can say how
    many times Meta was asked.
    """

    content: bytes = PNG
    mime_type: str = "image/png"
    probes: int = 0
    fetches: int = 0
    probe_error: BaseException | None = None
    fetch_error: BaseException | None = None
    before_fetch: Callable[[], Awaitable[None]] | None = None

    async def probe_media(self, media_id: str) -> MediaDescriptor:
        self.probes += 1
        if self.probe_error is not None:
            raise self.probe_error
        return MediaDescriptor(mime_type=self.mime_type, byte_size=len(self.content))

    async def fetch_media(self, media_id: str, *, max_bytes: int) -> DownloadedMedia:
        self.fetches += 1
        if self.before_fetch is not None:
            await self.before_fetch()
        if self.fetch_error is not None:
            raise self.fetch_error
        return DownloadedMedia(
            content=self.content,
            mime_type=self.mime_type,
            byte_size=len(self.content),
            declared_size=len(self.content),
            sha256=None,
        )


@dataclass
class Scene:
    """One workspace, one number, one customer, one conversation."""

    tenant: Tenant
    account: WhatsAppAccount
    contact: Contact
    conversation: Conversation
    counter: list[int] = field(default_factory=lambda: [0])


async def scene(session: AsyncSession, *, slug: str = "acme") -> Scene:
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
    return Scene(tenant=tenant, account=account, contact=contact, conversation=conversation)


async def attachment(
    session: AsyncSession,
    where: Scene,
    *,
    mime_type: str | None = "image/png",
    kind: MessageKind = MessageKind.IMAGE,
    filename: str | None = None,
) -> MessageMedia:
    where.counter[0] += 1
    number = where.counter[0]
    message = Message(
        tenant_id=where.tenant.id,
        conversation_id=where.conversation.id,
        wa_message_id=f"wamid.{where.tenant.slug}.{number}",
        direction=MessageDirection.INBOUND,
        kind=kind,
        status=MessageStatus.RECEIVED,
        origin=MessageOrigin.CUSTOMER,
    )
    session.add(message)
    await session.flush()

    media = MessageMedia(
        tenant_id=where.tenant.id,
        message_id=message.id,
        conversation_id=where.conversation.id,
        wa_media_id=f"media-{where.tenant.slug}-{number}",
        status=MediaStatus.PENDING,
        mime_type=mime_type,
        filename=filename,
        byte_size=0,
        is_voice=False,
        attempts=0,
    )
    session.add(media)
    await session.flush()
    return media


def worker(
    session: AsyncSession,
    tmp_path: Path,
    settings: Settings,
    *,
    whatsapp: Any = None,
    reader: Any = None,
    storage: MediaStorage | None = None,
    database: Any = None,
) -> MediaWorker:
    """A production `MediaWorker` with the network edges replaced.

    `reader=None` keeps the worker's own reader - the real `MediaReader`, with
    its bounded PDF child - which is what the parser tests need. Pass a stub to
    stand in for a provider.
    """
    built = MediaWorker(
        database=database or SessionHandle(session),  # type: ignore[arg-type]
        redis=FakeRedis(),  # type: ignore[arg-type]
        settings=settings,
        storage=storage or LocalMediaStorage(tmp_path),
        whatsapp_factory=lambda http: as_whatsapp(whatsapp or StubWhatsApp()),
        reader_factory=(None if reader is None else (lambda http: as_media_reader(reader))),
    )
    built._agents = RecordingQueue()  # type: ignore[assignment]
    return built


async def run(built: MediaWorker, media: MessageMedia) -> AgentJob | None:
    """Handle one job the way `_attempt` does, and queue what it owes."""
    follow_up = await built._handle(MediaJob(tenant_id=media.tenant_id, media_id=media.id))
    if follow_up is not None:
        await built._agents.enqueue(follow_up)
    return follow_up


def released(built: MediaWorker) -> list[AgentJob]:
    queue = built._agents
    assert isinstance(queue, RecordingQueue)
    return queue.jobs
