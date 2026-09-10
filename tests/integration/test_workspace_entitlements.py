"""How many workspaces an account may own comes from its plan, not from a constant.

`MAX_OWNED_WORKSPACES_PER_USER = 10` was a judgement call made when
`POST /workspaces` was added and there was no entitlement mechanism reaching
across tenants - a number that applied equally to a free account and to an
enterprise customer, which is not a pricing model. The limit is now
`plans.limits["owned_workspaces"]`, alongside everything else a plan sells.

Two things make it unlike every other limit in the product, and both are what
this suite is about.

**It is per *account*, not per workspace.** `EntitlementService` resolves one
tenant's subscription and counts one tenant's rows; this question spans every
tenant somebody owns. So the resolution rule has to answer "which plan applies
to a *person*", and the answer is **the most generous limit among the plans they
are actually paying for** - see `WorkspaceEntitlementService` for why the
alternatives are all worse.

**There is no tenant to lock at creation time**, which is exactly what makes the
limit bypassable by two simultaneous requests. An advisory lock keyed on the
owner is what closes that, and the concurrency test that proves it lives in
`test_lifecycle_concurrency.py` where the committed-transaction harness is.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import hash_password
from app.db.models import Membership, Tenant, TenantRole, User
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.enums import TenantStatus
from app.main import create_app
from app.services.workspace_entitlement_service import WorkspaceEntitlementService
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return -1

    async def rpush(self, key: str, value: str) -> int:
        return 1


class _Infra:
    def __init__(self) -> None:
        self.commands = _Redis()

    @property
    def client(self) -> _Redis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def plan_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        default_plan_code="ent-free",
    )


@pytest.fixture
def app(plan_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(plan_settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as client:
        yield client


async def _plan(session: AsyncSession, *, code: str, owned: int | None) -> Plan:
    """A plan whose `owned_workspaces` limit is `owned`, or unlimited if None."""
    existing = await session.scalar(select(Plan).where(Plan.code == code))
    if existing is not None:
        return existing
    limits: dict[str, Any] = {}
    if owned is not None:
        limits[LimitKey.OWNED_WORKSPACES.value] = owned
    plan = Plan(
        code=code,
        name=code.title(),
        price=Decimal("0.00"),
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits=limits,
    )
    session.add(plan)
    await session.flush()
    return plan


async def _user(session: AsyncSession, email: str) -> User:
    user = User(
        email=email,
        full_name="Owner",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=NOW,
    )
    session.add(user)
    await session.flush()
    return user


async def _owned_workspace(
    session: AsyncSession,
    *,
    slug: str,
    owner: User,
    plan: Plan | None = None,
    status: TenantStatus = TenantStatus.ACTIVE,
    subscription_status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    deleted: bool = False,
    role: TenantRole = TenantRole.TENANT_OWNER,
) -> Tenant:
    tenant = Tenant(
        name=slug.title(),
        slug=slug,
        status=status,
        deleted_at=NOW if deleted else None,
    )
    session.add(tenant)
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=owner.id, role=role))
    if plan is not None:
        session.add(
            Subscription(
                tenant_id=tenant.id,
                plan_id=plan.id,
                status=subscription_status,
                current_period_start=NOW - timedelta(days=1),
                current_period_end=NOW + timedelta(days=29),
            )
        )
    await session.flush()
    return tenant


async def _headers(http: AsyncClient, email: str) -> dict[str, str]:
    response = await http.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}


async def _create(http: AsyncClient, headers: dict[str, str], slug: str) -> Any:
    return await http.post(
        f"{API}/workspaces",
        json={"name": slug.title(), "slug": slug},
        headers=headers,
    )


# ------------------------------------------------------------- resolution


async def test_an_account_that_owns_nothing_gets_the_default_plans_limit(
    db_session: AsyncSession,
    plan_settings: Settings,
) -> None:
    """The Google-first case, and the common one."""
    await _plan(db_session, code="ent-free", owned=1)
    user = await _user(db_session, "fresh@example.com")

    allowance = await WorkspaceEntitlementService(db_session, settings=plan_settings).allowance(
        user=user
    )

    assert allowance.limit == 1
    assert allowance.owned == 0
    assert allowance.allowed is True
    assert allowance.plan_code == "ent-free"


async def test_the_most_generous_plan_among_owned_workspaces_wins(
    db_session: AsyncSession,
    plan_settings: Settings,
) -> None:
    """Somebody paying for Business who also keeps a free sandbox.

    Taking the lowest would hold them to the sandbox's ceiling and the fix
    would be to delete the sandbox, which is not a thing to make a paying
    customer do.
    """
    free = await _plan(db_session, code="ent-free", owned=1)
    business = await _plan(db_session, code="ent-business", owned=5)
    user = await _user(db_session, "mixed@example.com")
    await _owned_workspace(db_session, slug="sandbox", owner=user, plan=free)
    await _owned_workspace(db_session, slug="real-business", owner=user, plan=business)

    allowance = await WorkspaceEntitlementService(db_session, settings=plan_settings).allowance(
        user=user
    )

    assert allowance.limit == 5
    assert allowance.plan_code == "ent-business"
    assert allowance.owned == 2


async def test_an_absent_limit_is_unlimited(
    db_session: AsyncSession,
    plan_settings: Settings,
) -> None:
    """Enterprise, expressed the way the rest of the product expresses it.

    Not a huge integer somebody eventually compares against - the absence of
    the key. `Plan.limit_for` has meant this since the model was written, and
    this limit inherits it rather than inventing a second convention.
    """
    await _plan(db_session, code="ent-free", owned=1)
    enterprise = await _plan(db_session, code="ent-unlimited", owned=None)
    user = await _user(db_session, "enterprise@example.com")
    await _owned_workspace(db_session, slug="ent-one", owner=user, plan=enterprise)

    allowance = await WorkspaceEntitlementService(db_session, settings=plan_settings).allowance(
        user=user
    )

    assert allowance.limit is None
    assert allowance.allowed is True


async def test_a_cancelled_subscriptions_plan_grants_nothing(
    db_session: AsyncSession,
    plan_settings: Settings,
) -> None:
    """Otherwise cancelling keeps the entitlement and stops the invoices.

    The same rule `EntitlementService` already applies to workspace limits.
    """
    await _plan(db_session, code="ent-free", owned=1)
    business = await _plan(db_session, code="ent-business", owned=5)
    user = await _user(db_session, "lapsed@example.com")
    await _owned_workspace(
        db_session,
        slug="lapsed-co",
        owner=user,
        plan=business,
        subscription_status=SubscriptionStatus.CANCELLED,
    )

    allowance = await WorkspaceEntitlementService(db_session, settings=plan_settings).allowance(
        user=user
    )

    assert allowance.limit == 1, "fell back to the default plan"


# --------------------------------------------------------------- counting


async def test_suspended_counts_and_deleted_does_not(
    db_session: AsyncSession,
    plan_settings: Settings,
) -> None:
    """The counting policy, stated as a test.

    *Suspended counts*: the workspace, its data and its subscription are all
    still there, and excluding it would make suspension a way to create beyond
    the plan. *Deleted does not*: there is nothing left to own.
    """
    await _plan(db_session, code="ent-free", owned=5)
    user = await _user(db_session, "counting@example.com")
    await _owned_workspace(db_session, slug="count-active", owner=user)
    await _owned_workspace(
        db_session, slug="count-suspended", owner=user, status=TenantStatus.SUSPENDED
    )
    await _owned_workspace(db_session, slug="count-deleted", owner=user, deleted=True)

    allowance = await WorkspaceEntitlementService(db_session, settings=plan_settings).allowance(
        user=user
    )

    assert allowance.owned == 2


async def test_membership_without_ownership_does_not_count(
    db_session: AsyncSession,
    plan_settings: Settings,
) -> None:
    """Being a colleague in somebody else's business costs your plan nothing.

    Counting it would let one account's limit be exhausted by other people's
    invitations, which is a limit somebody else controls.
    """
    await _plan(db_session, code="ent-free", owned=1)
    user = await _user(db_session, "colleague@example.com")
    other = await _user(db_session, "their-owner@example.com")
    theirs = await _owned_workspace(db_session, slug="somebody-elses", owner=other)
    db_session.add(Membership(tenant_id=theirs.id, user_id=user.id, role=TenantRole.TENANT_ADMIN))
    await db_session.flush()

    allowance = await WorkspaceEntitlementService(db_session, settings=plan_settings).allowance(
        user=user
    )

    assert allowance.owned == 0
    assert allowance.allowed is True


# ------------------------------------------------------------ enforcement


async def test_creation_is_allowed_under_the_limit_and_refused_at_it(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Exactly at the limit is the boundary worth pinning."""
    await _plan(db_session, code="ent-free", owned=2)
    user = await _user(db_session, "at-limit@example.com")
    headers = await _headers(http, user.email)

    first = await _create(http, headers, "limit-one")
    second = await _create(http, headers, "limit-two")
    third = await _create(http, headers, "limit-three")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert third.status_code == 409, third.text
    body = third.json()["error"]
    assert body["code"] == "workspace_limit_reached"
    assert body["details"] == {"limit": 2, "owned": 2}


async def test_deleting_a_workspace_frees_a_slot(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Deleted does not count, so the slot comes back.

    That is the policy, and it has a consequence worth naming: an account can
    delete and recreate freely. It is bounded by the plan at any instant rather
    than over time, which is the right shape for an ownership limit - and the
    billing side is safe because deletion cancels the subscription and revokes
    the cards before the slot is released.
    """
    await _plan(db_session, code="ent-free", owned=1)
    user = await _user(db_session, "recycles@example.com")
    headers = await _headers(http, user.email)

    assert (await _create(http, headers, "recycle-one")).status_code == 201
    assert (await _create(http, headers, "recycle-two")).status_code == 409

    switched = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": "recycle-one"},
        headers=headers,
    )
    workspace_headers = {"Authorization": f"Bearer {switched.json()['access_token']}"}
    deleted = await http.request(
        "DELETE",
        f"{API}/workspace",
        json={"confirmation": "recycle-one"},
        headers=workspace_headers,
    )
    assert deleted.status_code == 200, deleted.text

    assert (await _create(http, headers, "recycle-two")).status_code == 201


async def test_a_downgrade_below_current_usage_keeps_the_workspaces(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The important negative. A downgrade must never destroy anything.

    Somebody who owns five workspaces and moves to a three-workspace plan keeps
    all five. What they lose is the ability to create a sixth - which is a
    refusal they can act on, rather than a deletion they cannot undo.
    """
    small = await _plan(db_session, code="ent-small", owned=1)
    user = await _user(db_session, "downgraded@example.com")
    await _plan(db_session, code="ent-free", owned=1)
    kept_one = await _owned_workspace(db_session, slug="over-one", owner=user, plan=small)
    kept_two = await _owned_workspace(db_session, slug="over-two", owner=user, plan=small)
    kept_three = await _owned_workspace(db_session, slug="over-three", owner=user, plan=small)

    headers = await _headers(http, user.email)
    refused = await _create(http, headers, "over-four")

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["details"] == {"limit": 1, "owned": 3}

    # Every existing workspace is untouched and still usable.
    for tenant in (kept_one, kept_two, kept_three):
        await db_session.refresh(tenant)
        assert tenant.deleted_at is None
        assert tenant.status is TenantStatus.ACTIVE
    usable = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": "over-one"},
        headers=headers,
    )
    assert usable.status_code == 200, usable.text


async def test_an_upgrade_takes_effect_immediately(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The limit is resolved per request, so there is nothing to invalidate."""
    small = await _plan(db_session, code="ent-free", owned=1)
    big = await _plan(db_session, code="ent-big", owned=3)
    user = await _user(db_session, "upgrades@example.com")
    tenant = await _owned_workspace(db_session, slug="upgrade-one", owner=user, plan=small)
    headers = await _headers(http, user.email)

    assert (await _create(http, headers, "upgrade-two")).status_code == 409

    subscription = await db_session.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant.id)
    )
    assert subscription is not None
    subscription.plan_id = big.id
    await db_session.flush()

    assert (await _create(http, headers, "upgrade-two")).status_code == 201


async def test_an_unlimited_plan_is_still_bounded_by_the_safety_ceiling(
    http: AsyncClient,
    db_session: AsyncSession,
    plan_settings: Settings,
) -> None:
    """The two ceilings are different kinds of thing, and both exist.

    The plan limit is what the product sells. The safety limit is a technical
    backstop against an empty catalogue or a plan edited to unlimited by
    mistake; a customer should never meet it, and one who does has found a
    defect rather than an upsell.
    """
    plan_settings.absolute_workspace_safety_limit = 2
    await _plan(db_session, code="ent-free", owned=None)
    user = await _user(db_session, "unbounded@example.com")
    headers = await _headers(http, user.email)

    assert (await _create(http, headers, "safety-one")).status_code == 201
    assert (await _create(http, headers, "safety-two")).status_code == 201
    refused = await _create(http, headers, "safety-three")

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["details"] == {"limit": 2, "owned": 2}


async def test_the_tenant_scoped_service_refuses_an_account_limit(
    db_session: AsyncSession,
) -> None:
    """A refusal of the question, not of the caller.

    `EntitlementService` resolves one workspace's plan and counts one
    workspace's rows. Answering "allowed" from a service that cannot evaluate
    the limit is the shape of a bypass, so it raises instead.
    """
    from app.services.entitlement_service import EntitlementService

    tenant = Tenant(name="Scoped", slug="scoped-refusal", status=TenantStatus.ACTIVE)
    db_session.add(tenant)
    await db_session.flush()
    service = EntitlementService(session=db_session, tenant_id=tenant.id, default_plan_code=None)

    with pytest.raises(ValueError, match="account limit"):
        await service.check(LimitKey.OWNED_WORKSPACES)
