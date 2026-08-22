"""Subscriptions through the service and the API.

Two halves. The service is driven against real rows, because the interesting
claims are about state that persists - a trial that becomes a subscription, a
cancellation that has not happened yet, a plan change that restarts the period.
The routes are then checked for the line that matters most here: an
administrator may invite colleagues, and is still not somebody who can commit
the company to a subscription.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_entitlement_service,
    get_plan_repository,
    get_subscription_service,
)
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    SubscriptionStatus,
)
from app.services.entitlement_service import Entitlement
from app.services.subscription_service import SubscriptionService

pytestmark = pytest.mark.integration

PATH = "/api/v1/billing"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


async def _tenant(session, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _plan(session, *, code: str, trial_days: int = 0, **limits) -> Plan:
    plan = Plan(
        code=code,
        name=code.title(),
        price=Decimal("99.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        trial_days=trial_days,
        limits={key.value: value for key, value in limits.items()},
    )
    session.add(plan)
    await session.flush()
    return plan


# ------------------------------------------------------------------- service


async def test_a_plan_with_a_trial_starts_the_workspace_on_it(db_session):
    tenant = await _tenant(db_session)
    await _plan(db_session, code="pro", trial_days=14)

    subscription = await SubscriptionService(db_session, tenant_id=tenant.id).start(
        plan_code="pro",
        now=NOW,
    )

    assert subscription.status is SubscriptionStatus.TRIALING
    assert subscription.trial_ends_at == NOW + timedelta(days=14)
    assert subscription.current_period_end == subscription.trial_ends_at


async def test_a_plan_without_a_trial_starts_active_for_a_full_period(db_session):
    tenant = await _tenant(db_session)
    await _plan(db_session, code="pro")

    subscription = await SubscriptionService(db_session, tenant_id=tenant.id).start(
        plan_code="pro",
        now=NOW,
    )

    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.current_period_end == datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


async def test_a_workspace_cannot_hold_two_subscriptions(db_session):
    tenant = await _tenant(db_session)
    await _plan(db_session, code="pro")
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code="pro", now=NOW)

    with pytest.raises(ConflictError):
        await service.start(plan_code="pro", now=NOW)


async def test_a_plan_nobody_offers_is_refused(db_session):
    tenant = await _tenant(db_session)
    retired = await _plan(db_session, code="old")
    retired.is_active = False
    await db_session.flush()

    service = SubscriptionService(db_session, tenant_id=tenant.id)
    with pytest.raises(ValidationError):
        await service.start(plan_code="old", now=NOW)
    with pytest.raises(ValidationError):
        await service.start(plan_code="imaginary", now=NOW)


async def test_changing_plan_restarts_the_period_and_ends_the_trial(db_session):
    tenant = await _tenant(db_session)
    await _plan(db_session, code="starter", trial_days=14)
    await _plan(db_session, code="pro")
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code="starter", now=NOW)

    later = NOW + timedelta(days=3)
    subscription = await service.change_plan(plan_code="pro", now=later)

    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.trial_ends_at is None
    assert subscription.current_period_start == later
    assert subscription.current_period_end == datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


async def test_changing_to_the_plan_already_held_is_refused(db_session):
    tenant = await _tenant(db_session)
    await _plan(db_session, code="pro")
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code="pro", now=NOW)

    with pytest.raises(ConflictError):
        await service.change_plan(plan_code="pro", now=NOW)


async def test_cancelling_leaves_the_customer_the_period_they_paid_for(db_session):
    """Ending it the instant they click takes something they bought, and is
    what makes people afraid to click."""
    tenant = await _tenant(db_session)
    await _plan(db_session, code="pro")
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code="pro", now=NOW)

    subscription = await service.cancel(now=NOW)

    assert subscription.cancel_at_period_end is True
    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.is_serving is True
    assert subscription.ended_at is None


async def test_cancelling_immediately_ends_the_period_too(db_session):
    """Nothing should count against an allowance the workspace no longer has."""
    tenant = await _tenant(db_session)
    await _plan(db_session, code="pro")
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code="pro", now=NOW)

    subscription = await service.cancel(immediately=True, now=NOW)

    assert subscription.status is SubscriptionStatus.CANCELLED
    assert subscription.ended_at == NOW
    assert subscription.current_period_end == NOW
    assert subscription.is_serving is False


async def test_a_cancellation_can_be_taken_back_before_it_happens(db_session):
    tenant = await _tenant(db_session)
    await _plan(db_session, code="pro")
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code="pro", now=NOW)
    await service.cancel(now=NOW)

    subscription = await service.resume()

    assert subscription.cancel_at_period_end is False
    assert subscription.cancelled_at is None


async def test_choosing_a_new_plan_takes_back_a_pending_cancellation(db_session):
    """Somebody choosing a plan has plainly changed their mind about leaving."""
    tenant = await _tenant(db_session)
    await _plan(db_session, code="starter")
    await _plan(db_session, code="pro")
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code="starter", now=NOW)
    await service.cancel(now=NOW)

    subscription = await service.change_plan(plan_code="pro", now=NOW)

    assert subscription.cancel_at_period_end is False


async def test_an_ended_subscription_cannot_be_resumed_or_changed(db_session):
    tenant = await _tenant(db_session)
    await _plan(db_session, code="starter")
    await _plan(db_session, code="pro")
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code="starter", now=NOW)
    await service.cancel(immediately=True, now=NOW)

    with pytest.raises(ConflictError):
        await service.resume()
    with pytest.raises(ConflictError):
        await service.change_plan(plan_code="pro", now=NOW)


async def test_a_workspace_with_no_subscription_has_nothing_to_cancel(db_session):
    tenant = await _tenant(db_session)
    with pytest.raises(NotFoundError):
        await SubscriptionService(db_session, tenant_id=tenant.id).cancel()


async def test_a_new_plans_limits_apply_at_once(db_session):
    """The point of an upgrade: the allowance arrives when the customer pays."""
    from app.services.entitlement_service import EntitlementService

    tenant = await _tenant(db_session)
    await _plan(db_session, code="starter", **{LimitKey.AGENTS: 1})
    await _plan(db_session, code="pro", **{LimitKey.AGENTS: 5})
    service = SubscriptionService(db_session, tenant_id=tenant.id)
    await service.start(plan_code="starter", now=NOW)

    before = await EntitlementService(db_session, tenant_id=tenant.id).check(LimitKey.AGENTS)
    await service.change_plan(plan_code="pro", now=NOW)
    after = await EntitlementService(db_session, tenant_id=tenant.id).check(LimitKey.AGENTS)

    assert before.limit == 1
    assert after.limit == 5


async def test_one_workspace_cannot_reach_anothers_subscription(db_session):
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    await _plan(db_session, code="pro")
    await SubscriptionService(db_session, tenant_id=acme.id).start(plan_code="pro", now=NOW)

    assert await SubscriptionService(db_session, tenant_id=rival.id).get() is None
    with pytest.raises(NotFoundError):
        await SubscriptionService(db_session, tenant_id=rival.id).cancel()


# --------------------------------------------------------------------- routes


class StubSubscriptions:
    """Records the calls a route makes, without touching a database."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.changed: list[str] = []
        self.cancelled: list[bool] = []
        self.resumed = 0
        self.subscription = None
        self.plan = Plan(
            id=uuid.uuid4(),
            code="pro",
            name="Pro",
            price=Decimal("99.00"),
            currency="USD",
            interval=BillingInterval.MONTHLY,
            trial_days=14,
            limits={LimitKey.AGENTS.value: 5},
        )

    def _row(self, **overrides):
        from app.db.models.billing import Subscription

        values = {
            "id": uuid.uuid4(),
            "tenant_id": TENANT_ID,
            "plan_id": self.plan.id,
            "status": SubscriptionStatus.ACTIVE,
            "current_period_start": NOW,
            "current_period_end": NOW + timedelta(days=30),
            "cancel_at_period_end": False,
        }
        values.update(overrides)
        return Subscription(**values)

    async def get(self):
        return self.subscription

    async def plan_for(self, subscription):
        return self.plan

    async def start(self, *, plan_code, now=None, actor=None):
        self.started.append(plan_code)
        self.subscription = self._row(status=SubscriptionStatus.TRIALING)
        return self.subscription

    async def change_plan(self, *, plan_code, now=None, actor=None):
        self.changed.append(plan_code)
        self.subscription = self._row()
        return self.subscription

    async def cancel(self, *, immediately=False, now=None, actor=None):
        self.cancelled.append(immediately)
        self.subscription = self._row(cancel_at_period_end=not immediately)
        return self.subscription

    async def resume(self, *, actor=None):
        self.resumed += 1
        self.subscription = self._row()
        return self.subscription


class StubEntitlements:
    async def snapshot(self, keys=None):
        return [
            Entitlement(key=LimitKey.AGENTS, limit=5, used=2, allowed=True, plan_code="pro"),
            Entitlement(key=LimitKey.PERIOD_MESSAGES, limit=None, used=9, allowed=True),
        ]


class StubPlans:
    def __init__(self) -> None:
        self.plan = Plan(
            id=uuid.uuid4(),
            code="pro",
            name="Pro",
            price=Decimal("99.00"),
            currency="USD",
            interval=BillingInterval.MONTHLY,
            trial_days=14,
            limits={LimitKey.AGENTS.value: 5},
        )

    async def list_plans(self, *, public_only=True, active_only=True):
        return [self.plan]


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(id=uuid.uuid4(), user_id=USER_ID, tenant_id=TENANT_ID, role=role),
        tenant=Tenant(id=TENANT_ID, name="Acme", slug="acme", status=TenantStatus.ACTIVE),
    )


def _as(app, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(role)


@pytest.fixture
def subscriptions(app) -> StubSubscriptions:
    stub = StubSubscriptions()
    app.dependency_overrides[get_subscription_service] = lambda: stub
    app.dependency_overrides[get_entitlement_service] = lambda: StubEntitlements()
    app.dependency_overrides[get_plan_repository] = lambda: StubPlans()
    return stub


async def test_any_member_can_read_the_catalogue(client, app, subscriptions):
    _as(app, TenantRole.MEMBER)

    response = await client.get(f"{PATH}/plans")

    assert response.status_code == 200
    plan = response.json()[0]
    assert plan["code"] == "pro"
    # Every key is named, so a comparison table renders "unlimited" rather than
    # a blank cell it has to guess the meaning of.
    limits = {row["key"]: row["limit"] for row in plan["limits"]}
    assert limits["agents"] == 5
    assert limits["period_messages"] is None


async def test_a_member_sees_where_the_workspace_stands(client, app, subscriptions):
    _as(app, TenantRole.MEMBER)

    response = await client.get(f"{PATH}/entitlements")

    assert response.status_code == 200
    body = {row["key"]: row for row in response.json()}
    assert body["agents"]["remaining"] == 3
    # Unlimited reports null rather than a large number: "999999 left" is false.
    assert body["period_messages"]["remaining"] is None


async def test_a_workspace_without_a_subscription_still_reports_its_limits(
    client,
    app,
    subscriptions,
):
    """It is being held to the default plan, and an empty list would say the
    opposite."""
    _as(app, TenantRole.MEMBER)

    response = await client.get(f"{PATH}/subscription")

    assert response.status_code == 200
    body = response.json()
    assert body["subscription"] is None
    assert len(body["entitlements"]) == 2


async def test_an_owner_can_choose_a_plan(client, app, subscriptions):
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.post(f"{PATH}/subscription", json={"plan_code": "pro"})

    assert response.status_code == 201
    assert subscriptions.started == ["pro"]
    assert response.json()["status"] == "trialing"


async def test_an_administrator_cannot_commit_the_company_to_a_subscription(
    client,
    app,
    subscriptions,
):
    """Inviting colleagues and signing a contract are different authorities."""
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/subscription", json={"plan_code": "pro"})

    assert response.status_code == 403
    assert subscriptions.started == []


async def test_a_member_cannot_cancel(client, app, subscriptions):
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/subscription/cancel", json={})

    assert response.status_code == 403
    assert subscriptions.cancelled == []


async def test_cancelling_defaults_to_the_end_of_the_period(client, app, subscriptions):
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.post(f"{PATH}/subscription/cancel", json={})

    assert response.status_code == 200
    assert subscriptions.cancelled == [False]
    assert response.json()["cancel_at_period_end"] is True


async def test_an_owner_can_give_up_the_rest_of_the_period(client, app, subscriptions):
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.post(f"{PATH}/subscription/cancel", json={"immediately": True})

    assert response.status_code == 200
    assert subscriptions.cancelled == [True]


async def test_a_plan_change_is_its_own_route(client, app, subscriptions):
    """Not a PATCH on a status field: that route lets a customer end their own
    trial and start a free forever."""
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.post(f"{PATH}/subscription/plan", json={"plan_code": "business"})

    assert response.status_code == 200
    assert subscriptions.changed == ["business"]


async def test_a_resume_is_a_route_of_its_own(client, app, subscriptions):
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.post(f"{PATH}/subscription/resume")

    assert response.status_code == 200
    assert subscriptions.resumed == 1


async def test_an_unknown_field_is_rejected_rather_than_ignored(client, app, subscriptions):
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.post(
        f"{PATH}/subscription",
        json={"plan_code": "pro", "trial_days": 3650},
    )

    assert response.status_code == 422
    assert subscriptions.started == []
