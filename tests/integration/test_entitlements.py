"""Entitlements against real rows.

Two kinds of limit, two kinds of query, and the cases that decide whether the
answers can be trusted: a workspace with no subscription at all, a plan with no
limit for a key, a period that has rolled over, and another workspace's rows
never counting toward this one's allowance.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PlanLimitExceededError
from app.db.models.agent import Agent, AgentStatus
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.services.entitlement_service import EntitlementService
from app.services.usage_service import UsageRecorder

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)
PERIOD_START = NOW - timedelta(days=5)
PERIOD_END = NOW + timedelta(days=25)


async def _tenant(session: AsyncSession, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _plan(
    session: AsyncSession,
    limits: Mapping[LimitKey, int] | None = None,
    *,
    code: str = "test",
) -> Plan:
    plan = Plan(
        code=code,
        name=code.title(),
        price=Decimal("10.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        limits={key.value: value for key, value in (limits or {}).items()},
    )
    session.add(plan)
    await session.flush()
    return plan


async def _subscribe(
    session: AsyncSession,
    tenant: Tenant,
    plan: Plan,
    *,
    start: datetime = PERIOD_START,
    end: datetime = PERIOD_END,
) -> Subscription:
    subscription = SubscriptionRepository(session, tenant_id=tenant.id).create(
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=start,
        current_period_end=end,
    )
    await session.flush()
    return subscription


def _service(
    session: AsyncSession, tenant: Tenant, *, default_plan_code: str | None = None
) -> EntitlementService:
    return EntitlementService(
        session,
        tenant_id=tenant.id,
        default_plan_code=default_plan_code,
    )


async def _agent(session: AsyncSession, tenant: Tenant, name: str) -> Agent:
    agent = Agent(
        tenant_id=tenant.id,
        name=name,
        status=AgentStatus.ACTIVE,
        model="gpt-4.1-mini",
        system_prompt="Be helpful.",
    )
    session.add(agent)
    await session.flush()
    return agent


# ------------------------------------------------------------ resource limits


async def test_a_resource_limit_counts_what_exists_now(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.AGENTS: 2})
    await _subscribe(db_session, tenant, plan)
    await _agent(db_session, tenant, "Sales")

    entitlement = await _service(db_session, tenant).check(LimitKey.AGENTS)
    assert entitlement.limit == 2
    assert entitlement.used == 1
    assert entitlement.remaining == 1
    assert entitlement.allowed is True


async def test_the_last_slot_is_allowed_and_the_next_is_not(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.AGENTS: 2})
    await _subscribe(db_session, tenant, plan)
    await _agent(db_session, tenant, "Sales")
    service = _service(db_session, tenant)

    assert (await service.check(LimitKey.AGENTS, additional=1)).allowed is True

    await _agent(db_session, tenant, "Support")
    # A fresh service: the resolved plan is cached per instance, and so is
    # nothing else - the count is read again.
    assert (await _service(db_session, tenant).check(LimitKey.AGENTS)).allowed is False


async def test_refusing_says_what_to_do_about_it(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.AGENTS: 1}, code="tiny")
    await _subscribe(db_session, tenant, plan)
    await _agent(db_session, tenant, "Sales")

    with pytest.raises(PlanLimitExceededError) as raised:
        await _service(db_session, tenant).require(LimitKey.AGENTS)

    assert raised.value.status_code == 402
    assert "Upgrade" in str(raised.value)


async def test_a_disabled_number_frees_its_slot(db_session: AsyncSession) -> None:
    """It is connected to nothing, and charging for it would charge for nothing."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.WHATSAPP_NUMBERS: 1})
    await _subscribe(db_session, tenant, plan)
    db_session.add(
        WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
            waba_id="555000111",
            display_phone_number="+201000000000",
            status=WhatsAppAccountStatus.DISABLED,
        )
    )
    await db_session.flush()

    assert (await _service(db_session, tenant).check(LimitKey.WHATSAPP_NUMBERS)).used == 0


async def test_a_draft_agent_still_occupies_a_slot(db_session: AsyncSession) -> None:
    """The asymmetry with numbers is deliberate: a limit that ignored drafts
    would be satisfied by twenty agents somebody toggles."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.AGENTS: 1})
    await _subscribe(db_session, tenant, plan)
    agent = await _agent(db_session, tenant, "Draft")
    agent.status = AgentStatus.DRAFT
    await db_session.flush()

    assert (await _service(db_session, tenant).check(LimitKey.AGENTS)).used == 1


# -------------------------------------------------------------- period limits


async def test_a_period_limit_counts_usage_inside_the_period(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.PERIOD_AI_REQUESTS: 100})
    await _subscribe(db_session, tenant, plan)

    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(UsageEventType.AI_REQUEST, quantity=30, occurred_at=NOW)
    # Before this period began: last month's spending is not this month's.
    recorder.record(
        UsageEventType.AI_REQUEST,
        quantity=999,
        occurred_at=PERIOD_START - timedelta(days=1),
    )
    await db_session.flush()

    entitlement = await _service(db_session, tenant).check(LimitKey.PERIOD_AI_REQUESTS)
    assert entitlement.used == 30
    assert entitlement.remaining == 70


async def test_messages_are_counted_in_both_directions(db_session: AsyncSession) -> None:
    """A conversation is two-sided, and a workspace that received a hundred
    thousand messages consumed something whoever typed them."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.PERIOD_MESSAGES: 10})
    await _subscribe(db_session, tenant, plan)

    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(UsageEventType.WHATSAPP_MESSAGE_SENT, quantity=3, occurred_at=NOW)
    recorder.record(UsageEventType.WHATSAPP_MESSAGE_RECEIVED, quantity=4, occurred_at=NOW)
    await db_session.flush()

    assert (await _service(db_session, tenant).check(LimitKey.PERIOD_MESSAGES)).used == 7


async def test_a_plan_that_allows_no_campaigns_refuses_the_first_one(
    db_session: AsyncSession,
) -> None:
    """Zero has to be expressible: it is what Starter allows."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.PERIOD_CAMPAIGN_MESSAGES: 0})
    await _subscribe(db_session, tenant, plan)

    entitlement = await _service(db_session, tenant).check(LimitKey.PERIOD_CAMPAIGN_MESSAGES)
    assert entitlement.limit == 0
    assert entitlement.allowed is False


# --------------------------------------------------------------- fallbacks


async def test_a_workspace_with_no_subscription_falls_back_to_the_default_plan(
    db_session: AsyncSession,
) -> None:
    """Every workspace predating billing has none, and a product that stopped
    working for them would be worse than any limit."""
    tenant = await _tenant(db_session)
    await _plan(db_session, {LimitKey.AGENTS: 1}, code="starter")

    entitlement = await _service(db_session, tenant, default_plan_code="starter").check(
        LimitKey.AGENTS
    )
    assert entitlement.plan_code == "starter"
    assert entitlement.limit == 1


async def test_no_plan_at_all_leaves_limits_unenforced(db_session: AsyncSession) -> None:
    """Taking a working deployment offline over a missing catalogue row is not
    a failure mode a limit check should have."""
    tenant = await _tenant(db_session)

    entitlement = await _service(db_session, tenant, default_plan_code="missing").check(
        LimitKey.AGENTS
    )
    assert entitlement.allowed is True
    assert entitlement.is_unlimited is True


async def test_an_unlimited_plan_reports_no_remaining_number(db_session: AsyncSession) -> None:
    """None rather than a large number: "999999 left" is a falsehood."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, code="enterprise")
    await _subscribe(db_session, tenant, plan)

    entitlement = await _service(db_session, tenant).check(LimitKey.AGENTS)
    assert entitlement.is_unlimited is True
    assert entitlement.remaining is None
    assert entitlement.allowed is True


async def test_a_workspace_without_a_subscription_counts_the_calendar_month(
    db_session: AsyncSession,
) -> None:
    """Otherwise the sum runs from the beginning of time and refuses everybody."""
    tenant = await _tenant(db_session)
    await _plan(db_session, {LimitKey.PERIOD_MESSAGES: 100}, code="starter")

    recorder = UsageRecorder(db_session, tenant_id=tenant.id)
    recorder.record(UsageEventType.WHATSAPP_MESSAGE_SENT, quantity=5, occurred_at=NOW)
    recorder.record(
        UsageEventType.WHATSAPP_MESSAGE_SENT,
        quantity=500,
        occurred_at=NOW.replace(day=1) - timedelta(days=1),
    )
    await db_session.flush()

    entitlement = await _service(db_session, tenant, default_plan_code="starter").check(
        LimitKey.PERIOD_MESSAGES
    )
    assert entitlement.used == 5


# --------------------------------------------------------------- isolation


async def test_another_workspaces_usage_is_not_charged_to_this_one(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    plan = await _plan(db_session, {LimitKey.PERIOD_AI_REQUESTS: 10})
    await _subscribe(db_session, acme, plan)
    await _subscribe(db_session, rival, plan)

    UsageRecorder(db_session, tenant_id=rival.id).record(
        UsageEventType.AI_REQUEST,
        quantity=50,
        occurred_at=NOW,
    )
    await db_session.flush()

    entitlement = await _service(db_session, acme).check(LimitKey.PERIOD_AI_REQUESTS)
    assert entitlement.used == 0
    assert entitlement.allowed is True


async def test_another_workspaces_agents_do_not_fill_this_ones_slots(
    db_session: AsyncSession,
) -> None:
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    plan = await _plan(db_session, {LimitKey.AGENTS: 1})
    await _subscribe(db_session, acme, plan)
    await _agent(db_session, rival, "Theirs")

    assert (await _service(db_session, acme).check(LimitKey.AGENTS)).used == 0


# ---------------------------------------------------------------- catalogue


async def test_the_seeded_catalogue_is_not_visible_to_this_suite(db_session: AsyncSession) -> None:
    """The schema here is built from the models, so migration 0016's seed rows
    are absent. Recorded so the next reader does not hunt for them."""
    assert await PlanRepository(db_session).list_plans() == []


async def test_a_private_plan_is_not_listed(db_session: AsyncSession) -> None:
    await _plan(db_session, code="public")
    bespoke = await _plan(db_session, code="bespoke")
    bespoke.is_public = False
    await db_session.flush()

    codes = [plan.code for plan in await PlanRepository(db_session).list_plans()]
    assert codes == ["public"]


async def test_a_retired_plan_is_kept_but_not_offered(db_session: AsyncSession) -> None:
    """Subscriptions still point at it, and their history has to keep meaning
    what it meant."""
    retired = await _plan(db_session, code="old")
    retired.is_active = False
    await db_session.flush()

    repository = PlanRepository(db_session)
    assert await repository.list_plans() == []
    assert await repository.get_by_code("old") is not None


async def test_a_snapshot_reports_every_limit(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.AGENTS: 3})
    await _subscribe(db_session, tenant, plan)

    snapshot = await _service(db_session, tenant).snapshot()
    assert {item.key for item in snapshot} == set(LimitKey)
    # Nothing is refused by asking: a snapshot adds nothing.
    assert all(item.allowed for item in snapshot)


async def test_a_released_number_frees_its_slot(db_session: AsyncSession) -> None:
    """Giving a number back must not cost a slot forever.

    A released row survives only because a customer's conversations hang off it
    (ADR-037). Its status is `released`, not `disabled`, so the disabled check
    above does not cover it - and a version of this that counted released rows
    would make handing a number back a permanent charge.
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.WHATSAPP_NUMBERS: 1})
    await _subscribe(db_session, tenant, plan)
    db_session.add(
        WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
            waba_id="555000111",
            display_phone_number="+201000000000",
            status=WhatsAppAccountStatus.RELEASED,
            released_at=datetime(2026, 8, 23, tzinfo=UTC),
        )
    )
    await db_session.flush()

    assert (await _service(db_session, tenant).check(LimitKey.WHATSAPP_NUMBERS)).used == 0


async def test_a_revoked_member_frees_their_seat(db_session: AsyncSession) -> None:
    """Otherwise removal is a one-way door.

    A workspace on a two-seat plan that removes a colleague could never hire a
    replacement: the seat would be held by somebody who cannot sign in, and the
    only fix would be paying for capacity nobody is using (ADR-038).
    """
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, {LimitKey.TEAM_MEMBERS: 2})
    await _subscribe(db_session, tenant, plan)
    present = User(email=f"present-{uuid.uuid4().hex[:8]}@example.test", is_active=True)
    gone = User(email=f"gone-{uuid.uuid4().hex[:8]}@example.test", is_active=True)
    db_session.add_all([present, gone])
    await db_session.flush()
    db_session.add_all(
        [
            Membership(
                tenant_id=tenant.id,
                user_id=present.id,
                role=TenantRole.TENANT_OWNER,
                status=MembershipStatus.ACTIVE,
            ),
            Membership(
                tenant_id=tenant.id,
                user_id=gone.id,
                role=TenantRole.MEMBER,
                status=MembershipStatus.REVOKED,
                revoked_at=datetime(2026, 8, 23, tzinfo=UTC),
            ),
        ]
    )
    await db_session.flush()

    entitlement = await _service(db_session, tenant).check(LimitKey.TEAM_MEMBERS)

    # One seat occupied, not two: the removed person does not hold one, so the
    # second seat is free for their replacement.
    assert entitlement.used == 1
    assert entitlement.allowed is True
    assert entitlement.remaining == 1


# ------------------------------------------------- entitlements follow status


@pytest.mark.parametrize(
    "status",
    [SubscriptionStatus.CANCELLED, SubscriptionStatus.EXPIRED],
)
async def test_a_subscription_that_has_ended_stops_granting_its_plan(
    db_session: AsyncSession,
    status: SubscriptionStatus,
) -> None:
    """Cancelling was a way to keep the entitlements and stop the invoices.

    `SERVING_STATUSES` and `Subscription.is_serving` both existed from the
    start and neither was read, so entitlement resolution loaded the plan off
    whatever subscription row was there whatever state it was in. A workspace
    could subscribe to the most expensive plan, cancel it, and keep its limits
    for as long as nobody deleted the row.
    """
    tenant = await _tenant(db_session)
    await _plan(db_session, {LimitKey.AGENTS: 1}, code="starter")
    expensive = await _plan(db_session, {LimitKey.AGENTS: 500}, code="enterprise")
    subscription = await _subscribe(db_session, tenant, expensive)

    service = _service(db_session, tenant, default_plan_code="starter")
    assert (await service.check(LimitKey.AGENTS)).limit == 500

    subscription.status = status
    await db_session.flush()

    # A fresh service: the resolution is cached per instance, as a request's is.
    after = _service(db_session, tenant, default_plan_code="starter")
    assert (await after.check(LimitKey.AGENTS)).limit == 1


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.PAST_DUE,
    ],
)
async def test_a_serving_subscription_still_grants_its_plan(
    db_session: AsyncSession,
    status: SubscriptionStatus,
) -> None:
    """`PAST_DUE` is in the serving set deliberately (see the model).

    A failed payment is a conversation to have with a customer, not a decision
    to cut them off mid-sentence, and this pins that so the check added above
    cannot quietly become a lockout on the first declined card.
    """
    tenant = await _tenant(db_session)
    await _plan(db_session, {LimitKey.AGENTS: 1}, code="starter")
    paid = await _plan(db_session, {LimitKey.AGENTS: 50}, code="pro")
    subscription = await _subscribe(db_session, tenant, paid)
    subscription.status = status
    await db_session.flush()

    service = _service(db_session, tenant, default_plan_code="starter")
    assert (await service.check(LimitKey.AGENTS)).limit == 50


async def test_an_ended_subscription_falls_back_rather_than_locking_out(
    db_session: AsyncSession,
) -> None:
    """Dropping to the free tier, not to nothing.

    With no default plan configured there is no plan at all and limits go
    unenforced, which is the existing behaviour for a workspace that never
    subscribed; with one, a cancelled workspace keeps working at its limits.
    Losing access outright would make cancelling unrecoverable.
    """
    tenant = await _tenant(db_session)
    await _plan(db_session, {LimitKey.AGENTS: 1}, code="starter")
    plan = await _plan(db_session, {LimitKey.AGENTS: 50}, code="pro")
    subscription = await _subscribe(db_session, tenant, plan)
    subscription.status = SubscriptionStatus.CANCELLED
    await db_session.flush()

    entitlement = await _service(db_session, tenant, default_plan_code="starter").check(
        LimitKey.AGENTS,
    )
    assert entitlement.limit == 1
    assert entitlement.allowed is True
