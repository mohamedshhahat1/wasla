"""Each entitlement invariant counts a violation when one exists (ADR-131, E01-E15).

`python -m scripts.omnichannel_invariants verify` runs the entitlement ledger
beside the omnichannel invariants. An invariant that cannot fail proves
nothing, so each one is shown non-vacuous here: a violation no application path
writes is injected by hand inside the test's rolled-back transaction, and
exactly that invariant counts one more.

Five of them restate something a key, a constraint or a trigger already
refuses - E05, E11, E13, E15 and part of E12. For those the guard is lifted
first, inside the same rolled-back transaction, which is the only way to show
the query would see the row the guard keeps out. The plan-version immutability
trigger is never touched: E15 lifts only the insert check on new versions'
channel types.

And the ledger reads zero where the flows leave the data they should: a
workspace over capacity with its reduction open, one whose subscription is not
served, and one whose channel slot has just stopped counting and whose boundary
the next billing pass judges.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.db.models.agent_turn import (
    AgentTurn,
    AgentTurnState,
    AITurnChargeState,
    AITurnReleaseReason,
    TurnOutcome,
)
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import BillingInterval, Plan, PlanVersion, SubscriptionStatus
from app.db.models.campaign import CampaignRecipient, RecipientStatus
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionDisabledReason,
    ConnectionStatus,
)
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.db.models.consent import ContactChannelConsent
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDeliveryState,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupSource, TopupStatus
from app.db.models.usage import UsageEvent, UsageEventType, UsageUnit
from app.services.audit_service import AuditTrail
from app.services.channel_ingestion_service import ChannelIngestionService
from scripts.omnichannel_invariants import (
    DEFAULT_HOLD_TTL_SECONDS,
    SWEEP_SECONDS,
    entitlement_violations,
)
from tests.channel_fakes import SyntheticAdapter, synthetic_payload
from tests.channel_plans import plan_with_channels, subscribe
from tests.consent import seed_opt_out
from tests.integration.test_campaigns import _account, _campaign, _customer, _template

pytestmark = pytest.mark.integration


async def _counts(session: AsyncSession) -> dict[str, int]:
    await session.flush()
    return await entitlement_violations(await session.connection(), read_only=False)


async def _added(session: AsyncSession, before: dict[str, int]) -> dict[str, int]:
    after = await _counts(session)
    return {name: after[name] - before[name] for name in after if after[name] != before[name]}


async def _tenant(session: AsyncSession) -> Tenant:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Ledger {tag}", slug=f"ledger-{tag}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _workspace(
    session: AsyncSession,
    *,
    channels: tuple[Channel, ...],
    capacity: int,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
) -> Tenant:
    """A workspace on a plan of its own allowing `channels` and `capacity` connections."""
    tenant = await _tenant(session)
    plan = await plan_with_channels(
        session, channels=channels, limits={"channel_connections": capacity}
    )
    await subscribe(session, tenant.id, plan, status=status)
    return tenant


async def _connection(
    session: AsyncSession,
    tenant: Tenant,
    channel: Channel,
    *,
    status: ConnectionStatus = ConnectionStatus.ACTIVE,
) -> ChannelConnection:
    """A connection written straight to the table - past the guard, as no path may."""
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=channel,
        external_account_id=f"{channel.value}-{uuid.uuid4().hex[:10]}",
        status=status,
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(connection)
    await session.flush()
    return connection


async def _inbound(session: AsyncSession, tenant: Tenant, count: int) -> list[Message]:
    """`count` customer messages on an Instagram connection: real triggers for turns."""
    connection = await _connection(session, tenant, Channel.INSTAGRAM)
    adapter = SyntheticAdapter(Channel.INSTAGRAM)
    service = ChannelIngestionService(session=session, adapter=cast(ChannelAdapter, adapter))
    for index in range(count):
        await service.ingest(
            adapter.parse(
                synthetic_payload(
                    connection.external_account_id,
                    {
                        "type": "message",
                        "id": f"m.{uuid.uuid4().hex}",
                        "from": "igsid-ledger",
                        "at": 1790000000 + index,
                        "text": f"Hello {index}",
                    },
                )
            )
        )
    await session.flush()
    rows = await session.scalars(
        select(Message)
        .where(Message.tenant_id == tenant.id)
        .where(Message.direction == MessageDirection.INBOUND)
        .order_by(Message.created_at)
    )
    messages = list(rows)
    assert len(messages) == count
    return messages


def _turn(trigger: Message, **charge: object) -> AgentTurn:
    return AgentTurn(
        tenant_id=trigger.tenant_id,
        conversation_id=trigger.conversation_id,
        trigger_message_id=trigger.id,
        state=AgentTurnState.ENGAGED,
        **charge,
    )


def _charge(turn: AgentTurn) -> UsageEvent:
    return UsageEvent(
        tenant_id=turn.tenant_id,
        event_type=UsageEventType.AI_TURN,
        quantity=1,
        unit=UsageUnit.COUNT,
        occurred_at=datetime.now(UTC),
        agent_turn_id=turn.id,
        channel=Channel.INSTAGRAM,
    )


# ------------------------------------------------------------ capacity (E01, E02)


async def test_e01_a_workspace_over_its_capacity_with_nothing_to_explain_it(
    db_session: AsyncSession,
) -> None:
    tenant = await _workspace(db_session, channels=(Channel.INSTAGRAM,), capacity=1)
    await _connection(db_session, tenant, Channel.INSTAGRAM)
    before = await _counts(db_session)

    await _connection(db_session, tenant, Channel.INSTAGRAM)

    assert await _added(db_session, before) == {"e01_connections_over_capacity_unexplained": 1}


async def test_e01_and_e02_are_explained_by_an_open_reduction_a_lapsed_plan_and_the_sweep(
    db_session: AsyncSession,
) -> None:
    """What the flows leave over capacity on purpose counts nothing (ENT-14..16)."""
    before = await _counts(db_session)
    moment = datetime.now(UTC)

    # Over capacity, and of a type the plan dropped, with its reduction open.
    reducing = await _workspace(db_session, channels=(Channel.WHATSAPP,), capacity=1)
    for channel in (Channel.INSTAGRAM, Channel.INSTAGRAM, Channel.MESSENGER):
        await _connection(db_session, reducing, channel)
    db_session.add(
        ChannelCapacityReduction(
            tenant_id=reducing.id,
            cause=CapacityReductionCause.DOWNGRADE,
            status=CapacityReductionStatus.PENDING_SELECTION,
            target_general=1,
            target_typed={},
            target_allowed_types=["whatsapp"],
            effective_at=moment,
            grace_ends_at=moment + timedelta(days=7),
        )
    )
    # Suspended, over capacity and of a type the default plan lacks (ENT-16).
    lapsed = await _workspace(
        db_session,
        channels=(Channel.WHATSAPP,),
        capacity=1,
        status=SubscriptionStatus.SUSPENDED,
    )
    for channel in (Channel.INSTAGRAM, Channel.MESSENGER):
        await _connection(db_session, lapsed, channel)
    # A channel slot that stopped counting a minute ago, not yet swept.
    await _lapsed_slot(db_session, ended=moment - timedelta(minutes=1))

    assert await _added(db_session, before) == {}

    # One the sweep should have judged long ago is no longer explained.
    await _lapsed_slot(db_session, ended=moment - timedelta(seconds=SWEEP_SECONDS + 60))
    assert await _added(db_session, before) == {"e01_connections_over_capacity_unexplained": 1}


async def _lapsed_slot(session: AsyncSession, *, ended: datetime) -> Tenant:
    """A workspace holding two connections on one slot, its granted second slot ended at `ended`."""
    tenant = await _workspace(session, channels=(Channel.INSTAGRAM,), capacity=1)
    subscription_id = await session.scalar(
        text("SELECT id FROM subscriptions WHERE tenant_id = :t"), {"t": tenant.id}
    )
    session.add(
        TopupPurchase(
            tenant_id=tenant.id,
            subscription_id=subscription_id,
            source=TopupSource.PLATFORM_GRANT,
            product_name="Platform grant",
            entitlement_key=TopupEntitlement.CHANNEL_CONNECTIONS,
            quantity=1,
            unit_price=Decimal("0.00"),
            total_amount=Decimal("0.00"),
            currency="EGP",
            billing_period_start=ended - timedelta(days=30),
            billing_period_end=ended,
            expires_at=ended,
            status=TopupStatus.GRANTED,
            granted_at=ended - timedelta(days=29),
            reason="Ledger fixture.",
        )
    )
    for _ in range(2):
        await _connection(session, tenant, Channel.INSTAGRAM)
    return tenant


async def test_e02_an_active_connection_of_a_type_the_plan_does_not_allow(
    db_session: AsyncSession,
) -> None:
    tenant = await _workspace(db_session, channels=(Channel.WHATSAPP,), capacity=5)
    before = await _counts(db_session)

    await _connection(db_session, tenant, Channel.MESSENGER)

    assert await _added(db_session, before) == {
        "e02_connection_of_a_type_not_allowed_unexplained": 1
    }


# ------------------------------------------------------------ top-ups (E03, E04)


async def test_e03_a_typed_slot_sold_for_a_type_the_plan_did_not_allow(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    before = await _counts(db_session)

    AuditTrail(db_session, tenant_id=tenant.id).record(
        AuditAction.BILLING_TOPUP_CHECKOUT_CREATED,
        actor=None,
        actor_kind=AuditActorKind.SYSTEM,
        target_type="topup_purchase",
        target_id=uuid.uuid4(),
        meta={
            "entitlement_key": "channel_connections",
            "channel_type": "tiktok",
            "allowed_channel_types": ["instagram", "messenger", "whatsapp"],
            "eligible_plan_ids": [],
        },
    )

    assert await _added(db_session, before) == {"e03_typed_slot_for_a_type_not_allowed": 1}


async def test_e03_a_typed_slot_granted_for_a_type_the_plan_did_not_allow(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    before = await _counts(db_session)

    AuditTrail(db_session, tenant_id=tenant.id).record(
        AuditAction.BILLING_TOPUP_PLATFORM_GRANTED,
        actor=None,
        actor_kind=AuditActorKind.PLATFORM_STAFF,
        target_type="topup_purchase",
        target_id=uuid.uuid4(),
        meta={
            "entitlement_key": "channel_connections",
            "channel_type": "telegram",
            "allowed_channel_types": ["whatsapp"],
        },
    )

    assert await _added(db_session, before) == {"e03_typed_slot_for_a_type_not_allowed": 1}


async def test_e04_a_channel_topup_sold_to_a_plan_it_was_not_offered_to(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    before = await _counts(db_session)

    AuditTrail(db_session, tenant_id=tenant.id).record(
        AuditAction.BILLING_TOPUP_CHECKOUT_CREATED,
        actor=None,
        actor_kind=AuditActorKind.SYSTEM,
        target_type="topup_purchase",
        target_id=uuid.uuid4(),
        meta={
            "entitlement_key": "channel_connections",
            "channel_type": None,
            "allowed_channel_types": ["whatsapp"],
            "plan_id": str(uuid.uuid4()),
            "eligible_plan_ids": [str(uuid.uuid4())],
        },
    )

    assert await _added(db_session, before) == {"e04_channel_topup_sold_to_an_ineligible_plan": 1}


# ------------------------------------------------------------ AI turns (E05-E08)


async def test_e05_a_turn_charged_twice(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    before = await _counts(db_session)
    # The unique index refuses the second charge; lifted, inside this
    # rolled-back transaction only, to show the ledger sees what it keeps out.
    await db_session.execute(text("DROP INDEX uq_usage_events_tenant_id_agent_turn_id"))
    turn_id = uuid.uuid4()

    for _ in range(2):
        db_session.add(
            UsageEvent(
                tenant_id=tenant.id,
                event_type=UsageEventType.AI_TURN,
                quantity=1,
                unit=UsageUnit.COUNT,
                occurred_at=datetime.now(UTC),
                agent_turn_id=turn_id,
            )
        )

    assert await _added(db_session, before) == {"e05_turn_charged_twice": 1}


async def test_e06_a_charge_for_an_empty_answer(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    (trigger,) = await _inbound(db_session, tenant, 1)
    moment = datetime.now(UTC)
    turn = _turn(
        trigger,
        charge_state=AITurnChargeState.RELEASED,
        held_at=moment - timedelta(seconds=5),
        released_at=moment,
        charge_release_reason=AITurnReleaseReason.NOT_CHARGEABLE,
    )
    turn.state = AgentTurnState.COMPLETED
    turn.outcome = TurnOutcome.EMPTY_RESPONSE
    db_session.add(turn)
    await db_session.flush()
    before = await _counts(db_session)

    db_session.add(_charge(turn))

    assert await _added(db_session, before) == {"e06_charge_for_a_turn_not_chargeable": 1}


async def test_e06_a_charge_for_an_escalation_its_row_calls_charged(
    db_session: AsyncSession,
) -> None:
    """The outcome alone convicts: an escalation is never a generation (ENT-02)."""
    tenant = await _tenant(db_session)
    (trigger,) = await _inbound(db_session, tenant, 1)
    moment = datetime.now(UTC)
    turn = _turn(
        trigger,
        charge_state=AITurnChargeState.CHARGED,
        held_at=moment - timedelta(seconds=5),
        charged_at=moment,
    )
    db_session.add(turn)
    await db_session.flush()
    db_session.add(_charge(turn))
    await db_session.flush()
    before = await _counts(db_session)

    turn.state = AgentTurnState.COMPLETED
    turn.outcome = TurnOutcome.ESCALATED

    assert await _added(db_session, before) == {"e06_charge_for_a_turn_not_chargeable": 1}


async def test_e07_a_hold_outliving_its_ttl_and_the_sweep(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    first, second = await _inbound(db_session, tenant, 2)
    moment = datetime.now(UTC)
    # Inside the TTL and the sweep after it: not yet anybody's failure.
    db_session.add(_turn(first, charge_state=AITurnChargeState.HELD, held_at=moment))
    await db_session.flush()
    before = await _counts(db_session)

    outlived = moment - timedelta(seconds=DEFAULT_HOLD_TTL_SECONDS + SWEEP_SECONDS + 60)
    db_session.add(_turn(second, charge_state=AITurnChargeState.HELD, held_at=outlived))

    assert await _added(db_session, before) == {"e07_hold_outliving_its_ttl_and_the_sweep": 1}


async def test_e08_a_turn_charged_without_its_charge(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    (trigger,) = await _inbound(db_session, tenant, 1)
    moment = datetime.now(UTC)
    before = await _counts(db_session)

    db_session.add(
        _turn(
            trigger,
            charge_state=AITurnChargeState.CHARGED,
            held_at=moment - timedelta(seconds=5),
            charged_at=moment,
        )
    )

    assert await _added(db_session, before) == {"e08_charged_turn_without_its_charge": 1}


# -------------------------------------------------------- reductions (E09-E11)


def _resolved(tenant: Tenant, disabled: list[uuid.UUID]) -> ChannelCapacityReduction:
    moment = datetime.now(UTC)
    return ChannelCapacityReduction(
        tenant_id=tenant.id,
        cause=CapacityReductionCause.DOWNGRADE,
        status=CapacityReductionStatus.RESOLVED_BY_OWNER,
        target_general=1,
        target_typed={},
        target_allowed_types=["instagram"],
        effective_at=moment - timedelta(days=1),
        grace_ends_at=moment + timedelta(days=6),
        resolved_at=moment,
        kept_connection_ids=[],
        disabled_connection_ids=disabled,
    )


async def test_e09_a_reduction_disable_without_its_audit(db_session: AsyncSession) -> None:
    # On a plan that allows the connection: a workspace with none reads the
    # deployment's default plan - WhatsApp alone on a migration-built schema - and
    # the setup itself would count under E02 before anything is injected.
    tenant = await _workspace(db_session, channels=(Channel.INSTAGRAM,), capacity=1)
    connection = await _connection(db_session, tenant, Channel.INSTAGRAM)
    before = await _counts(db_session)

    connection.status = ConnectionStatus.DISABLED
    connection.disabled_reason = ConnectionDisabledReason.CAPACITY_REDUCTION_AUTOMATIC
    connection.disabled_at = datetime.now(UTC)

    assert await _added(db_session, before) == {"e09_reduction_disable_without_its_audit": 1}


async def test_e10_a_connection_a_reduction_lists_but_released_instead(
    db_session: AsyncSession,
) -> None:
    tenant = await _workspace(db_session, channels=(Channel.MESSENGER,), capacity=1)
    connection = await _connection(db_session, tenant, Channel.MESSENGER)
    before = await _counts(db_session)

    connection.status = ConnectionStatus.RELEASED
    connection.released_at = datetime.now(UTC)
    db_session.add(_resolved(tenant, [connection.id]))

    assert await _added(db_session, before) == {"e10_reduction_released_or_deleted_a_connection": 1}


async def test_e10_a_connection_a_reduction_lists_that_no_longer_exists(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    before = await _counts(db_session)

    db_session.add(_resolved(tenant, [uuid.uuid4()]))

    assert await _added(db_session, before) == {"e10_reduction_released_or_deleted_a_connection": 1}


async def test_e11_two_open_reductions_for_one_workspace(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    before = await _counts(db_session)
    # The partial unique index refuses the second; lifted for this transaction.
    await db_session.execute(text("DROP INDEX uq_channel_capacity_reductions_open"))
    moment = datetime.now(UTC)

    for _ in range(2):
        db_session.add(
            ChannelCapacityReduction(
                tenant_id=tenant.id,
                cause=CapacityReductionCause.TOPUP_EXPIRED,
                status=CapacityReductionStatus.PENDING_SELECTION,
                target_general=1,
                target_typed={},
                target_allowed_types=["whatsapp"],
                effective_at=moment,
                grace_ends_at=moment + timedelta(days=7),
            )
        )

    assert await _added(db_session, before) == {"e11_more_than_one_open_reduction": 1}


# ---------------------------------------------------- retired key, consent (E12-E14)


async def test_e12_the_retired_key_on_terms_that_state_channel_types(
    db_session: AsyncSession,
) -> None:
    before = await _counts(db_session)

    db_session.add(
        Plan(
            code=f"retired-{uuid.uuid4().hex[:8]}",
            name="Retired key",
            price=Decimal("0.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={"whatsapp_numbers": 2},
            allowed_channel_types=["whatsapp"],
        )
    )

    assert await _added(db_session, before) == {"e12_retired_whatsapp_numbers_key_in_use": 1}


async def test_e13_an_opt_out_with_nobody_deciding_it(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant=tenant)
    contact = await _customer(db_session, tenant=tenant, account=account, wa_id="201000000913")
    before = await _counts(db_session)
    # A check constraint keeps an opt-out's source; lifted for this transaction.
    await db_session.execute(
        text(
            "ALTER TABLE contact_channel_consents"
            " DROP CONSTRAINT ck_contact_channel_consents_opt_out_has_source"
        )
    )

    db_session.add(
        ContactChannelConsent(
            tenant_id=tenant.id,
            contact_id=contact.id,
            channel=Channel.WHATSAPP,
            marketing_opt_out_at=datetime.now(UTC),
        )
    )

    assert await _added(db_session, before) == {"e13_opt_out_without_channel_or_source": 1}


async def test_e14_a_campaign_copy_sent_after_a_stop_on_its_channel(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)
    contact = await _customer(db_session, tenant=tenant, account=account, wa_id="201000000914")
    campaign = await _campaign(db_session, tenant=tenant, account=account, template=template)
    conversation = await db_session.scalar(
        select(Conversation).where(Conversation.contact_id == contact.id)
    )
    assert conversation is not None
    stopped = datetime.now(UTC) - timedelta(hours=1)
    # A STOP on Instagram says nothing about this WhatsApp campaign.
    await seed_opt_out(
        db_session,
        tenant_id=tenant.id,
        contact_id=contact.id,
        channel=Channel.INSTAGRAM,
        at=stopped,
    )
    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND,
        kind=MessageKind.TEMPLATE,
        status=MessageStatus.SENT,
        delivery_state=MessageDeliveryState.SENT,
        template_name=template.name,
        template_language=template.language,
        origin=MessageOrigin.CAMPAIGN,
    )
    db_session.add(message)
    await db_session.flush()
    db_session.add(
        CampaignRecipient(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            contact_id=contact.id,
            conversation_id=conversation.id,
            message_id=message.id,
            status=RecipientStatus.SENT,
            sent_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    before = await _counts(db_session)

    await seed_opt_out(
        db_session, tenant_id=tenant.id, contact_id=contact.id, channel=Channel.WHATSAPP, at=stopped
    )

    assert await _added(db_session, before) == {
        "e14_campaign_copy_sent_after_an_opt_out_on_its_channel": 1
    }


# ------------------------------------------------------------ channel types (E15)


async def test_e15_a_pinned_version_naming_a_label_outside_the_vocabulary(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    plan = await plan_with_channels(db_session, channels=(Channel.WHATSAPP,))
    before = await _counts(db_session)
    # The insert check on a new version's channel types refuses this; only it
    # is lifted, for this transaction. The immutability trigger stays.
    await db_session.execute(
        text("ALTER TABLE plan_versions DISABLE TRIGGER plan_versions_entitlement_terms")
    )
    version = PlanVersion(
        plan_id=plan.id,
        version=99,
        name="Unreadable",
        price=Decimal("0.00"),
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits={},
        allowed_channel_types=["whatsapp", "fax"],
        effective_at=datetime.now(UTC) - timedelta(days=1),
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(version)
    await db_session.flush()
    subscription = await subscribe(db_session, tenant.id, plan)

    subscription.plan_version_id = version.id

    assert await _added(db_session, before) == {
        "e15_pinned_version_with_unreadable_channel_types": 1
    }
