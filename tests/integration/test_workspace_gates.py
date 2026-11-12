"""Workspace gates every member can read (FEB-07, ADR-194).

Four things, each through the real application and a model-built database:

- `/auth/me` gives every workspace its `status`, and keeps a suspended one in
  the list instead of dropping it (WG-01);
- a suspended workspace answers 403 `workspace_suspended` on the routes that
  resolve the active workspace (WG-02);
- a withdrawn member is told `membership_revoked`, while somebody who never
  belonged gets exactly the 404 a random workspace id gets (WG-03);
- `GET /workspace/gates` answers any member, a suspended workspace included,
  with flags and one date and no amount (WG-04).

Every error body is also held to the committed OpenAPI document's envelope.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import create_access_token, hash_password
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.billing import SubscriptionStatus
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.db.models.enums import MembershipStatus
from app.main import create_app
from app.schemas.channel_capacity import ChannelCapacityRead
from app.services import channel_capacity_view
from tests.billing_fixtures import renewal_invoice
from tests.channel_plans import plan_with_channels, subscribe
from tests.conftest import AllowingEntitlements
from tests.integration.test_error_envelope_contract import _assert_contract, _Infra
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

API = "/api/v1"
GATES = f"{API}/workspace/gates"
GATE_KEYS = {
    "status",
    "payment_required",
    "subscription_status",
    "trial_ends_at",
    "channel_capacity",
}

# One route from each router that resolves the active workspace, read as a
# member may read it. Each must refuse a suspended workspace by its own code.
WORKSPACE_ROUTES = (
    "/workspace",
    "/conversations",
    "/contacts",
    "/leads",
    "/agents",
    "/workspace/members",
    "/billing/entitlements",
    "/billing/channel-capacity",
)


@pytest.fixture
def settings_used() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
    )


@pytest.fixture
def app(settings_used: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(settings_used)
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wasla.test") as c:
        yield c


async def _user(session: AsyncSession, *, verified: bool = True) -> User:
    user = User(
        email=f"gates-{uuid.uuid4().hex[:10]}@example.com",
        full_name="Gates",
        hashed_password=hash_password("correct horse battery staple"),
        is_active=True,
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(user)
    await session.flush()
    return user


async def _workspace(
    session: AsyncSession, *, status: TenantStatus = TenantStatus.ACTIVE, name: str = "Gates"
) -> Tenant:
    tenant = Tenant(
        timezone=TEST_TIMEZONE,
        name=name,
        slug=f"gates-{uuid.uuid4().hex[:8]}",
        status=status,
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def _join(
    session: AsyncSession,
    user: User,
    tenant: Tenant,
    role: TenantRole = TenantRole.MEMBER,
    *,
    revoked: bool = False,
) -> Membership:
    membership = Membership(
        tenant_id=tenant.id,
        user_id=user.id,
        role=role,
        status=MembershipStatus.REVOKED if revoked else MembershipStatus.ACTIVE,
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    session.add(membership)
    await session.flush()
    return membership


def _bearer(settings: Settings, user: User, tenant_id: uuid.UUID | None) -> dict[str, str]:
    token, _ = create_access_token(
        settings=settings,
        subject=user.id,
        tenant_id=tenant_id,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _subscribed(session: AsyncSession, tenant: Tenant, **kwargs: Any) -> Any:
    plan = await plan_with_channels(session)
    return plan, await subscribe(session, tenant.id, plan, **kwargs)


def _code(response: Response) -> str:
    body: dict[str, Any] = response.json()
    return str(body["error"]["code"])


# ------------------------------------------------------------- WG-01 /auth/me


async def test_auth_me_gives_every_workspace_its_status_and_keeps_a_suspended_one(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    running = await _workspace(db_session, name="Running")
    paused = await _workspace(db_session, status=TenantStatus.SUSPENDED, name="Paused")
    withdrawn = await _workspace(db_session, name="Withdrawn")
    gone = await _workspace(db_session, name="Gone")
    gone.deleted_at = datetime.now(UTC)
    await _join(db_session, user, running, TenantRole.TENANT_OWNER)
    await _join(db_session, user, paused, TenantRole.TENANT_ADMIN)
    await _join(db_session, user, withdrawn, revoked=True)
    await _join(db_session, user, gone, TenantRole.TENANT_OWNER)
    await db_session.flush()

    response = await http.get(f"{API}/auth/me", headers=_bearer(settings_used, user, running.id))

    assert response.status_code == 200, response.text
    listed = {item["slug"]: item for item in response.json()["workspaces"]}
    assert set(listed) == {running.slug, paused.slug}
    assert listed[running.slug]["status"] == "active"
    assert listed[paused.slug]["status"] == "suspended"
    assert listed[paused.slug]["role"] == "tenant_admin"
    assert set(listed[paused.slug]) == {"id", "name", "slug", "role", "status"}


async def test_auth_me_still_answers_while_the_token_names_a_suspended_workspace(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    paused = await _workspace(db_session, status=TenantStatus.SUSPENDED)
    await _join(db_session, user, paused)

    response = await http.get(f"{API}/auth/me", headers=_bearer(settings_used, user, paused.id))

    assert response.status_code == 200, response.text
    assert [item["status"] for item in response.json()["workspaces"]] == ["suspended"]


async def test_the_switch_answer_carries_the_status(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    first = await _workspace(db_session)
    second = await _workspace(db_session)
    await _join(db_session, user, first)
    await _join(db_session, user, second)

    response = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": second.slug},
        headers=_bearer(settings_used, user, first.id),
    )

    assert response.status_code == 200, response.text
    assert response.json()["active_workspace"]["status"] == "active"


# ---------------------------------------------------- WG-02 suspended → own code


@pytest.mark.parametrize("path", WORKSPACE_ROUTES)
async def test_a_suspended_workspace_says_so_by_its_own_code(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings, path: str
) -> None:
    user = await _user(db_session)
    paused = await _workspace(db_session, status=TenantStatus.SUSPENDED)
    await _join(db_session, user, paused, TenantRole.TENANT_OWNER)

    response = await http.get(f"{API}{path}", headers=_bearer(settings_used, user, paused.id))

    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] == "This workspace is not available."
    _assert_contract(response, f"{API}{path}", "GET", "workspace_suspended")


async def test_a_deleted_workspace_keeps_the_generic_refusal(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    gone = await _workspace(db_session)
    await _join(db_session, user, gone, TenantRole.TENANT_OWNER)
    gone.deleted_at = datetime.now(UTC)
    await db_session.flush()

    response = await http.get(f"{API}/workspace", headers=_bearer(settings_used, user, gone.id))

    assert response.status_code == 403, response.text
    assert _code(response) == "permission_denied"


# ---------------------------------------- WG-03 revoked is told, stranger is not


async def test_a_withdrawn_member_is_told_membership_revoked(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    tenant = await _workspace(db_session)
    await _join(db_session, user, tenant, revoked=True)

    response = await http.get(
        f"{API}/conversations", headers=_bearer(settings_used, user, tenant.id)
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["message"] == "You no longer have access to this workspace."
    _assert_contract(response, f"{API}/conversations", "GET", "membership_revoked")


async def test_a_withdrawn_member_of_a_suspended_workspace_is_still_told_revoked(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    paused = await _workspace(db_session, status=TenantStatus.SUSPENDED)
    await _join(db_session, user, paused, revoked=True)

    response = await http.get(f"{API}/workspace", headers=_bearer(settings_used, user, paused.id))

    assert response.status_code == 403
    assert _code(response) == "membership_revoked"


async def test_somebody_who_never_belonged_learns_nothing(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    """The same answer for a real workspace they never joined as for a random id."""
    stranger = await _user(db_session)
    real = await _workspace(db_session)
    owner = await _user(db_session)
    await _join(db_session, owner, real, TenantRole.TENANT_OWNER)
    paused = await _workspace(db_session, status=TenantStatus.SUSPENDED)

    answers = [
        await http.get(f"{API}{path}", headers=_bearer(settings_used, stranger, workspace_id))
        for workspace_id in (real.id, paused.id, uuid.uuid4())
        for path in ("/conversations", "/workspace/gates")
    ]

    assert {response.status_code for response in answers} == {404}
    assert {_code(response) for response in answers} == {"not_found"}
    assert len({response.json()["error"]["message"] for response in answers}) == 1


async def test_a_deleted_workspace_answers_the_generic_404_even_to_its_former_members(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    gone = await _workspace(db_session)
    await _join(db_session, user, gone, revoked=True)
    gone.deleted_at = datetime.now(UTC)
    await db_session.flush()

    response = await http.get(f"{API}/workspace", headers=_bearer(settings_used, user, gone.id))

    assert response.status_code == 404
    assert _code(response) == "not_found"


async def test_a_member_let_back_in_is_answered_again(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    tenant = await _workspace(db_session)
    membership = await _join(db_session, user, tenant, revoked=True)
    headers = _bearer(settings_used, user, tenant.id)
    assert _code(await http.get(f"{API}/workspace", headers=headers)) == "membership_revoked"

    membership.status = MembershipStatus.ACTIVE
    membership.revoked_at = None
    await db_session.flush()

    response = await http.get(f"{API}/workspace", headers=headers)

    assert response.status_code == 200, response.text


# --------------------------------------------------- WG-04 GET /workspace/gates


@pytest.mark.parametrize(
    "role", [TenantRole.TENANT_OWNER, TenantRole.TENANT_ADMIN, TenantRole.MEMBER]
)
async def test_every_member_reads_the_gates_and_nothing_more(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings, role: TenantRole
) -> None:
    user = await _user(db_session)
    tenant = await _workspace(db_session)
    await _join(db_session, user, tenant, role)
    await _subscribed(db_session, tenant)

    response = await http.get(GATES, headers=_bearer(settings_used, user, tenant.id))

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == GATE_KEYS
    assert set(body["channel_capacity"]) == {"over_limit", "reduction_pending"}
    assert body == {
        "status": "active",
        "payment_required": False,
        "subscription_status": "active",
        "trial_ends_at": None,
        "channel_capacity": {"over_limit": False, "reduction_pending": False},
    }


async def test_the_gates_answer_for_a_suspended_workspace(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    paused = await _workspace(db_session, status=TenantStatus.SUSPENDED)
    await _join(db_session, user, paused)
    await _subscribed(db_session, paused, status=SubscriptionStatus.SUSPENDED)

    response = await http.get(GATES, headers=_bearer(settings_used, user, paused.id))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "suspended"
    assert response.json()["subscription_status"] == "suspended"


async def test_a_trial_gives_its_end(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    tenant = await _workspace(db_session)
    await _join(db_session, user, tenant)
    _, subscription = await _subscribed(db_session, tenant, status=SubscriptionStatus.TRIALING)
    ends = datetime(2031, 3, 1, 12, 0, tzinfo=UTC)
    subscription.trial_ends_at = ends
    await db_session.flush()

    body = (await http.get(GATES, headers=_bearer(settings_used, user, tenant.id))).json()

    assert body["subscription_status"] == "trialing"
    assert datetime.fromisoformat(body["trial_ends_at"]) == ends


async def test_an_owed_renewal_is_a_flag_for_a_member_and_never_an_amount(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    member = await _user(db_session)
    tenant = await _workspace(db_session)
    await _join(db_session, member, tenant)
    plan, subscription = await _subscribed(db_session, tenant, status=SubscriptionStatus.PAST_DUE)
    invoice = await renewal_invoice(
        db_session, subscription=subscription, plan=plan, amount_due=Decimal("450.00")
    )
    headers = _bearer(settings_used, member, tenant.id)

    response = await http.get(GATES, headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["payment_required"] is True
    assert body["subscription_status"] == "past_due"
    text = response.text
    for secret in (str(invoice.id), plan.code, f"{invoice.amount_due:.2f}", invoice.currency):
        assert secret not in text
    # The member still cannot read what the owner reads.
    assert (await http.get(f"{API}/billing/summary", headers=headers)).status_code == 403


async def test_no_subscription_is_null(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    tenant = await _workspace(db_session)
    await _join(db_session, user, tenant)

    response = await http.get(GATES, headers=_bearer(settings_used, user, tenant.id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["subscription_status"], body["trial_ends_at"], body["payment_required"]) == (
        None,
        None,
        False,
    )


async def test_an_open_capacity_reduction_is_pending(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user = await _user(db_session)
    tenant = await _workspace(db_session)
    await _join(db_session, user, tenant)
    await _subscribed(db_session, tenant)
    now = datetime.now(UTC)
    db_session.add(
        ChannelCapacityReduction(
            tenant_id=tenant.id,
            cause=CapacityReductionCause.TOPUP_EXPIRED,
            status=CapacityReductionStatus.PENDING_SELECTION,
            target_general=1,
            target_typed={},
            target_allowed_types=["whatsapp"],
            effective_at=now,
            grace_ends_at=now + timedelta(days=7),
        )
    )
    await db_session.flush()

    body = (await http.get(GATES, headers=_bearer(settings_used, user, tenant.id))).json()

    assert body["channel_capacity"] == {"over_limit": False, "reduction_pending": True}


async def test_over_limit_comes_from_the_capacity_view(
    http: AsyncClient,
    db_session: AsyncSession,
    settings_used: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is the capacity view's own `over_limit`, not a second computation."""
    user = await _user(db_session)
    tenant = await _workspace(db_session)
    await _join(db_session, user, tenant)
    await _subscribed(db_session, tenant)
    real = channel_capacity_view.read_channel_capacity

    async def over(session: AsyncSession, tenant_id: uuid.UUID, settings: Settings) -> Any:
        view: ChannelCapacityRead = await real(session, tenant_id, settings)
        return view.model_copy(update={"over_limit": True})

    monkeypatch.setattr(channel_capacity_view, "read_channel_capacity", over)

    body = (await http.get(GATES, headers=_bearer(settings_used, user, tenant.id))).json()

    assert body["channel_capacity"]["over_limit"] is True


async def test_the_gates_refuse_a_withdrawn_member_and_an_unverified_one(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    withdrawn = await _user(db_session)
    unverified = await _user(db_session, verified=False)
    tenant = await _workspace(db_session)
    await _join(db_session, withdrawn, tenant, revoked=True)
    await _join(db_session, unverified, tenant)

    revoked = await http.get(GATES, headers=_bearer(settings_used, withdrawn, tenant.id))
    gated = await http.get(GATES, headers=_bearer(settings_used, unverified, tenant.id))
    none = await http.get(GATES, headers=_bearer(settings_used, withdrawn, None))

    assert revoked.status_code == 403
    _assert_contract(revoked, GATES, "GET", "membership_revoked")
    assert gated.status_code == 403
    _assert_contract(gated, GATES, "GET", "email_verification_required")
    assert none.status_code == 403
    assert _code(none) == "permission_denied"
