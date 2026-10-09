"""The final audit's small findings, on PostgreSQL.

- OMNI-052: a connection's own sending allowance overrides the deployment's,
  in either direction, and null keeps the deployment's.
- OMNI-054: an authentication template is refused before staging for a
  conversation pinned to a business-scoped id, and still sent to a phone.
- OMNI-048: tenant analytics split by channel; the channel-filtered inbox is
  served by its own index.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.policy import PolicyRefusalError
from app.core.config import Settings
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
    IdentitySource,
)
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
from app.services import messaging_service as messaging_module
from app.services.analytics_service import AnalyticsService
from app.services.campaign_service import CampaignService
from app.services.messaging_service import MessagingService

pytestmark = pytest.mark.integration

INBOX_INDEX = "ix_conversations_tenant_id_channel_last_message_at"


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


def _phone() -> str:
    return f"2011{uuid.uuid4().int % 10**8:08d}"


async def _number(session: AsyncSession) -> tuple[Tenant, WhatsAppAccount]:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Low {tag}", slug=f"low-{tag}")
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-{tag}",
        waba_id=f"waba-{tag}",
        display_phone_number="+20 100 000 0000",
    )
    session.add(account)
    await session.flush()
    return tenant, account


async def _conversation(
    session: AsyncSession, tenant: Tenant, account: WhatsAppAccount
) -> Conversation:
    contact = Contact(tenant_id=tenant.id, wa_id=_phone())
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
    for _ in range(customers):
        conversation = await _conversation(session, tenant, account)
        session.add(
            CampaignRecipient(
                tenant_id=tenant.id, campaign_id=campaign.id, contact_id=conversation.contact_id
            )
        )
    await session.flush()
    return campaign


async def _allow(session: AsyncSession, account: WhatsAppAccount, per_minute: int | None) -> None:
    await session.execute(
        update(ChannelConnection)
        .where(ChannelConnection.id == account.id)
        .values(sends_per_minute=per_minute)
    )


async def _dispatch(
    session: AsyncSession, tenant: Tenant, campaign: Campaign, settings: Settings
) -> int:
    service = CampaignService(
        session=session,
        tenant_id=tenant.id,
        messaging=MessagingService(session=session, settings=settings, tenant_id=tenant.id),
    )
    outcome = await service.dispatch_batch(campaign, now=datetime.now(UTC))
    return outcome.sent


# ------------------------------------------------------------- OMNI-052


async def test_a_connections_own_allowance_holds_where_the_deployment_has_none(
    db_session: AsyncSession, meta: Meta
) -> None:
    tenant, account = await _number(db_session)
    campaign = await _campaign(db_session, tenant, account, customers=3)
    await _allow(db_session, account, 1)

    sent = await _dispatch(db_session, tenant, campaign, _settings())

    assert sent == 1
    assert len(meta.bodies) == 1


async def test_a_connections_own_allowance_can_be_larger_than_the_deployments(
    db_session: AsyncSession, meta: Meta
) -> None:
    tenant, account = await _number(db_session)
    campaign = await _campaign(db_session, tenant, account, customers=3)
    await _allow(db_session, account, 3)

    sent = await _dispatch(db_session, tenant, campaign, _settings(connection_sends_per_minute=1))

    assert sent == 3


async def test_null_keeps_the_deployments_allowance(db_session: AsyncSession, meta: Meta) -> None:
    tenant, account = await _number(db_session)
    campaign = await _campaign(db_session, tenant, account, customers=3)
    await _allow(db_session, account, None)

    sent = await _dispatch(db_session, tenant, campaign, _settings(connection_sends_per_minute=1))

    assert sent == 1


async def test_a_non_positive_allowance_is_refused_by_the_database(
    db_session: AsyncSession,
) -> None:
    _, account = await _number(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _allow(db_session, account, 0)


# ------------------------------------------------------------- OMNI-054


async def _authentication_conversation(
    session: AsyncSession, *, business_scoped: bool
) -> tuple[Tenant, Conversation]:
    tenant, account = await _number(session)
    session.add(
        WhatsAppTemplate(
            tenant_id=tenant.id,
            account_id=account.id,
            name="login_code",
            language="en",
            category=TemplateCategory.AUTHENTICATION,
            status=TemplateStatus.APPROVED,
        )
    )
    if not business_scoped:
        return tenant, await _conversation(session, tenant, account)

    contact = Contact(tenant_id=tenant.id, wa_id=None)
    session.add(contact)
    await session.flush()
    identity = ContactIdentity(
        tenant_id=tenant.id,
        contact_id=contact.id,
        channel=Channel.WHATSAPP,
        kind=IdentityKind.BSUID,
        scope=IdentityScope.PROVIDER_ACCOUNT,
        scope_ref=account.waba_id,
        value=f"EG.0synthetic{uuid.uuid4().hex[:12]}",
        source=IdentitySource.PROVIDER,
    )
    session.add(identity)
    await session.flush()
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        participant_identity_id=identity.id,
        last_inbound_at=datetime.now(UTC),
    )
    session.add(conversation)
    await session.flush()
    return tenant, conversation


async def test_an_authentication_template_to_a_business_scoped_id_is_refused_before_staging(
    db_session: AsyncSession, meta: Meta
) -> None:
    tenant, conversation = await _authentication_conversation(db_session, business_scoped=True)
    service = MessagingService(session=db_session, settings=_settings(), tenant_id=tenant.id)

    with pytest.raises(PolicyRefusalError):
        await service.send_template(
            conversation_id=conversation.id,
            name="login_code",
            language="en",
            origin=MessageOrigin.HUMAN,
        )

    staged = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )
    assert staged == 0
    assert meta.bodies == []


async def test_an_authentication_template_to_a_phone_is_still_sent(
    db_session: AsyncSession, meta: Meta
) -> None:
    tenant, conversation = await _authentication_conversation(db_session, business_scoped=False)
    service = MessagingService(session=db_session, settings=_settings(), tenant_id=tenant.id)

    message = await service.send_template(
        conversation_id=conversation.id,
        name="login_code",
        language="en",
        origin=MessageOrigin.HUMAN,
    )

    assert message.status is MessageStatus.SENT
    assert len(meta.bodies) == 1


# ------------------------------------------------------------- OMNI-048


async def test_analytics_split_traffic_by_channel(db_session: AsyncSession, meta: Meta) -> None:
    tenant, account = await _number(db_session)
    conversation = await _conversation(db_session, tenant, account)
    service = MessagingService(session=db_session, settings=_settings(), tenant_id=tenant.id)
    await service.send_text(
        conversation_id=conversation.id, body="Hello.", origin=MessageOrigin.HUMAN
    )

    now = datetime.now(UTC)
    report = await AnalyticsService(db_session, tenant_id=tenant.id).report(
        since=now - timedelta(hours=1), until=now + timedelta(hours=1)
    )

    figures = [
        (row.channel, row.conversations_created, row.messages_received, row.messages_sent)
        for row in report.channels
    ]
    assert figures == [(Channel.WHATSAPP, 1, 0, 1)]


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


async def test_the_channel_inbox_is_served_by_its_own_index(db_session: AsyncSession) -> None:
    """E5 from the final audit, on a workspace large enough to matter.

    Non-vacuity: with the index dropped inside this (rolled-back) transaction
    the same query no longer uses any index condition on channel.
    """
    tenant, account = await _number(db_session)
    await db_session.execute(
        text(
            "INSERT INTO contacts (id, tenant_id, wa_id) SELECT gen_random_uuid(), :tenant,"
            " '2014' || lpad(g::text, 8, '0') FROM generate_series(1, 5000) g"
        ),
        {"tenant": tenant.id},
    )
    await db_session.execute(
        text(
            "INSERT INTO conversations (id, tenant_id, contact_id, account_id, status, mode,"
            " last_message_at) SELECT gen_random_uuid(), c.tenant_id, c.id, :account, 'open',"
            " 'ai', now() - (random() * interval '30 days') FROM contacts c"
            " WHERE c.tenant_id = :tenant"
        ),
        {"tenant": tenant.id, "account": account.id},
    )
    await db_session.execute(text("ANALYZE conversations"))
    query = text(
        "EXPLAIN (FORMAT JSON) SELECT id FROM conversations WHERE tenant_id = :tenant"
        " AND status <> 'closed' AND channel = 'instagram'"
        " ORDER BY last_message_at DESC NULLS LAST, id DESC LIMIT 50"
    )

    async def plan() -> list[dict[str, Any]]:
        document = (await db_session.execute(query, {"tenant": tenant.id})).scalar_one()
        parsed = json.loads(document) if isinstance(document, str) else document
        return list(_nodes(parsed[0]["Plan"]))

    with_index = await plan()
    assert any(node.get("Index Name") == INBOX_INDEX for node in with_index), with_index

    await db_session.execute(text(f"DROP INDEX {INBOX_INDEX}"))
    without = await plan()
    assert not any("channel" in node.get("Index Cond", "") for node in without), without
