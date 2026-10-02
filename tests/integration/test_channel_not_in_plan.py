"""Nothing automated runs on a channel the plan in force excludes (ENT-16).

A workspace whose subscription is not served falls back to the default plan,
and a channel its paid plan included may not be one the default includes. On
such a connection:

- inbound is stored and visible - never refused (ADR-030);
- a person may still reply;
- AI turns end `channel_not_in_plan` before any hold, provider call or charge;
- campaign copies and follow-ups are skipped with reason `channel_not_in_plan`.

WhatsApp, in every plan, keeps answering within the default plan's allowance.
`past_due` still resolves the paid plan; paying restores it; nothing needs
re-enabling. AI turns run through the real `AgentWorker` (OpenAI and Meta faked
at the transport, `ai_harness`); follow-ups through the real dispatch.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter, TextContent
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.db.models.agent_turn import AgentTurn
from app.db.models.billing import LimitKey, Subscription, SubscriptionStatus
from app.db.models.campaign import CampaignRecipient, RecipientStatus
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import Conversation, Message, MessageDirection, MessageOrigin
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind
from app.db.models.tenant import Tenant
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.entitlement_service import CHANNEL_NOT_IN_PLAN_DETAIL
from app.services.follow_up_service import FollowUpService
from app.services.messaging_service import MessagingService
from app.workers.campaign_worker import CampaignWorker
from app.workers.queue import AgentQueue
from tests.channel_fakes import SyntheticAdapter, synthetic_payload
from tests.channel_plans import plan_with_channels, subscribe
from tests.fakes import as_database, as_messaging
from tests.integration.ai_harness import (
    DEFAULT_REPLY,
    FakeProviders,
    TurnRunner,
    Workspace,
    _TakesTheHandoff,
)
from tests.integration.test_campaign_worker import (
    SessionHandle,
    StubMessaging,
    _sending_campaign,
)

pytestmark = pytest.mark.integration

TURNS = LimitKey.PERIOD_AI_TURNS.value


async def _paid_workspace(
    ai_turns: TurnRunner, status: SubscriptionStatus
) -> tuple[Workspace, ChannelConnection, SyntheticAdapter, ChannelRegistry]:
    """A workspace on a paid plan with WhatsApp and Instagram, its subscription at `status`.

    The default plan - what a workspace not served falls back to - is the
    runner's own, which states no channel types and so allows WhatsApp alone.
    """
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    adapter = SyntheticAdapter(Channel.INSTAGRAM)
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.INSTAGRAM: cast(ChannelAdapter, adapter),
        },
    )
    async with ai_turns.database.session() as session:
        page = ChannelConnection(
            id=uuid.uuid4(),
            tenant_id=workspace.tenant_id,
            channel=Channel.INSTAGRAM,
            external_account_id=f"ig-{uuid.uuid4().hex[:10]}",
            status=ConnectionStatus.ACTIVE,
            ownership_started_at=datetime.now(UTC) - timedelta(days=2),
        )
        session.add(page)
        paid = await plan_with_channels(
            session,
            channels=[Channel.WHATSAPP, Channel.INSTAGRAM],
            limits={TURNS: 10, "channel_connections": 5},
        )
        ai_turns.plans.append(paid.code)
        await subscribe(session, workspace.tenant_id, paid, status=status)
    return workspace, page, adapter, registry


async def _instagram_writes(
    ai_turns: TurnRunner, adapter: SyntheticAdapter, page: ChannelConnection
) -> tuple[uuid.UUID, uuid.UUID]:
    mid = f"m.{uuid.uuid4().hex}"
    async with ai_turns.database.session() as session:
        await ChannelIngestionService(
            session=session,
            adapter=cast(ChannelAdapter, adapter),
            queue=cast(AgentQueue, _TakesTheHandoff()),
        ).ingest(
            adapter.parse(
                synthetic_payload(
                    page.external_account_id,
                    {
                        "type": "message",
                        "id": mid,
                        "from": "igsid-plan",
                        "at": int(datetime.now(UTC).timestamp()),
                        "text": "Do you deliver?",
                    },
                )
            )
        )
        message = await session.scalar(select(Message).where(Message.wa_message_id == mid))
        assert message is not None
        return message.conversation_id, message.id


async def _turn(
    ai_turns: TurnRunner,
    workspace: Workspace,
    registry: ChannelRegistry,
    conversation_id: uuid.UUID,
    trigger: uuid.UUID,
) -> None:
    await ai_turns.enqueue(workspace, conversation_id, trigger)
    worker = ai_turns.worker()
    worker._channels = registry
    assert await worker.run_once(wait_seconds=1)


async def _turn_for(ai_turns: TurnRunner, trigger: uuid.UUID) -> AgentTurn:
    async with ai_turns.database.session() as session:
        turn = await session.scalar(
            select(AgentTurn).where(AgentTurn.trigger_message_id == trigger)
        )
        assert turn is not None
        return turn


async def test_suspended_the_instagram_ai_is_refused_and_whatsapp_still_answers(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """M-E27's killer."""
    workspace, page, adapter, registry = await _paid_workspace(
        ai_turns, SubscriptionStatus.SUSPENDED
    )

    instagram, trigger = await _instagram_writes(ai_turns, adapter, page)
    await _turn(ai_turns, workspace, registry, instagram, trigger)

    # The inbound is stored and visible; the turn ended before anything ran.
    turn = await _turn_for(ai_turns, trigger)
    assert turn.outcome is not None and turn.outcome.value == "channel_not_in_plan"
    assert (turn.charge_state, turn.held_at) == (None, None)
    assert ai_providers.inference == 0 and ai_providers.sentiment == 0
    assert adapter.log.sent == []
    assert "ai_turn" not in await ai_turns.usage(workspace.tenant_id)

    # WhatsApp is in every plan: answered, within the default plan's turns.
    whatsapp, ids = await ai_turns.write(workspace, ["hello"])
    await _turn(ai_turns, workspace, registry, whatsapp, ids[0])
    assert len(ai_providers.sends) == 1
    assert (await ai_turns.usage(workspace.tenant_id))["ai_turn"] == 1


async def test_suspended_a_person_still_replies_on_instagram(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace, page, adapter, registry = await _paid_workspace(
        ai_turns, SubscriptionStatus.SUSPENDED
    )
    instagram, _trigger = await _instagram_writes(ai_turns, adapter, page)

    async with ai_turns.database.session() as session:
        sent = await MessagingService(
            session=session,
            settings=ai_turns.settings,
            tenant_id=workspace.tenant_id,
            channels=registry,
        ).send_text(
            conversation_id=instagram,
            body="Yes, across Cairo.",
            origin=MessageOrigin.HUMAN,
        )
        assert sent.direction is MessageDirection.OUTBOUND

    [(_recipient, content)] = adapter.log.sent
    assert isinstance(content, TextContent) and content.body == "Yes, across Cairo."


@pytest.mark.parametrize(
    "status", [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE], ids=str
)
async def test_a_served_subscription_answers_on_instagram(
    ai_turns: TurnRunner, ai_providers: FakeProviders, status: SubscriptionStatus
) -> None:
    """Active, and past due too: a failed payment is a conversation, not a cut-off."""
    workspace, page, adapter, registry = await _paid_workspace(ai_turns, status)

    instagram, trigger = await _instagram_writes(ai_turns, adapter, page)
    await _turn(ai_turns, workspace, registry, instagram, trigger)

    turn = await _turn_for(ai_turns, trigger)
    assert turn.outcome is not None and turn.outcome.value == "replied"
    [(_recipient, content)] = adapter.log.sent
    assert isinstance(content, TextContent) and DEFAULT_REPLY in content.body


async def test_paying_restores_the_channel_with_nothing_to_re_enable(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace, page, adapter, registry = await _paid_workspace(
        ai_turns, SubscriptionStatus.SUSPENDED
    )
    first, refused = await _instagram_writes(ai_turns, adapter, page)
    await _turn(ai_turns, workspace, registry, first, refused)
    assert (await _turn_for(ai_turns, refused)).outcome is not None

    await ai_turns.execute(
        update(Subscription)
        .where(Subscription.tenant_id == workspace.tenant_id)
        .values(status=SubscriptionStatus.ACTIVE)
    )
    second, answered = await _instagram_writes(ai_turns, adapter, page)
    await _turn(ai_turns, workspace, registry, second, answered)

    turn = await _turn_for(ai_turns, answered)
    assert turn.outcome is not None and turn.outcome.value == "replied"
    assert len(adapter.log.sent) == 1
    async with ai_turns.database.session() as session:
        status = await session.scalar(
            select(ChannelConnection.status).where(ChannelConnection.id == page.id)
        )
    assert status is ConnectionStatus.ACTIVE, "nothing was disabled, so nothing to re-enable"


async def test_suspended_a_follow_up_on_instagram_is_skipped(
    db_session: AsyncSession, settings: Settings
) -> None:
    adapter = SyntheticAdapter(Channel.INSTAGRAM)
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.INSTAGRAM: cast(ChannelAdapter, adapter),
        },
    )
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Plan {tag}", slug=f"plan-{tag}")
    db_session.add(tenant)
    await db_session.flush()
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"ig-{tag}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=30),
    )
    db_session.add(connection)
    await db_session.flush()
    await ChannelIngestionService(session=db_session, adapter=cast(ChannelAdapter, adapter)).ingest(
        adapter.parse(
            synthetic_payload(
                connection.external_account_id,
                {
                    "type": "message",
                    "id": f"m.{tag}",
                    "from": "igsid-plan",
                    "at": int(datetime.now(UTC).timestamp()),
                    "text": "Let me think about it",
                },
            )
        )
    )
    conversation = await db_session.scalar(
        select(Conversation).where(Conversation.account_id == connection.id)
    )
    assert conversation is not None
    paid = await plan_with_channels(db_session, channels=[Channel.WHATSAPP, Channel.INSTAGRAM])
    subscription = await subscribe(db_session, tenant.id, paid)
    service = FollowUpService(
        session=db_session,
        tenant_id=tenant.id,
        settings=settings,
        messaging=MessagingService(
            session=db_session, settings=settings, tenant_id=tenant.id, channels=registry
        ),
        channels=registry,
    )

    def due() -> FollowUp:
        row = FollowUp(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            status=FollowUpStatus.PENDING,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
            body="Still thinking?",
            created_by_kind=ActorKind.AGENT,
        )
        db_session.add(row)
        return row

    # Served: the nudge goes out.
    served = due()
    await db_session.flush()
    assert (await service.dispatch(served)).follow_up.status is FollowUpStatus.SENT
    assert len(adapter.log.sent) == 1

    # Suspended: the default plan allows WhatsApp alone, so the nudge is skipped.
    subscription.status = SubscriptionStatus.SUSPENDED
    await db_session.flush()
    suspended = due()
    await db_session.flush()
    result = await service.dispatch(suspended)

    assert result.follow_up.status is FollowUpStatus.SKIPPED
    assert result.follow_up.last_error == CHANNEL_NOT_IN_PLAN_DETAIL
    assert result.follow_up.claim_token is None
    assert len(adapter.log.sent) == 1, "nothing more was sent"


@pytest.mark.parametrize(
    ("status", "sent"),
    [(SubscriptionStatus.SUSPENDED, 0), (SubscriptionStatus.ACTIVE, 2)],
    ids=["suspended", "active"],
)
async def test_a_campaign_on_a_channel_the_plan_in_force_excludes_is_skipped(
    db_session: AsyncSession, settings: Settings, status: SubscriptionStatus, sent: int
) -> None:
    """Through the real campaign worker and service; campaigns send on WhatsApp alone.

    The paid plan includes WhatsApp; the plan a workspace not served falls back
    to - the deployment's default - is made to exclude it here, the one way a
    campaign's channel can fall outside the plan in force today. Served, both
    copies go out; suspended, both are skipped `channel_not_in_plan`, terminal.
    """
    tag = uuid.uuid4().hex[:8]
    campaign = await _sending_campaign(db_session, slug=f"plan-{tag}", recipients=2)
    paid = await plan_with_channels(db_session, channels=[Channel.WHATSAPP, Channel.INSTAGRAM])
    fallback = await plan_with_channels(db_session, channels=[Channel.INSTAGRAM])
    await subscribe(db_session, campaign.tenant_id, paid, status=status)
    sends: list[StubMessaging] = []

    def factory(session: AsyncSession, tenant_id: uuid.UUID) -> MessagingService:
        stub = StubMessaging(session, tenant_id=tenant_id)
        sends.append(stub)
        return as_messaging(stub)

    worker = CampaignWorker(
        database=as_database(SessionHandle(db_session)),
        settings=settings.model_copy(update={"default_plan_code": fallback.code}),
        messaging_factory=factory,
    )
    assert await worker.run_once() == 1
    await db_session.flush()

    recipients = list(
        await db_session.scalars(
            select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id)
        )
    )
    assert sum(stub.sends for stub in sends) == sent
    if sent:
        assert {row.status for row in recipients} == {RecipientStatus.SENT}
    else:
        assert {row.status for row in recipients} == {RecipientStatus.SKIPPED}
        assert {row.last_error for row in recipients} == {CHANNEL_NOT_IN_PLAN_DETAIL}
