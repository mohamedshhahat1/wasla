"""The neutral core, driven through a channel that is not WhatsApp (OMNI-R10).

The synthetic adapter in `tests/channel_fakes.py` stands in for a second
provider: connection-scoped sender ids, a 1,000-byte limit, a seven-day window
and no templates, several attachments per message, echoes of the business's own
sends, and read receipts as a watermark. It is registered only here, in a
registry of the test's own.

What this proves is the foundation's claim: that the shared ingestion,
identity, projection, media, status and sending code works for a connection on
another channel **without** any of it reaching for WhatsApp - and that where the
deployment has no adapter, nothing falls back to WhatsApp's.

Mutants this suite kills: M5 (WhatsApp's 24-hour window applied to another
channel), M6 (another channel's send handed to the WhatsApp sender), M7 (the
inbox ignoring the connection filter), M11 (an echo projected as a customer
message) and M12 at the send boundary (a byte limit counted in characters).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry, ChannelUnavailableError
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
)
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.media import MediaLocatorKind, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEvent, UsageEventType
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.repositories.conversation_repository import ConversationRepository
from app.services import messaging_service as messaging_module
from app.services.channel_ingestion_service import ChannelIngestionService, IngestionOutcome
from app.services.messaging_service import MessagingService
from app.workers.media_queue import MediaJob, MediaQueue
from app.workers.queue import AgentJob, AgentQueue
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

pytestmark = pytest.mark.integration

SENDER = "igsid-0synthetic0customer"
ARABIC = "مرحبا بك في متجرنا، كيف يمكنني مساعدتك اليوم؟ "


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[Any] = []

    async def enqueue(self, job: AgentJob | MediaJob) -> None:
        self.jobs.append(job)


class NoWhatsApp:
    """The WhatsApp HTTP pool: any request reaching it is the defect."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(500)

        return httpx.MockTransport(handle)


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


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> Iterator[NoWhatsApp]:
    fake = NoWhatsApp()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=fake.transport()),
    )
    yield fake


@pytest.fixture
def adapter() -> SyntheticAdapter:
    return SyntheticAdapter(Channel.INSTAGRAM, file=b"\x89PNG\r\n\x1a\n" + b"0" * 64)


@pytest.fixture
def registry(adapter: SyntheticAdapter) -> ChannelRegistry:
    return ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.INSTAGRAM: cast(ChannelAdapter, adapter),
        },
        unmetered=True,
    )


async def _workspace(session: AsyncSession) -> Tenant:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Second {tag}", slug=f"second-{tag}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _connection(session: AsyncSession, tenant: Tenant) -> ChannelConnection:
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"syn-{uuid.uuid4().hex[:12]}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(connection)
    await session.flush()
    return connection


async def _ingest(
    session: AsyncSession,
    adapter: SyntheticAdapter,
    payload: dict[str, Any],
    *,
    queue: RecordingQueue | None = None,
    media: RecordingQueue | None = None,
) -> IngestionOutcome:
    return await ChannelIngestionService(
        session=session,
        adapter=cast(ChannelAdapter, adapter),
        queue=cast("AgentQueue", queue or RecordingQueue()),
        media_queue=cast("MediaQueue", media or RecordingQueue()),
    ).ingest(adapter.parse(payload))


def _message(event_id: str, *, text: str = "hello", **extra: Any) -> dict[str, Any]:
    return {
        "type": "message",
        "id": event_id,
        "from": SENDER,
        "at": int(datetime.now(UTC).timestamp()),
        "text": text,
        **extra,
    }


async def _the_conversation(session: AsyncSession, connection: ChannelConnection) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(Conversation.account_id == connection.id)
    )
    assert conversation is not None
    return conversation


# ------------------------------------------------------------- inbound


async def test_a_message_on_another_channel_lands_on_its_own_connection(
    db_session: AsyncSession, adapter: SyntheticAdapter
) -> None:
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)
    queue = RecordingQueue()

    outcome = await _ingest(
        db_session,
        adapter,
        synthetic_payload(connection.external_account_id, _message("syn.1")),
        queue=queue,
    )

    assert (outcome.stored, outcome.ignored) == (1, 0)
    conversation = await _the_conversation(db_session, connection)
    assert conversation.channel is Channel.INSTAGRAM
    contact = await db_session.get(Contact, conversation.contact_id)
    assert contact is not None and contact.wa_id is None
    identity = await db_session.get(ContactIdentity, conversation.participant_identity_id)
    assert identity is not None
    assert (identity.channel, identity.kind, identity.value) == (
        Channel.INSTAGRAM,
        IdentityKind.IGSID,
        SENDER,
    )
    # Unique per connection, the scope the adapter declared.
    assert (identity.scope, identity.scope_ref, identity.connection_id) == (
        IdentityScope.CONNECTION,
        str(connection.id),
        connection.id,
    )
    message = await db_session.scalar(
        select(Message).where(Message.conversation_id == conversation.id)
    )
    assert message is not None and message.connection_id == connection.id
    assert [job.conversation_id for job in queue.jobs] == [conversation.id]


async def test_a_channel_without_a_decided_meter_is_not_metered_as_whatsapp(
    db_session: AsyncSession, adapter: SyntheticAdapter
) -> None:
    """ADR-122: what another channel's message costs is undecided, so nothing
    is written under a WhatsApp meter for it."""
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)

    await _ingest(
        db_session, adapter, synthetic_payload(connection.external_account_id, _message("syn.2"))
    )

    whatsapp_meters = await db_session.scalar(
        select(func.count())
        .select_from(UsageEvent)
        .where(
            UsageEvent.tenant_id == tenant.id,
            UsageEvent.event_type.in_(
                [UsageEventType.WHATSAPP_MESSAGE_RECEIVED, UsageEventType.WHATSAPP_MESSAGE_SENT]
            ),
        )
    )
    assert whatsapp_meters == 0


async def test_an_echo_of_our_own_send_is_evidence_not_a_turn(
    db_session: AsyncSession,
    adapter: SyntheticAdapter,
    registry: ChannelRegistry,
    settings: Settings,
    graph: NoWhatsApp,
) -> None:
    """M11: the business's own message reported back is never a customer's
    message - it stores no message, opens no window and queues no turn.
    (An echo naming no Wasla send is a reply typed outside Wasla: see
    test_external_echoes.py, OMNI-037.)"""
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)
    await _ingest(
        db_session, adapter, synthetic_payload(connection.external_account_id, _message("syn.3"))
    )
    conversation = await _the_conversation(db_session, connection)
    window = conversation.last_inbound_at
    sent = await MessagingService(
        session=db_session, settings=settings, tenant_id=tenant.id, channels=registry
    ).send_text(conversation_id=conversation.id, body="We replied", origin=MessageOrigin.AGENT)
    messages_before = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )
    queue = RecordingQueue()

    outcome = await _ingest(
        db_session,
        adapter,
        synthetic_payload(
            connection.external_account_id,
            {
                "type": "echo",
                "id": sent.wa_message_id,
                "to": SENDER,
                "at": int(datetime.now(UTC).timestamp()),
                "text": "We replied",
            },
        ),
        queue=queue,
    )

    assert (outcome.echoes, outcome.stored, outcome.external_echoes) == (1, 1, 0)
    assert queue.jobs == []
    messages_after = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )
    assert messages_after == messages_before
    await db_session.refresh(conversation)
    assert conversation.last_inbound_at == window


async def test_every_attachment_of_a_message_is_recorded_and_queued(
    db_session: AsyncSession, adapter: SyntheticAdapter
) -> None:
    """OMNI-009: one message, three files - three rows in the provider's
    order, each with its own locator, each handed to the media worker."""
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)
    media = RecordingQueue()
    urls = [f"https://cdn.synthetic.test/{index}.png" for index in range(3)]

    outcome = await _ingest(
        db_session,
        adapter,
        synthetic_payload(
            connection.external_account_id,
            _message(
                "syn.4",
                text="which one?",
                attachments=[{"url": url, "kind": "image", "mime": "image/png"} for url in urls],
            ),
        ),
        media=media,
    )

    assert outcome.media_queued == 1
    rows = (
        (
            await db_session.execute(
                select(MessageMedia)
                .where(MessageMedia.tenant_id == tenant.id)
                .order_by(MessageMedia.position)
            )
        )
        .scalars()
        .all()
    )
    assert [(row.position, row.locator_kind, row.locator) for row in rows] == [
        (index, MediaLocatorKind.URL, url) for index, url in enumerate(urls)
    ]
    assert all(row.wa_media_id is None for row in rows)
    assert sorted(job.media_id for job in media.jobs) == sorted(row.id for row in rows)


# ------------------------------------------------------------- outbound


async def test_a_reply_goes_through_the_conversations_own_adapter(
    db_session: AsyncSession,
    settings: Settings,
    adapter: SyntheticAdapter,
    registry: ChannelRegistry,
    graph: NoWhatsApp,
) -> None:
    """M6: the synthetic sender, to the synthetic identity - and not one
    request to WhatsApp's API."""
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)
    await _ingest(
        db_session, adapter, synthetic_payload(connection.external_account_id, _message("syn.5"))
    )
    conversation = await _the_conversation(db_session, connection)

    message = await MessagingService(
        session=db_session, settings=settings, tenant_id=tenant.id, channels=registry
    ).send_text(conversation_id=conversation.id, body="Here you go.", origin=MessageOrigin.HUMAN)

    assert message.status is MessageStatus.SENT
    assert message.connection_id == connection.id
    ((recipient, _),) = adapter.log.sent
    assert (recipient.kind, recipient.value) == (IdentityKind.IGSID.value, SENDER)
    assert graph.requests == []


async def test_another_channels_window_is_its_own_not_whatsapps(
    db_session: AsyncSession,
    settings: Settings,
    adapter: SyntheticAdapter,
    registry: ChannelRegistry,
    graph: NoWhatsApp,
) -> None:
    """M5: three days after the customer wrote, WhatsApp's 24-hour window
    would refuse free text. This channel's window is seven days, and it is the
    one that applies."""
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)
    await _ingest(
        db_session, adapter, synthetic_payload(connection.external_account_id, _message("syn.6"))
    )
    conversation = await _the_conversation(db_session, connection)
    conversation.last_inbound_at = datetime.now(UTC) - timedelta(days=3)
    await db_session.flush()
    service = MessagingService(
        session=db_session, settings=settings, tenant_id=tenant.id, channels=registry
    )

    assert service.window_open(conversation) is True
    message = await service.send_text(
        conversation_id=conversation.id, body="Still here.", origin=MessageOrigin.HUMAN
    )
    assert message.status is MessageStatus.SENT

    conversation.last_inbound_at = datetime.now(UTC) - timedelta(days=8)
    await db_session.flush()
    with pytest.raises(ValidationError, match="synthetic reply window"):
        await service.send_text(
            conversation_id=conversation.id, body="Late.", origin=MessageOrigin.HUMAN
        )


async def test_a_byte_bounded_channel_refuses_text_that_fits_whatsapp(
    db_session: AsyncSession,
    settings: Settings,
    adapter: SyntheticAdapter,
    registry: ChannelRegistry,
    graph: NoWhatsApp,
) -> None:
    """M12 at the send boundary: 700 Arabic characters are ~1,400 bytes -
    refused before anything is staged, not after the provider says no."""
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)
    await _ingest(
        db_session, adapter, synthetic_payload(connection.external_account_id, _message("syn.7"))
    )
    conversation = await _the_conversation(db_session, connection)
    body = (ARABIC * 20)[:700]

    with pytest.raises(ValidationError):
        await MessagingService(
            session=db_session, settings=settings, tenant_id=tenant.id, channels=registry
        ).send_text(conversation_id=conversation.id, body=body, origin=MessageOrigin.HUMAN)

    staged = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.direction == MessageDirection.OUTBOUND,
        )
    )
    assert staged == 0
    assert adapter.log.sent == []


async def test_without_an_adapter_the_channel_is_refused_not_sent_as_whatsapp(
    db_session: AsyncSession,
    settings: Settings,
    adapter: SyntheticAdapter,
    graph: NoWhatsApp,
) -> None:
    """The deployment's own registry operates WhatsApp only. A conversation on
    any other channel is refused before anything is staged."""
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)
    await _ingest(
        db_session, adapter, synthetic_payload(connection.external_account_id, _message("syn.8"))
    )
    conversation = await _the_conversation(db_session, connection)

    with pytest.raises(ChannelUnavailableError):
        await MessagingService(
            session=db_session, settings=settings, tenant_id=tenant.id
        ).send_text(conversation_id=conversation.id, body="Hello", origin=MessageOrigin.HUMAN)

    outbound = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.direction == MessageDirection.OUTBOUND,
        )
    )
    assert outbound == 0
    assert graph.requests == []


# ---------------------------------------------------- receipts (OMNI-011)


async def test_a_read_watermark_advances_only_what_was_sent_before_it(
    db_session: AsyncSession,
    settings: Settings,
    adapter: SyntheticAdapter,
    registry: ChannelRegistry,
    graph: NoWhatsApp,
) -> None:
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)
    await _ingest(
        db_session, adapter, synthetic_payload(connection.external_account_id, _message("syn.9"))
    )
    conversation = await _the_conversation(db_session, connection)
    service = MessagingService(
        session=db_session, settings=settings, tenant_id=tenant.id, channels=registry
    )
    earlier = await service.send_text(
        conversation_id=conversation.id, body="One.", origin=MessageOrigin.HUMAN
    )
    later = await service.send_text(
        conversation_id=conversation.id, body="Two.", origin=MessageOrigin.HUMAN
    )
    now = datetime.now(UTC)
    earlier.sent_at = now - timedelta(minutes=10)
    later.sent_at = now - timedelta(seconds=10)
    await db_session.flush()
    watermark = now - timedelta(minutes=5)

    outcome = await _ingest(
        db_session,
        adapter,
        synthetic_payload(
            connection.external_account_id,
            {
                "type": "read",
                "from": SENDER,
                "watermark": int(watermark.timestamp()),
                "at": int(now.timestamp()),
            },
        ),
    )

    assert outcome.stored == 1
    await db_session.refresh(earlier)
    await db_session.refresh(later)
    assert earlier.status is MessageStatus.READ
    assert later.status is MessageStatus.SENT


# --------------------------------------------------------- inbox (M7)


async def test_the_inbox_narrows_to_one_channel_and_one_connection(
    db_session: AsyncSession, adapter: SyntheticAdapter
) -> None:
    tenant = await _workspace(db_session)
    connection = await _connection(db_session, tenant)
    await _ingest(
        db_session, adapter, synthetic_payload(connection.external_account_id, _message("syn.10"))
    )
    number = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-{uuid.uuid4().hex[:10]}",
        waba_id="waba-inbox",
        display_phone_number="+20 100 000 0009",
    )
    db_session.add(number)
    await db_session.flush()
    contact = Contact(tenant_id=tenant.id, wa_id="201000000909")
    db_session.add(contact)
    await db_session.flush()
    db_session.add(
        Conversation(
            tenant_id=tenant.id,
            contact_id=contact.id,
            account_id=number.id,
            last_message_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    rival = await _workspace(db_session)
    rivals = await _connection(db_session, rival)
    inbox = ConversationRepository(db_session, tenant_id=tenant.id)

    everything = await inbox.list_open(limit=10)
    instagram = await inbox.list_open(limit=10, channel=Channel.INSTAGRAM)
    whatsapp = await inbox.list_open(limit=10, connection_id=number.id)
    foreign = await inbox.list_open(limit=10, connection_id=rivals.id)

    assert len(everything) == 2
    assert [row.account_id for row in instagram] == [connection.id]
    assert [row.channel for row in whatsapp] == [Channel.WHATSAPP]
    assert foreign == []
