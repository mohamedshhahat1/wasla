"""The inbox renders every channel; a paused channel is visible and inert (OMNI-031).

`GET /conversations` used to compute each conversation's reply policy through a
registry that raises for a channel without an adapter, so one Instagram-labelled
conversation in a workspace turned the whole page into a 422 - WhatsApp threads
included - and removing a misbehaving adapter, the only switch a channel had,
broke every affected workspace's inbox.

Every request here goes through the real application - routes, dependencies,
`_present_all`, the messaging service - against PostgreSQL; only the registry is
chosen per test, through the `get_channel_registry` dependency the routes use.

Mutants this suite kills: M-O10 (raising in `policy_for` during render) and
M-O11 (a paused channel allowing outbound).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.lifecycle import outcome_for, serving_state
from app.api.dependencies import get_channel_registry, get_entitlement_service
from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelPausedError, ChannelRegistry
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models import Membership, Tenant, TenantRole, User
from app.db.models.agent_turn import TurnOutcome
from app.db.models.campaign import CampaignRecipient, RecipientStatus
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import Conversation, Message, MessageOrigin
from app.db.models.enums import TenantStatus
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.main import create_app
from app.services.campaign_service import CampaignService
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.follow_up_service import FollowUpService
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from tests.channel_fakes import SyntheticAdapter, synthetic_payload
from tests.channel_plans import allow_channels
from tests.conftest import AllowingEntitlements, FakeDependency
from tests.integration.test_omnichannel_operations import (  # noqa: F401 - `meta` is a fixture
    Meta,
    _campaign,
    _customer,
    _number,
    _tenant,
    meta,
)

pytestmark = pytest.mark.integration


def _registry(*, instagram: bool, paused: tuple[Channel, ...] = ()) -> ChannelRegistry:
    adapters: dict[Channel, ChannelAdapter] = {Channel.WHATSAPP: WhatsAppAdapter()}
    if instagram:
        adapters[Channel.INSTAGRAM] = cast(ChannelAdapter, SyntheticAdapter(Channel.INSTAGRAM))
    return ChannelRegistry(adapters, unmetered=True, paused=paused)


@dataclass
class Desk:
    tenant: Tenant
    headers: dict[str, str]
    whatsapp: Conversation
    instagram: Conversation
    instagram_connection: ChannelConnection


@pytest.fixture
def chosen() -> dict[str, ChannelRegistry]:
    return {"registry": _registry(instagram=False)}


@pytest.fixture
def inbox_app(
    settings: Settings,
    db_session: AsyncSession,
    fake_redis: FakeDependency,
    chosen: dict[str, ChannelRegistry],
) -> Iterator[FastAPI]:
    application = create_app(settings)
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = fake_redis

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    application.dependency_overrides[get_channel_registry] = lambda: chosen["registry"]
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(inbox_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=inbox_app), base_url="http://t") as client:
        yield client


@pytest_asyncio.fixture
async def desk(db_session: AsyncSession, settings: Settings) -> Desk:
    tag = uuid.uuid4().hex[:8]
    tenant = Tenant(name=f"Inbox {tag}", slug=f"inbox-{tag}", status=TenantStatus.ACTIVE)
    owner = User(
        email=f"owner@{tag}.example",
        hashed_password="x",
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    db_session.add_all([tenant, owner])
    await db_session.flush()
    # Follow-ups run on Instagram only under a plan that includes it (ENT-16).
    await allow_channels(db_session, tenant.id)
    db_session.add(Membership(tenant_id=tenant.id, user_id=owner.id, role=TenantRole.TENANT_OWNER))
    number = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"pn-{tag}",
        waba_id=f"waba-{tag}",
        display_phone_number="+201000000000",
        status=WhatsAppAccountStatus.ACTIVE,
    )
    instagram = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"ig-{tag}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add_all([number, instagram])
    await db_session.flush()
    now = int(datetime.now(UTC).timestamp())
    await WhatsAppIngestionService(session=db_session).ingest(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": number.phone_number_id},
                                "contacts": [{"wa_id": "201000000909"}],
                                "messages": [
                                    {
                                        "from": "201000000909",
                                        "id": f"wamid.{uuid.uuid4().hex}",
                                        "timestamp": str(now),
                                        "type": "text",
                                        "text": {"body": "hello"},
                                    }
                                ],
                            },
                        }
                    ]
                }
            ],
        }
    )
    adapter = SyntheticAdapter(Channel.INSTAGRAM)
    await ChannelIngestionService(session=db_session, adapter=cast(ChannelAdapter, adapter)).ingest(
        adapter.parse(
            synthetic_payload(
                instagram.external_account_id,
                {"type": "message", "id": f"m.{tag}", "from": "igsid-x", "at": now, "text": "hi"},
            )
        )
    )
    await db_session.flush()
    whatsapp = await db_session.scalar(
        select(Conversation).where(Conversation.account_id == number.id)
    )
    on_instagram = await db_session.scalar(
        select(Conversation).where(Conversation.account_id == instagram.id)
    )
    assert whatsapp is not None and on_instagram is not None
    token, _ = create_access_token(
        settings=settings, subject=owner.id, tenant_id=tenant.id, token_version=owner.token_version
    )
    return Desk(
        tenant=tenant,
        headers={"Authorization": f"Bearer {token}"},
        whatsapp=whatsapp,
        instagram=on_instagram,
        instagram_connection=instagram,
    )


def _by_id(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in page["items"]}


async def test_a_page_mixing_an_operable_and_an_unregistered_channel_renders(
    http: AsyncClient, desk: Desk
) -> None:
    response = await http.get("/api/v1/conversations", headers=desk.headers)

    assert response.status_code == 200
    items = _by_id(response.json())
    assert set(items) == {str(desk.whatsapp.id), str(desk.instagram.id)}
    instagram = items[str(desk.instagram.id)]
    assert instagram["service_window_open"] is False
    assert instagram["reply_policy"]["state"] == "unavailable"
    assert instagram["reply_policy"]["free_text_allowed"] is False
    assert instagram["reply_policy"]["templates"] is False
    whatsapp = items[str(desk.whatsapp.id)]
    assert whatsapp["reply_policy"]["state"] == "operational"
    assert whatsapp["reply_policy"]["free_text_allowed"] is True
    assert whatsapp["service_window_open"] is True


async def test_the_channel_filter_and_the_detail_route_render_too(
    http: AsyncClient, desk: Desk
) -> None:
    filtered = await http.get(
        "/api/v1/conversations", params={"channel": "whatsapp"}, headers=desk.headers
    )
    detail = await http.get(f"/api/v1/conversations/{desk.instagram.id}", headers=desk.headers)

    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()["items"]] == [str(desk.whatsapp.id)]
    assert detail.status_code == 200
    assert detail.json()["reply_policy"]["state"] == "unavailable"


async def test_a_paused_channel_is_listed_and_refuses_every_send(
    http: AsyncClient, desk: Desk, chosen: dict[str, ChannelRegistry], db_session: AsyncSession
) -> None:
    chosen["registry"] = _registry(instagram=True, paused=(Channel.INSTAGRAM,))

    page = await http.get("/api/v1/conversations", headers=desk.headers)
    detail = await http.get(f"/api/v1/conversations/{desk.instagram.id}", headers=desk.headers)
    before = await db_session.scalar(select(func.count()).select_from(Message))
    sent = await http.post(
        f"/api/v1/conversations/{desk.instagram.id}/messages",
        json={"body": "hello"},
        headers=desk.headers,
    )
    after = await db_session.scalar(select(func.count()).select_from(Message))

    assert page.status_code == 200
    instagram = _by_id(page.json())[str(desk.instagram.id)]
    assert instagram["reply_policy"]["state"] == "paused"
    assert instagram["reply_policy"]["free_text_allowed"] is False
    # The paused channel's own limits are still described.
    assert instagram["reply_policy"]["text_limit_unit"] == "utf8_bytes"
    assert detail.status_code == 200
    assert sent.status_code == 422
    assert "paused" in sent.json()["error"]["message"]
    # Refused before anything was staged.
    assert after == before


async def test_rolling_a_channel_back_never_changes_whatsapps_inbox(
    http: AsyncClient, desk: Desk, chosen: dict[str, ChannelRegistry]
) -> None:
    """Operational, then paused, then removed: WhatsApp's rendering is identical throughout."""
    seen = []
    for registry in (
        _registry(instagram=True),
        _registry(instagram=True, paused=(Channel.INSTAGRAM,)),
        _registry(instagram=False),
    ):
        chosen["registry"] = registry
        page = await http.get("/api/v1/conversations", headers=desk.headers)
        assert page.status_code == 200
        seen.append(_by_id(page.json())[str(desk.whatsapp.id)])

    assert seen[0] == seen[1] == seen[2]


async def test_a_paused_channel_keeps_its_inbound_and_refuses_its_agent(
    db_session: AsyncSession, desk: Desk
) -> None:
    paused = _registry(instagram=True, paused=(Channel.INSTAGRAM,))
    adapter = SyntheticAdapter(Channel.INSTAGRAM)

    outcome = await ChannelIngestionService(
        session=db_session, adapter=cast(ChannelAdapter, adapter)
    ).ingest(
        adapter.parse(
            synthetic_payload(
                desk.instagram_connection.external_account_id,
                {
                    "type": "message",
                    "id": f"m.{uuid.uuid4().hex}",
                    "from": "igsid-x",
                    "at": int(datetime.now(UTC).timestamp()),
                    "text": "still there?",
                },
            )
        )
    )

    assert outcome.stored == 1
    paused_state = await serving_state(
        db_session,
        tenant_id=desk.tenant.id,
        conversation_id=desk.instagram.id,
        agent_id=None,
        channels=paused,
    )
    operational_state = await serving_state(
        db_session,
        tenant_id=desk.tenant.id,
        conversation_id=desk.instagram.id,
        agent_id=None,
        channels=_registry(instagram=True),
    )
    assert paused_state.channel_available is False
    assert operational_state.channel_available is True
    # An agent's turn on it ends as the channel's refusal, not a failure.
    assert outcome_for(replace(paused_state, agent_active=True)) is TurnOutcome.SUPPRESSED_CHANNEL
    assert outcome_for(replace(operational_state, agent_active=True)) is None


async def test_a_paused_channel_refuses_at_the_send_choke_point(
    db_session: AsyncSession, desk: Desk, settings: Settings
) -> None:
    messaging = MessagingService(
        session=db_session,
        settings=settings,
        tenant_id=desk.tenant.id,
        channels=_registry(instagram=True, paused=(Channel.INSTAGRAM,)),
    )

    with pytest.raises(ChannelPausedError):
        await messaging.send_text(
            conversation_id=desk.instagram.id, body="hello", origin=MessageOrigin.HUMAN
        )


async def test_a_follow_up_on_a_paused_channel_waits_without_spending_an_attempt(
    db_session: AsyncSession, desk: Desk, settings: Settings
) -> None:
    paused = _registry(instagram=True, paused=(Channel.INSTAGRAM,))
    service = FollowUpService(
        session=db_session, tenant_id=desk.tenant.id, settings=settings, channels=paused
    )
    follow_up = FollowUp(
        tenant_id=desk.tenant.id,
        conversation_id=desk.instagram.id,
        status=FollowUpStatus.PENDING,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        body="checking in",
        created_by_kind="user",
    )
    db_session.add(follow_up)
    await db_session.flush()

    result = await service.dispatch(follow_up)

    assert result.follow_up.status is FollowUpStatus.PENDING
    assert result.follow_up.attempts == 0
    assert result.follow_up.scheduled_at > datetime.now(UTC) + timedelta(minutes=10)
    assert result.detail == "The channel is paused."


async def test_a_campaign_on_a_paused_channel_waits_and_spends_nothing(
    db_session: AsyncSession, settings: Settings, meta: Meta  # noqa: F811 - the fixture
) -> None:
    """WhatsApp paused: no recipient fails, no attempt is spent, the campaign waits."""
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact, conversation = await _customer(db_session, tenant, account, "201000000431")
    campaign = await _campaign(db_session, tenant, account)
    recipient = CampaignRecipient(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        contact_id=contact.id,
        conversation_id=conversation.id,
    )
    db_session.add(recipient)
    await db_session.flush()
    paused = ChannelRegistry({Channel.WHATSAPP: WhatsAppAdapter()}, paused=(Channel.WHATSAPP,))
    service = CampaignService(
        session=db_session,
        tenant_id=tenant.id,
        messaging=MessagingService(
            session=db_session, settings=settings, tenant_id=tenant.id, channels=paused
        ),
    )
    now = datetime.now(UTC)

    outcome = await service.dispatch_batch(campaign, now=now)

    assert (outcome.sent, outcome.failed, outcome.skipped) == (0, 0, 0)
    assert meta.bodies == []
    await db_session.refresh(recipient)
    assert recipient.status is RecipientStatus.PENDING
    assert recipient.attempts == 0
    assert recipient.message_id is None
    assert campaign.next_send_at is not None
    assert campaign.next_send_at >= now + timedelta(minutes=10)
