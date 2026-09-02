"""Every meter, on the path that actually fires it.

`test_usage_metering.py` proves the aggregates. This proves the wiring, which is
where metering goes wrong in practice: a meter is easy to add and easy to forget,
and a figure that is quietly missing an event type looks exactly like a quiet
month.

Each test drives the real service - the ingestion path, the messaging service,
the campaign dispatcher - and then reads the totals back. The two failures worth
naming are the ones asserted repeatedly here: counting something twice, and
counting something that did not happen.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.core.storage import LocalMediaStorage
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.lead import LeadSource
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)
from app.integrations.openai.types import TokenUsage
from app.integrations.whatsapp.client import DownloadedMedia, MediaDescriptor
from app.repositories.usage_repository import UsageEventRepository
from app.services import messaging_service as messaging_module
from app.services.campaign_service import CampaignService
from app.services.lead_service import ExtractedLead, LeadService
from app.services.media_reader import TRANSCRIPTION_METHOD, VISION_METHOD, ReadResult
from app.services.media_service import MediaService
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.ai_worker import _TurnProgress
from app.workers.queue import AgentJob

pytestmark = pytest.mark.integration

PHONE_NUMBER_ID = "109876543210"
WABA_ID = "555000111"
DISPLAY_NUMBER = "+201000000000"
CUSTOMER = "201234567890"
WAMID = "wamid.sent"
TEMPLATE_NAME = "appointment_reminder"
TEMPLATE_LANGUAGE = "ar_EG"

# Stand-ins for a real file. Nothing on this path inspects the bytes - the
# mime type decides how a file is read - so a plausible-looking header would
# only suggest a check that does not happen.
PNG_BYTES = b"png-bytes" + b"0" * 63
OGG_BYTES = b"ogg-bytes" + b"0" * 63


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        meta_access_token="test-access-token",
        # The agent worker builds a provider client before it runs a turn,
        # and that client refuses to exist without a key. Nothing here
        # reaches the network: the transport is a mock.
        openai_api_key="test-openai-key",
    )


@pytest.fixture
def meta(monkeypatch):
    """Answers as Meta would, so a send reaches the code that meters it."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messaging_product": "whatsapp",
                "contacts": [{"wa_id": CUSTOMER}],
                "messages": [{"id": WAMID}],
            },
        )

    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


@pytest.fixture
def refusing_meta(monkeypatch):
    """Rejects every send, so the failure path can be checked for silence."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Recipient not opted in."}})

    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


async def _tenant(session, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _account(session, tenant: Tenant) -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=PHONE_NUMBER_ID,
        waba_id=WABA_ID,
        display_phone_number=DISPLAY_NUMBER,
    )
    session.add(account)
    await session.flush()
    return account


async def _conversation(session, tenant: Tenant, account: WhatsAppAccount) -> Conversation:
    contact = Contact(tenant_id=tenant.id, wa_id=CUSTOMER)
    session.add(contact)
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        last_inbound_at=datetime.now(UTC),
    )
    session.add(conversation)
    await session.flush()
    return conversation


def _inbound(*, message_id: str = "wamid.in", text: str = "Hello") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Nour"}}],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": CUSTOMER,
                                    "type": "text",
                                    "timestamp": "1786000000",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


async def _totals(session, tenant: Tenant) -> dict[UsageEventType, int]:
    rows = await UsageEventRepository(session, tenant_id=tenant.id).totals()
    return {row.event_type: row.quantity for row in rows}


# --------------------------------------------------------------------- inbound


async def test_an_inbound_message_and_its_first_conversation_are_metered(db_session):
    tenant = await _tenant(db_session)
    await _account(db_session, tenant)

    await WhatsAppIngestionService(session=db_session).ingest(_inbound())
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.WHATSAPP_MESSAGE_RECEIVED] == 1
    assert totals[UsageEventType.CONVERSATION_CREATED] == 1


async def test_a_replayed_delivery_is_not_metered_twice(db_session):
    """Meta retries; a bill must not.

    The event id already stops the message being stored twice, and the meter
    rides on that rather than repeating the check.
    """
    tenant = await _tenant(db_session)
    await _account(db_session, tenant)
    service = WhatsAppIngestionService(session=db_session)

    await service.ingest(_inbound())
    await service.ingest(_inbound())
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.WHATSAPP_MESSAGE_RECEIVED] == 1
    assert totals[UsageEventType.CONVERSATION_CREATED] == 1


async def test_a_second_message_opens_no_second_conversation(db_session):
    tenant = await _tenant(db_session)
    await _account(db_session, tenant)
    service = WhatsAppIngestionService(session=db_session)

    await service.ingest(_inbound(message_id="wamid.one"))
    await service.ingest(_inbound(message_id="wamid.two", text="Still there?"))
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.WHATSAPP_MESSAGE_RECEIVED] == 2
    assert totals[UsageEventType.CONVERSATION_CREATED] == 1


# -------------------------------------------------------------------- outbound


async def test_a_sent_message_is_metered(db_session, settings, meta):
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    conversation = await _conversation(db_session, tenant, account)

    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)
    await service.send_text(conversation_id=conversation.id, body="On its way.")
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.WHATSAPP_MESSAGE_SENT] == 1


async def test_a_refused_send_is_not_metered(db_session, settings, refusing_meta):
    """Nothing was delivered, so nothing was consumed.

    The message row still exists in `failed` state - the attempt is recorded -
    but a workspace is not charged for a message Meta refused to carry.
    """
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    conversation = await _conversation(db_session, tenant, account)

    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)
    message = await service.send_text(conversation_id=conversation.id, body="On its way.")
    await db_session.flush()

    assert message.status is MessageStatus.FAILED
    assert await _totals(db_session, tenant) == {}


# ------------------------------------------------------------------------ CRM


async def test_a_lead_captured_from_a_conversation_is_metered_once(db_session):
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    conversation = await _conversation(db_session, tenant, account)
    service = LeadService(session=db_session, tenant_id=tenant.id)

    await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Nour", interest="finishing"),
    )
    # A second extraction on the same customer updates the lead it already has.
    # Counting that would make "leads created" grow every time somebody speaks.
    await service.capture_from_conversation(
        conversation_id=conversation.id,
        extracted=ExtractedLead(name="Nour", interest="finishing a flat"),
    )
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.LEAD_CREATED] == 1


async def test_a_lead_entered_by_a_person_is_metered(db_session):
    tenant = await _tenant(db_session)
    service = LeadService(session=db_session, tenant_id=tenant.id)

    await service.create_lead(actor_id=None, source=LeadSource.MANUAL, name="Walk-in")
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.LEAD_CREATED] == 1


# ------------------------------------------------------------------ campaigns


async def _campaign(session, tenant: Tenant, account: WhatsAppAccount) -> Campaign:
    template = WhatsAppTemplate(
        tenant_id=tenant.id,
        account_id=account.id,
        name=TEMPLATE_NAME,
        language=TEMPLATE_LANGUAGE,
        category=TemplateCategory.MARKETING,
        status=TemplateStatus.APPROVED,
    )
    session.add(template)
    await session.flush()

    campaign = Campaign(
        tenant_id=tenant.id,
        account_id=account.id,
        template_id=template.id,
        name="August offer",
        status=CampaignStatus.RUNNING,
        audience_size=1,
        messages_per_minute=60,
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def test_a_campaign_message_is_metered_as_both_a_message_and_campaign_traffic(
    db_session,
    settings,
    meta,
):
    """Two meters, two questions.

    A messaging allowance is spent by every message; a workspace looking at why
    a broadcast cost what it did wants the campaign figure on its own. The
    recipient row is what keeps the pair from double counting: it moves to
    `sent` in this same transaction and is never claimed again.
    """
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    conversation = await _conversation(db_session, tenant, account)
    campaign = await _campaign(db_session, tenant, account)

    db_session.add(
        CampaignRecipient(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            contact_id=conversation.contact_id,
        )
    )
    await db_session.flush()

    messaging = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)
    service = CampaignService(session=db_session, tenant_id=tenant.id, messaging=messaging)
    await service.dispatch_batch(campaign, now=datetime.now(UTC))
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.CAMPAIGN_MESSAGE] == 1
    assert totals[UsageEventType.WHATSAPP_MESSAGE_SENT] == 1


# ---------------------------------------------------------------------- media


class StubWhatsApp:
    """Answers the two calls the download path makes, without a network."""

    def __init__(self, content: bytes, mime_type: str) -> None:
        self._content = content
        self._mime_type = mime_type

    async def probe_media(self, media_id: str) -> MediaDescriptor:
        return MediaDescriptor(mime_type=self._mime_type, byte_size=len(self._content))

    async def fetch_media(self, media_id: str) -> DownloadedMedia:
        return DownloadedMedia(
            content=self._content,
            mime_type=self._mime_type,
            byte_size=len(self._content),
            declared_size=len(self._content),
            sha256=None,
        )


class StubReader:
    """Returns a fixed reading, by a named method."""

    def __init__(self, method: str) -> None:
        self._method = method

    async def read(self, *, content: bytes, mime_type: str | None) -> ReadResult:
        return ReadResult(transcript="what it said", method=self._method)


async def _attachment(session, tenant, account, *, is_voice: bool, mime_type: str):
    conversation = await _conversation(session, tenant, account)
    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.AUDIO if is_voice else MessageKind.IMAGE,
        status=MessageStatus.RECEIVED,
    )
    session.add(message)
    await session.flush()

    media = MessageMedia(
        tenant_id=tenant.id,
        message_id=message.id,
        conversation_id=conversation.id,
        wa_media_id="media-1",
        mime_type=mime_type,
        is_voice=is_voice,
        status=MediaStatus.PENDING,
    )
    session.add(media)
    await session.flush()
    return media


async def test_a_stored_file_meters_the_bytes_it_took(db_session, settings, tmp_path):
    """Metered when bytes are written, not by sweeping the store: a sweep
    reports a level, and a level cannot be billed for a period already closed."""
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    media = await _attachment(db_session, tenant, account, is_voice=False, mime_type="image/png")

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=StubWhatsApp(PNG_BYTES, "image/png"),
    )
    await service.download(media)
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.STORAGE_USED] == len(PNG_BYTES)


async def test_reading_a_voice_note_meters_the_read_and_the_transcription(
    db_session,
    settings,
    tmp_path,
):
    """Two providers, two meters. The transcription is a count of recordings
    rather than of seconds - the configured models report no duration, and a
    number inferred from a compressed byte count does not belong in a bill."""
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    media = await _attachment(db_session, tenant, account, is_voice=True, mime_type="audio/ogg")

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=StubWhatsApp(OGG_BYTES, "audio/ogg"),
    )
    await service.download(media)
    await service.understand(media, reader=StubReader(TRANSCRIPTION_METHOD))
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.MEDIA_PROCESSING] == 1
    assert totals[UsageEventType.VOICE_TRANSCRIPTION] == 1


async def test_reading_a_photograph_meters_no_transcription(db_session, settings, tmp_path):
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    media = await _attachment(db_session, tenant, account, is_voice=False, mime_type="image/png")

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=StubWhatsApp(PNG_BYTES, "image/png"),
    )
    await service.download(media)
    await service.understand(media, reader=StubReader(VISION_METHOD))
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.MEDIA_PROCESSING] == 1
    assert UsageEventType.VOICE_TRANSCRIPTION not in totals


async def test_a_file_already_read_is_not_metered_again(db_session, settings, tmp_path):
    """The media job can be retried, and a second read would be paid for twice."""
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    media = await _attachment(db_session, tenant, account, is_voice=False, mime_type="image/png")

    service = MediaService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        storage=LocalMediaStorage(tmp_path),
        whatsapp=StubWhatsApp(PNG_BYTES, "image/png"),
    )
    await service.download(media)
    await service.download(media)
    await service.understand(media, reader=StubReader(VISION_METHOD))
    await service.understand(media, reader=StubReader(VISION_METHOD))
    await db_session.flush()

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.MEDIA_PROCESSING] == 1
    assert totals[UsageEventType.STORAGE_USED] == len(PNG_BYTES)


# ---------------------------------------------------------------------- agent


class SessionHandle:
    """Hands the worker the test's own session, so its writes roll back."""

    def __init__(self, session) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


class FakeRedis:
    @property
    def client(self):
        return object()


class StubOrchestrator:
    """Returns a fixed turn without a provider.

    What is under test here is the worker's own responsibility - metering a turn
    it has just run - so the turn itself is a value rather than an inference.
    """

    def __init__(self, outcome) -> None:
        self._outcome = outcome

    async def answer(self, *, conversation_id, agent=None):
        return self._outcome


def _worker(monkeypatch, db_session, settings, outcome):
    from app.workers import ai_worker as worker_module

    monkeypatch.setattr(worker_module, "AgentOrchestrator", lambda **_: StubOrchestrator(outcome))
    monkeypatch.setattr(
        worker_module,
        "build_http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200))
        ),
    )
    return worker_module.AgentWorker(
        database=SessionHandle(db_session),
        redis=FakeRedis(),
        settings=settings,
    )


def _outcome(*, reply=None, handed_off=False, rounds=1):
    from app.agents.orchestrator import AgentOutcome

    return AgentOutcome(
        reply=reply,
        handed_off=handed_off,
        tools_run=(),
        usage=TokenUsage(input_tokens=420, output_tokens=60, total_tokens=480),
        rounds=rounds,
        model="gpt-5.1",
    )


async def test_an_agent_turn_meters_its_provider_calls_and_tokens(
    db_session,
    settings,
    meta,
    monkeypatch,
):
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    conversation = await _conversation(db_session, tenant, account)

    # Two tool rounds are two provider calls, and the tokens are their sum.
    worker = _worker(monkeypatch, db_session, settings, _outcome(reply="Certainly.", rounds=2))
    progress = _TurnProgress()
    await worker._handle(AgentJob(tenant_id=tenant.id, conversation_id=conversation.id), progress)
    await db_session.flush()

    # The turn reached the provider, so it is no longer safe to retry: a second
    # run would bill another inference and send the customer a second reply
    # (ADR-068). Asserted here rather than only in the unit suite, because this
    # is the one test that drives the real `_handle` end to end.
    assert progress.engaged is True

    totals = await _totals(db_session, tenant)
    assert totals[UsageEventType.AI_INPUT_TOKEN] == 420
    assert totals[UsageEventType.AI_OUTPUT_TOKEN] == 60
    # The reply went out, so it is counted too.
    assert totals[UsageEventType.WHATSAPP_MESSAGE_SENT] == 1
    # The *request* meter is no longer written here. It is taken per round by
    # the reservation the orchestrator calls before each provider call, so that
    # two workers cannot both spend the last permitted request. This stub
    # orchestrator makes no provider calls and so reserves nothing; the real
    # accounting is proved in `test_ai_security.py`.
    assert UsageEventType.AI_REQUEST not in totals


async def test_a_turn_that_says_nothing_is_still_metered(
    db_session,
    settings,
    monkeypatch,
):
    """A handoff cost the same inference as an answer.

    Metering only the turns that produced words would under-count exactly the
    conversations that took the most attention.
    """
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    conversation = await _conversation(db_session, tenant, account)

    worker = _worker(monkeypatch, db_session, settings, _outcome(handed_off=True))
    progress = _TurnProgress()
    await worker._handle(AgentJob(tenant_id=tenant.id, conversation_id=conversation.id), progress)
    await db_session.flush()

    # A handoff still engaged the provider, so it is still not retryable.
    assert progress.engaged is True

    totals = await _totals(db_session, tenant)
    # Tokens are metered whether or not the turn produced words - a handoff
    # cost the same inference as an answer. The request meter belongs to the
    # reservation now; see the note above.
    assert totals[UsageEventType.AI_INPUT_TOKEN] > 0
    assert UsageEventType.WHATSAPP_MESSAGE_SENT not in totals


# ------------------------------------------------------------------- isolation


async def test_metering_stays_inside_the_workspace_that_consumed_it(db_session, settings, meta):
    """Two workspaces, one busy and one silent. The silent one owes nothing."""
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    account = await _account(db_session, acme)
    conversation = await _conversation(db_session, acme, account)

    messaging = MessagingService(session=db_session, settings=settings, tenant_id=acme.id)
    await messaging.send_text(conversation_id=conversation.id, body="Hello again.")
    await db_session.flush()

    assert await _totals(db_session, rival) == {}
    assert (await _totals(db_session, acme))[UsageEventType.WHATSAPP_MESSAGE_SENT] == 1


async def test_a_window_that_ended_before_the_work_reports_nothing(db_session, settings, meta):
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant)
    conversation = await _conversation(db_session, tenant, account)

    messaging = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)
    await messaging.send_text(conversation_id=conversation.id, body="Hello again.")
    await db_session.flush()

    yesterday = datetime.now(UTC) - timedelta(days=1)
    rows = await UsageEventRepository(db_session, tenant_id=tenant.id).totals(
        since=yesterday - timedelta(days=1),
        until=yesterday,
    )
    assert rows == []
