"""The sweep that advances a subscription when its period ends, and the
subscription a new workspace is given.

Two things only a real database can show. The first is the query: which rows a
sweep picks up and which it leaves alone, including the ones whose period ended
long ago but which nobody should touch again. The second is registration - a
workspace is created, a membership is created, and a subscription with them, in
one transaction that either all happens or none of it does.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.core.token_store import RefreshTokenStore
from app.db.models.billing import (
    BillingInterval,
    Plan,
    SubscriptionStatus,
)
from app.db.models.tenant import Tenant
from app.repositories.billing_repository import (
    PlatformSubscriptionRepository,
    SubscriptionRepository,
)
from app.services.auth_service import AuthService
from app.services.subscription_service import SubscriptionService
from app.workers.billing_worker import BillingWorker

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
ENDED = NOW - timedelta(hours=1)


class SessionHandle:
    """Hands the worker the test's own session, so its writes roll back."""

    def __init__(self, session) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self):
        yield self._session


class FakeTokenStore:
    """Registration issues a refresh token; nothing here reads it back."""

    async def remember(self, *args: object, **kwargs: object) -> None:
        return None

    async def revoke(self, *args: object, **kwargs: object) -> None:
        return None

    async def is_revoked(self, *args: object, **kwargs: object) -> bool:
        return False


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "jwt_secret": "a-test-secret-that-is-long-enough-32",
    }
    values.update(overrides)
    return Settings(**values)


async def _tenant(session, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _plan(session, *, code: str = "pro", trial_days: int = 0) -> Plan:
    plan = Plan(
        code=code,
        name=code.title(),
        price=Decimal("99.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
        trial_days=trial_days,
        limits={},
    )
    session.add(plan)
    await session.flush()
    return plan


async def _subscription(session, tenant, plan, *, status, end=ENDED, cancel=False):
    subscription = SubscriptionRepository(session, tenant_id=tenant.id).create(
        plan_id=plan.id,
        status=status,
        current_period_start=end - timedelta(days=30),
        current_period_end=end,
    )
    subscription.cancel_at_period_end = cancel
    await session.flush()
    return subscription


def _worker(db_session) -> BillingWorker:
    return BillingWorker(database=SessionHandle(db_session), settings=_settings())


# --------------------------------------------------------------- the sweep


async def test_a_finished_trial_expires(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session, trial_days=14)
    subscription = await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.TRIALING,
    )

    handled = await _worker(db_session).run_once(now=NOW)

    assert handled == 1
    assert subscription.status is SubscriptionStatus.EXPIRED
    assert subscription.is_serving is False


async def test_an_active_subscription_opens_its_next_period(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _subscription(db_session, tenant, plan, status=SubscriptionStatus.ACTIVE)

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.ACTIVE
    # The new period starts where the old one ended, so a sweep that runs late
    # does not silently shorten the customer's month.
    assert subscription.current_period_start == ENDED
    assert subscription.current_period_end == ENDED + timedelta(days=31)


async def test_a_pending_cancellation_takes_effect(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.ACTIVE,
        cancel=True,
    )

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.CANCELLED
    assert subscription.ended_at == NOW


async def test_a_period_still_running_is_left_alone(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.ACTIVE,
        end=NOW + timedelta(days=5),
    )

    assert await _worker(db_session).run_once(now=NOW) == 0


async def test_a_subscription_that_already_ended_is_never_picked_up_again(db_session):
    """Its period ending is the past, not an event. Sweeping it forever would
    rewrite `ended_at` on every pass."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.CANCELLED,
        end=NOW - timedelta(days=90),
    )

    assert await _worker(db_session).run_once(now=NOW) == 0


async def test_an_unpaid_subscription_rolls_over_still_unpaid(db_session):
    """A new period does not settle an old debt."""
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    subscription = await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.PAST_DUE,
    )

    await _worker(db_session).run_once(now=NOW)

    assert subscription.status is SubscriptionStatus.PAST_DUE
    assert subscription.current_period_end > NOW


async def test_the_sweep_crosses_workspaces(db_session):
    """The one query in this module that is supposed to: a platform sweep sees
    every workspace, which is why it uses the platform repository."""
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    plan = await _plan(db_session)
    await _subscription(db_session, acme, plan, status=SubscriptionStatus.TRIALING)
    await _subscription(db_session, rival, plan, status=SubscriptionStatus.TRIALING)

    assert await _worker(db_session).run_once(now=NOW) == 2


async def test_a_sweep_is_bounded(db_session):
    """Rows are held until the commit, so ten thousand renewals on the first of
    the month take several passes rather than one enormous transaction."""
    plan = await _plan(db_session)
    for index in range(3):
        tenant = await _tenant(db_session, f"tenant-{index}")
        await _subscription(db_session, tenant, plan, status=SubscriptionStatus.ACTIVE)

    worker = BillingWorker(
        database=SessionHandle(db_session),
        settings=_settings(),
        claim_limit=2,
    )
    assert await worker.run_once(now=NOW) == 2


async def test_the_due_query_ignores_what_is_not_due(db_session):
    tenant = await _tenant(db_session)
    plan = await _plan(db_session)
    await _subscription(
        db_session,
        tenant,
        plan,
        status=SubscriptionStatus.ACTIVE,
        end=NOW + timedelta(days=1),
    )

    due = await PlatformSubscriptionRepository(db_session).due(now=NOW)
    assert list(due) == []


# ------------------------------------------------------------- registration


def _auth(session, settings) -> AuthService:
    return AuthService(
        session=session,
        settings=settings,
        token_store=RefreshTokenStore(FakeTokenStore()),
    )


async def test_registering_puts_the_workspace_on_the_default_plan(db_session):
    await _plan(db_session, code="starter", trial_days=14)
    settings = _settings(default_plan_code="starter")

    session_result = await _auth(db_session, settings).register(
        email="owner@acme-example.com",
        password="a-very-long-password-1",
        workspace_name="Acme",
        workspace_slug="acme",
    )
    await db_session.flush()

    tenant_id = session_result.workspace.tenant.id
    subscription = await SubscriptionRepository(db_session, tenant_id=tenant_id).get()
    assert subscription is not None
    assert subscription.status is SubscriptionStatus.TRIALING


async def test_registration_survives_a_catalogue_that_has_no_such_plan(db_session):
    """A signup that 500s over billing configuration is the least forgivable
    failure in the product. The workspace is still entitled to the default plan
    by code, so the worst case is a missing row."""
    settings = _settings(default_plan_code="not-a-plan")

    session_result = await _auth(db_session, settings).register(
        email="owner@acme-example.com",
        password="a-very-long-password-1",
        workspace_name="Acme",
        workspace_slug="acme",
    )
    await db_session.flush()

    tenant_id = session_result.workspace.tenant.id
    assert await SubscriptionRepository(db_session, tenant_id=tenant_id).get() is None


async def test_a_second_workspace_gets_its_own_subscription(db_session):
    """One per workspace, not one per person: the same owner registering twice
    is two companies with two arrangements."""
    await _plan(db_session, code="starter")
    settings = _settings(default_plan_code="starter")
    service = _auth(db_session, settings)

    first = await service.register(
        email="one@acme-example.com",
        password="a-very-long-password-1",
        workspace_name="One",
        workspace_slug="one",
    )
    second = await service.register(
        email="two@acme-example.com",
        password="a-very-long-password-1",
        workspace_name="Two",
        workspace_slug="two",
    )
    await db_session.flush()

    for result in (first, second):
        tenant_id = result.workspace.tenant.id
        assert await SubscriptionRepository(db_session, tenant_id=tenant_id).get() is not None


async def test_a_workspace_created_before_billing_can_still_subscribe(db_session):
    """The upgrade path for every workspace that predates this phase."""
    tenant = await _tenant(db_session)
    await _plan(db_session, code="pro")

    subscription = await SubscriptionService(db_session, tenant_id=tenant.id).start(
        plan_code="pro",
        now=NOW,
    )

    assert subscription.status is SubscriptionStatus.ACTIVE
    assert subscription.tenant_id == tenant.id
