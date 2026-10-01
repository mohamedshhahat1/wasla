"""A connection's shared sending allowance, on PostgreSQL (OMNI-017, ADR-123).

The allowance is one conditional UPDATE on the connection row. What has to be
true of it is proved here against the real database: the arithmetic of the
window, that concurrent senders on independent connections can never admit more
than the allowance between them, that a campaign meeting it waits without
spending anybody's attempts, that a reply is counted but never refused - and
that with no allowance configured nothing is written at all, so ADR-026's
per-campaign rate stays exactly what it was.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus
from app.db.models.channel import ChannelConnection
from app.db.models.conversation import Contact, Conversation, MessageOrigin, MessageStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate
from app.repositories.channel_repository import ChannelConnectionRepository
from app.services import messaging_service as messaging_module
from app.services.campaign_service import CampaignService
from app.services.messaging_service import MessagingService

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class Meta:
    def __init__(self) -> None:
        self.bodies: list[dict[str, object]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "messages": [{"id": f"wamid.{uuid.uuid4().hex}"}],
                },
            )

        return httpx.MockTransport(handle)


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> Iterator[Meta]:
    fake = Meta()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=fake.transport()),
    )
    yield fake


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        meta_access_token="test-access-token",
        **overrides,  # type: ignore[arg-type]
    )


async def _number(session: AsyncSession) -> tuple[Tenant, WhatsAppAccount]:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Throughput {tag}", slug=f"throughput-{tag}")
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-{tag}",
        waba_id="waba-throughput",
        display_phone_number="+20 100 000 0000",
    )
    session.add(account)
    await session.flush()
    return tenant, account


async def _window(session: AsyncSession, account: WhatsAppAccount) -> tuple[datetime | None, int]:
    row = (
        await session.execute(
            select(ChannelConnection.send_window_started_at, ChannelConnection.send_window_count)
            .where(ChannelConnection.id == account.id)
            .execution_options(populate_existing=True)
        )
    ).one()
    return row[0], int(row[1])


# ------------------------------------------------------------ arithmetic


async def test_the_window_admits_its_allowance_then_says_when_to_return(
    db_session: AsyncSession,
) -> None:
    tenant, account = await _number(db_session)
    connections = ChannelConnectionRepository(db_session, tenant_id=tenant.id)

    admitted = [
        await connections.take_send_allowance(
            account.id, per_window=3, now=NOW + timedelta(seconds=step)
        )
        for step in range(3)
    ]
    refused = await connections.take_send_allowance(
        account.id, per_window=3, now=NOW + timedelta(seconds=10)
    )

    assert admitted == [None, None, None]
    # The window opened at the first send and closes a minute after it.
    assert refused == NOW + timedelta(minutes=1)
    assert await _window(db_session, account) == (NOW, 3)


async def test_a_reply_is_counted_even_over_the_allowance(db_session: AsyncSession) -> None:
    tenant, account = await _number(db_session)
    connections = ChannelConnectionRepository(db_session, tenant_id=tenant.id)
    await connections.take_send_allowance(account.id, per_window=1, now=NOW)

    counted = await connections.take_send_allowance(
        account.id, per_window=1, now=NOW + timedelta(seconds=1), may_refuse=False
    )

    assert counted is None
    assert await _window(db_session, account) == (NOW, 2)


async def test_an_expired_window_starts_again(db_session: AsyncSession) -> None:
    tenant, account = await _number(db_session)
    connections = ChannelConnectionRepository(db_session, tenant_id=tenant.id)
    await connections.take_send_allowance(account.id, per_window=1, now=NOW)
    later = NOW + timedelta(minutes=1, seconds=1)

    assert await connections.take_send_allowance(account.id, per_window=1, now=later) is None
    assert await _window(db_session, account) == (later, 1)


async def test_another_workspace_cannot_spend_this_allowance(db_session: AsyncSession) -> None:
    _, account = await _number(db_session)
    rival, _ = await _number(db_session)

    await ChannelConnectionRepository(db_session, tenant_id=rival.id).take_send_allowance(
        account.id, per_window=5, now=NOW
    )

    assert await _window(db_session, account) == (None, 0)


# ------------------------------------------------------------ concurrency


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Real commits on independent connections: the race is PostgreSQL's."""
    engine: AsyncEngine = create_async_engine(prepared_database, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("senders", [8])
async def test_concurrent_senders_never_admit_more_than_the_allowance(
    committing: async_sessionmaker[AsyncSession], senders: int
) -> None:
    async with committing() as session:
        tenant, account = await _number(session)
        await session.commit()
    barrier = asyncio.Barrier(senders)

    async def send() -> bool:
        async with committing() as session:
            await barrier.wait()
            refused = await ChannelConnectionRepository(
                session, tenant_id=tenant.id
            ).take_send_allowance(account.id, per_window=3, now=datetime.now(UTC))
            await session.commit()
            return refused is None

    try:
        results = await asyncio.gather(*(send() for _ in range(senders)))
        async with committing() as session:
            _, count = await _window(session, account)
        assert (sum(results), count) == (3, 3)
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


# ------------------------------------------------------- the senders


async def _campaign(
    session: AsyncSession, tenant: Tenant, account: WhatsAppAccount, customers: int
) -> Campaign:
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
        audience_size=customers,
        messages_per_minute=60,
    )
    session.add(campaign)
    await session.flush()
    for index in range(customers):
        contact = Contact(tenant_id=tenant.id, wa_id=f"20100000{index:04d}")
        session.add(contact)
        await session.flush()
        session.add(
            Conversation(
                tenant_id=tenant.id,
                contact_id=contact.id,
                account_id=account.id,
                last_inbound_at=datetime.now(UTC),
            )
        )
        session.add(
            CampaignRecipient(tenant_id=tenant.id, campaign_id=campaign.id, contact_id=contact.id)
        )
    await session.flush()
    return campaign


async def test_a_campaign_waits_for_its_numbers_window_without_spending_attempts(
    db_session: AsyncSession, meta: Meta
) -> None:
    tenant, account = await _number(db_session)
    campaign = await _campaign(db_session, tenant, account, customers=3)
    settings = _settings(connection_sends_per_minute=1)
    service = CampaignService(
        session=db_session,
        tenant_id=tenant.id,
        messaging=MessagingService(session=db_session, settings=settings, tenant_id=tenant.id),
    )
    moment = datetime.now(UTC)

    outcome = await service.dispatch_batch(campaign, now=moment)
    await db_session.flush()

    assert (outcome.sent, outcome.failed, outcome.skipped) == (1, 0, 0)
    assert len(meta.bodies) == 1
    recipients = (
        await db_session.execute(
            select(CampaignRecipient.status, CampaignRecipient.attempts).where(
                CampaignRecipient.campaign_id == campaign.id
            )
        )
    ).all()
    assert sorted((status.value, attempts) for status, attempts in recipients) == [
        ("pending", 0),
        ("pending", 0),
        ("sent", 0),
    ]
    started, _ = await _window(db_session, account)
    assert started is not None
    assert campaign.next_send_at is not None
    assert campaign.next_send_at >= started + timedelta(minutes=1)
    assert campaign.status is CampaignStatus.RUNNING


async def test_without_an_allowance_nothing_is_counted(
    db_session: AsyncSession, meta: Meta
) -> None:
    """Default off: a campaign is spaced by its own rate alone (ADR-026)."""
    tenant, account = await _number(db_session)
    campaign = await _campaign(db_session, tenant, account, customers=3)
    service = CampaignService(
        session=db_session,
        tenant_id=tenant.id,
        messaging=MessagingService(session=db_session, settings=_settings(), tenant_id=tenant.id),
    )

    outcome = await service.dispatch_batch(campaign, now=datetime.now(UTC))

    assert outcome.sent == 3
    assert await _window(db_session, account) == (None, 0)


async def test_a_reply_is_never_held_back_for_a_broadcast(
    db_session: AsyncSession, meta: Meta
) -> None:
    tenant, account = await _number(db_session)
    contact = Contact(tenant_id=tenant.id, wa_id="201000009999")
    db_session.add(contact)
    await db_session.flush()
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        last_inbound_at=datetime.now(UTC),
    )
    db_session.add(conversation)
    await db_session.flush()
    service = MessagingService(
        session=db_session, settings=_settings(connection_sends_per_minute=1), tenant_id=tenant.id
    )

    first = await service.send_text(
        conversation_id=conversation.id, body="One.", origin=MessageOrigin.HUMAN
    )
    second = await service.send_text(
        conversation_id=conversation.id, body="Two.", origin=MessageOrigin.AGENT
    )

    assert (first.status, second.status) == (MessageStatus.SENT, MessageStatus.SENT)
    _, count = await _window(db_session, account)
    assert count == 2
