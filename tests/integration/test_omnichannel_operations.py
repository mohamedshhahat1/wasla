"""The operational half of the foundation, on PostgreSQL.

- **Credential health** (OMNI-012): a send the provider refuses as unauthorised
  marks its connection `auth_failed`; the next send that works clears it.
- **A campaign sends through its own connection, to the identity it was built
  for** (OMNI-004, mutant M10).
- **The event log, recovery and retention are every channel's** (OMNI-007): an
  event the synthetic channel stored while its queue was down is finished by
  the same sweep as WhatsApp's, and its payload ages out the same way.
- **The new gauges** (OMNI-021): connections by channel, status and health,
  and the inbound backlog by channel - closed labels only.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.channels.outcomes import ProviderAuthError
from app.core.config import Settings
from app.core.metrics import MetricsRegistry
from app.core.redis import RedisClient
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionHealth,
    ConnectionStatus,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
    IdentitySource,
)
from app.db.models.channel_event import ChannelEvent, ChannelEventKind, ChannelEventState
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate
from app.repositories.campaign_repository import AudienceFilter
from app.repositories.channel_event_repository import WebhookPayloadRetention
from app.services import messaging_service as messaging_module
from app.services.campaign_service import CampaignService
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.messaging_service import MessagingService
from app.services.metrics_service import MetricsService
from app.workers.inbound_recovery import InboundRecoveryWorker
from app.workers.media_queue import MediaQueue
from app.workers.queue import AgentJob, AgentQueue
from tests.channel_fakes import SyntheticAdapter, synthetic_payload
from tests.fake_queue_redis import FakeQueueRedis
from tests.fakes import as_database

pytestmark = pytest.mark.integration


class SessionHandle:
    """Hands a worker the test's own session, so its writes roll back."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[Any] = []

    async def enqueue(self, job: Any) -> None:
        self.jobs.append(job)


class BrokenQueue:
    async def enqueue(self, job: Any) -> None:
        raise RedisError("Redis is down.")


class Meta:
    """Meta's messages endpoint, answering with whatever `status` says."""

    def __init__(self) -> None:
        self.status = 200
        self.bodies: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            if self.status == 401:
                return httpx.Response(
                    401,
                    json={
                        "error": {
                            "message": "Error validating access token.",
                            "type": "OAuthException",
                            "code": 190,
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "messages": [{"id": f"wamid.{uuid.uuid4().hex}"}],
                },
            )

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
def meta(monkeypatch: pytest.MonkeyPatch) -> Iterator[Meta]:
    fake = Meta()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=fake.transport()),
    )
    yield fake


async def _tenant(session: AsyncSession) -> Tenant:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Operations {tag}", slug=f"operations-{tag}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _number(session: AsyncSession, tenant: Tenant) -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-{uuid.uuid4().hex[:10]}",
        waba_id="waba-operations",
        display_phone_number="+20 100 000 0000",
    )
    session.add(account)
    await session.flush()
    return account


async def _customer(
    session: AsyncSession, tenant: Tenant, account: WhatsAppAccount, wa_id: str
) -> tuple[Contact, Conversation]:
    contact = Contact(tenant_id=tenant.id, wa_id=wa_id)
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
    return contact, conversation


async def _health(session: AsyncSession, account: WhatsAppAccount) -> ConnectionHealth:
    health: ConnectionHealth | None = await session.scalar(
        select(ChannelConnection.health)
        .where(ChannelConnection.id == account.id)
        .execution_options(populate_existing=True)
    )
    assert health is not None
    return health


# ---------------------------------------------- credential health (OMNI-012)


async def test_a_refused_credential_marks_the_connection_and_a_working_one_clears_it(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    _, conversation = await _customer(db_session, tenant, account, "201000000401")
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)
    meta.status = 401

    with pytest.raises(ProviderAuthError):
        await service.send_text(
            conversation_id=conversation.id, body="Hello", origin=MessageOrigin.HUMAN
        )

    assert await _health(db_session, account) is ConnectionHealth.AUTH_FAILED
    failed = await db_session.scalar(
        select(Message.status).where(Message.conversation_id == conversation.id)
    )
    assert failed is MessageStatus.FAILED

    meta.status = 200
    sent = await service.send_text(
        conversation_id=conversation.id, body="Hello again", origin=MessageOrigin.HUMAN
    )

    assert sent.status is MessageStatus.SENT
    assert await _health(db_session, account) is ConnectionHealth.OK


# ---------------------------------------------------- campaigns (M10)


async def _campaign(session: AsyncSession, tenant: Tenant, account: WhatsAppAccount) -> Campaign:
    template = WhatsAppTemplate(
        tenant_id=tenant.id,
        account_id=account.id,
        name="restock",
        language="en",
        category=TemplateCategory.MARKETING,
        status=TemplateStatus.APPROVED,
    )
    session.add(template)
    await session.flush()
    campaign = Campaign(
        tenant_id=tenant.id,
        account_id=account.id,
        template_id=template.id,
        name="Restock",
        status=CampaignStatus.RUNNING,
        audience_size=1,
        messages_per_minute=60,
    )
    session.add(campaign)
    await session.flush()
    return campaign


async def test_a_campaign_audience_records_the_identity_each_copy_goes_to(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    _, conversation = await _customer(db_session, tenant, account, "201000000411")
    campaign = await _campaign(db_session, tenant, account)
    campaign.status = CampaignStatus.DRAFT
    await db_session.flush()

    await CampaignService(session=db_session, tenant_id=tenant.id).set_audience(
        campaign_id=campaign.id, filters=AudienceFilter()
    )
    await db_session.flush()

    (recipient,) = (
        await db_session.execute(
            select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
        )
    ).scalars()
    assert recipient.participant_identity_id == conversation.participant_identity_id


async def test_a_campaign_never_sends_through_another_numbers_conversation(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    """M10: the recipient names the same customer's conversation on the
    workspace's *other* number. It is refused, not sent through."""
    tenant = await _tenant(db_session)
    account, other = await _number(db_session, tenant), await _number(db_session, tenant)
    contact, _ = await _customer(db_session, tenant, account, "201000000421")
    elsewhere = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=other.id,
        last_inbound_at=datetime.now(UTC),
    )
    db_session.add(elsewhere)
    await db_session.flush()
    campaign = await _campaign(db_session, tenant, account)
    recipient = CampaignRecipient(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        contact_id=contact.id,
        conversation_id=elsewhere.id,
    )
    db_session.add(recipient)
    await db_session.flush()
    service = CampaignService(
        session=db_session,
        tenant_id=tenant.id,
        messaging=MessagingService(session=db_session, settings=settings, tenant_id=tenant.id),
    )

    outcome = await service.dispatch_batch(campaign, now=datetime.now(UTC))

    assert (outcome.sent, outcome.skipped) == (0, 1)
    assert meta.bodies == []
    await db_session.refresh(recipient)
    assert recipient.status is RecipientStatus.SKIPPED
    assert recipient.last_error == "This copy names a conversation outside this campaign's number."


async def test_a_campaign_refuses_a_copy_whose_conversation_no_longer_addresses_its_identity(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    """The copy was built for one of the contact's identities; the
    conversation on this number addresses another. It is skipped, not sent
    to whichever address the conversation happens to hold."""
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact, conversation = await _customer(db_session, tenant, account, "201000000431")
    business_scoped = ContactIdentity(
        tenant_id=tenant.id,
        contact_id=contact.id,
        channel=Channel.WHATSAPP,
        kind=IdentityKind.BSUID,
        scope=IdentityScope.PROVIDER_ACCOUNT,
        scope_ref=account.waba_id,
        value="EG.0campaign0copy",
        source=IdentitySource.PROVIDER,
    )
    db_session.add(business_scoped)
    await db_session.flush()
    assert conversation.participant_identity_id != business_scoped.id
    campaign = await _campaign(db_session, tenant, account)
    recipient = CampaignRecipient(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        contact_id=contact.id,
        participant_identity_id=business_scoped.id,
    )
    db_session.add(recipient)
    await db_session.flush()
    service = CampaignService(
        session=db_session,
        tenant_id=tenant.id,
        messaging=MessagingService(session=db_session, settings=settings, tenant_id=tenant.id),
    )

    outcome = await service.dispatch_batch(campaign, now=datetime.now(UTC))

    assert (outcome.sent, outcome.skipped) == (0, 1)
    assert meta.bodies == []
    await db_session.refresh(recipient)
    assert recipient.status is RecipientStatus.SKIPPED
    assert recipient.last_error is not None
    assert "no longer addresses the identity" in recipient.last_error


async def test_a_legacy_recipient_records_the_identity_its_copy_went_to(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    """A recipient materialised before identities existed names none; the send
    records the one it used, so the row still answers "sent to whom"."""
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact, conversation = await _customer(db_session, tenant, account, "201000000451")
    campaign = await _campaign(db_session, tenant, account)
    recipient = CampaignRecipient(
        tenant_id=tenant.id, campaign_id=campaign.id, contact_id=contact.id
    )
    db_session.add(recipient)
    await db_session.flush()
    service = CampaignService(
        session=db_session,
        tenant_id=tenant.id,
        messaging=MessagingService(session=db_session, settings=settings, tenant_id=tenant.id),
    )

    outcome = await service.dispatch_batch(campaign, now=datetime.now(UTC))

    assert outcome.sent == 1
    ((body),) = meta.bodies
    assert body["to"] == "201000000451"
    await db_session.refresh(recipient)
    assert recipient.participant_identity_id == conversation.participant_identity_id


# ------------------------------------------- recovery and retention (OMNI-007)


async def _synthetic(session: AsyncSession, tenant: Tenant) -> ChannelConnection:
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


async def test_another_channels_owed_event_is_finished_by_the_same_sweep(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session)
    connection = await _synthetic(db_session, tenant)
    adapter = SyntheticAdapter()
    payload = synthetic_payload(
        connection.external_account_id,
        {
            "type": "message",
            "id": "syn.owed.1",
            "from": "igsid-owed",
            "at": int(datetime.now(UTC).timestamp()),
            "text": "hi",
        },
    )
    outcome = await ChannelIngestionService(
        session=db_session,
        adapter=cast(ChannelAdapter, adapter),
        queue=cast(AgentQueue, BrokenQueue()),
        media_queue=cast(MediaQueue, RecordingQueue()),
    ).ingest(adapter.parse(payload))
    assert (outcome.stored, outcome.queued) == (1, 0)
    event = await db_session.scalar(
        select(ChannelEvent).where(ChannelEvent.event_id == "syn.owed.1")
    )
    assert event is not None and event.state is ChannelEventState.RECEIVED
    assert event.channel is Channel.INSTAGRAM
    worker = InboundRecoveryWorker(
        database=as_database(SessionHandle(db_session)),
        redis=cast(RedisClient, SimpleNamespace(client=FakeQueueRedis())),
        settings=settings,
        grace_seconds=0.0,
    )
    agents = RecordingQueue()
    worker._agent_queue = cast(AgentQueue, agents)

    await worker.run_once(now=datetime.now(UTC) + timedelta(seconds=1))

    (job,) = agents.jobs
    assert isinstance(job, AgentJob)
    message = await db_session.scalar(select(Message).where(Message.wa_message_id == "syn.owed.1"))
    assert message is not None and job.trigger_message_id == message.id
    settled = await db_session.scalar(
        select(ChannelEvent.state)
        .where(ChannelEvent.id == event.id)
        .execution_options(populate_existing=True)
    )
    assert settled is ChannelEventState.PROCESSED


async def test_recovery_never_answers_a_message_that_is_not_a_customers(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    """An owed event whose id names Wasla's own outbound message is given up,
    not answered - the sweep has the same backstop ingestion has."""
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    _, conversation = await _customer(db_session, tenant, account, "201000000441")
    sent = await MessagingService(
        session=db_session, settings=settings, tenant_id=tenant.id
    ).send_text(conversation_id=conversation.id, body="Ours.", origin=MessageOrigin.HUMAN)
    assert sent.wa_message_id is not None
    db_session.add(
        ChannelEvent(
            tenant_id=tenant.id,
            account_id=account.id,
            event_id=sent.wa_message_id,
            kind=ChannelEventKind.MESSAGE,
            payload={"seeded": True},
            received_at=datetime.now(UTC),
            state=ChannelEventState.RECEIVED,
        )
    )
    await db_session.flush()
    worker = InboundRecoveryWorker(
        database=as_database(SessionHandle(db_session)),
        redis=cast(RedisClient, SimpleNamespace(client=FakeQueueRedis())),
        settings=settings,
        grace_seconds=0.0,
    )
    agents = RecordingQueue()
    worker._agent_queue = cast(AgentQueue, agents)

    await worker.run_once(now=datetime.now(UTC) + timedelta(seconds=1))

    assert agents.jobs == []
    state = await db_session.scalar(
        select(ChannelEvent.state)
        .where(ChannelEvent.event_id == sent.wa_message_id)
        .execution_options(populate_existing=True)
    )
    assert state is ChannelEventState.FAILED


async def test_another_channels_payloads_age_out_like_whatsapps(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    connection = await _synthetic(db_session, tenant)
    long_ago = datetime(2019, 1, 1, tzinfo=UTC)
    event = ChannelEvent(
        tenant_id=tenant.id,
        account_id=connection.id,
        channel=Channel.INSTAGRAM,
        event_id=f"syn.retained.{uuid.uuid4().hex}",
        kind=ChannelEventKind.MESSAGE,
        payload={"text": "a customer's words"},
        received_at=long_ago,
        state=ChannelEventState.PROCESSED,
        processed_at=long_ago,
    )
    db_session.add(event)
    await db_session.flush()

    cleared = await WebhookPayloadRetention(db_session).redact(
        older_than=long_ago + timedelta(days=1), now=datetime.now(UTC), limit=100
    )

    assert cleared == 1
    await db_session.refresh(event)
    assert event.payload is None
    assert event.payload_redacted_at is not None


# ------------------------------------------------------- gauges (OMNI-021)


async def test_connections_and_backlog_are_counted_by_closed_labels(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    await db_session.execute(
        update(ChannelConnection)
        .where(ChannelConnection.id == account.id)
        .values(health=ConnectionHealth.AUTH_FAILED)
    )
    connection = await _synthetic(db_session, tenant)
    db_session.add(
        ChannelEvent(
            tenant_id=tenant.id,
            account_id=connection.id,
            channel=Channel.INSTAGRAM,
            event_id=f"syn.backlog.{uuid.uuid4().hex}",
            kind=ChannelEventKind.MESSAGE,
            payload={"seeded": True},
            received_at=datetime.now(UTC),
            state=ChannelEventState.RECEIVED,
        )
    )
    await db_session.flush()
    await db_session.execute(
        update(ChannelEvent)
        .where(ChannelEvent.account_id == connection.id)
        .values(created_at=datetime.now(UTC) - timedelta(hours=1))
    )

    lines = await MetricsService(
        None, registry=MetricsRegistry(), database=as_database(SessionHandle(db_session))
    )._messaging_lines(now=datetime.now(UTC))

    rendered = "\n".join(lines)
    assert (
        'wasla_channel_connections{channel="whatsapp",health="auth_failed",status="active"}'
        in rendered
    )
    assert 'wasla_channel_connections{channel="instagram",health="ok",status="active"}' in rendered
    assert 'wasla_unprocessed_inbound_events_by_channel{channel="instagram"} 1' in rendered
    assert str(tenant.id) not in rendered and str(account.id) not in rendered
