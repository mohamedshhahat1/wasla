"""Platform deletion of a last owner must not leave an ACTIVE ownerless workspace.

The asymmetry this file is about: `DELETE /auth/me` **refuses** while the caller
is somebody's last owner, and `DELETE /platform/users/{id}` **does not**. That is
deliberate - an abusive or compromised account must not become undeletable by
owning a workspace, and a refusal there would mean the account most worth
removing is the one that cannot be removed.

What follows from allowing it is the state this file exists to prevent:
`status = ACTIVE` with zero active owners. Such a workspace cannot be
administered by anybody, because inviting an owner, changing the plan and
closing it are all owner-only - it is unrecoverable from the inside, which is
the same failure the tenant lock and the last-owner rules exist to stop
customers producing.

So the workspace is **suspended** instead: service stops, the reason is audited,
and `POST /platform/tenants/{id}/ownership` puts it back under somebody's
control. Restoration is refused until that has happened, which is what stops the
recovery path from recreating the invalid state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
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
from app.db.models import (
    Membership,
    MembershipStatus,
    PlatformRole,
    Tenant,
    TenantRole,
    User,
)
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.enums import TenantStatus
from app.main import create_app
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"


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
def orphan_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        default_plan_code="",
    )


@pytest.fixture
def app(orphan_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(orphan_settings)
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


async def _user(
    session: AsyncSession,
    *,
    email: str,
    platform_role: PlatformRole | None = None,
) -> User:
    user = User(
        email=email,
        full_name=email.split("@")[0].title(),
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
        platform_role=platform_role,
    )
    session.add(user)
    await session.flush()
    return user


async def _workspace(session: AsyncSession, *, slug: str, owner: User) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=owner.id, role=TenantRole.TENANT_OWNER))
    await session.flush()
    return tenant


async def _join(
    session: AsyncSession, *, tenant: Tenant, user: User, role: TenantRole
) -> Membership:
    membership = Membership(tenant_id=tenant.id, user_id=user.id, role=role)
    session.add(membership)
    await session.flush()
    return membership


async def _staff_headers(http: AsyncClient, email: str) -> dict[str, str]:
    response = await http.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}


async def _entries(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    rows = await session.execute(select(AuditLog).where(AuditLog.action == action))
    return list(rows.scalars())


# --------------------------------------------------------- the suspension


async def test_deleting_the_final_owner_suspends_the_workspace(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The deletion succeeds *and* the workspace becomes non-operational.

    Both halves matter. Refusing the deletion would make ownership a shield for
    an abusive account; allowing it without the suspension would leave a
    workspace that answers requests and that nobody can administer.
    """
    target = await _user(db_session, email="sole-owner@example.com")
    tenant = await _workspace(db_session, slug="about-to-orphan", owner=target)
    colleague = await _user(db_session, email="colleague@example.com")
    await _join(db_session, tenant=tenant, user=colleague, role=TenantRole.MEMBER)
    staff = await _user(
        db_session,
        email="staff@example.com",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )

    headers = await _staff_headers(http, staff.email)
    response = await http.delete(f"{API}/platform/users/{target.id}", headers=headers)

    assert response.status_code == 200, response.text
    await db_session.refresh(tenant)
    assert tenant.status is TenantStatus.SUSPENDED
    assert tenant.deleted_at is None, "suspension is not deletion"

    suspensions = await _entries(db_session, AuditAction.WORKSPACE_SUSPENDED)
    assert len(suspensions) == 1
    assert suspensions[0].meta == {"reason": "last_owner_removed_by_platform"}
    assert suspensions[0].tenant_id == tenant.id


async def test_deleting_one_of_two_owners_leaves_the_workspace_active(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The control. Suspension is for the workspace that is actually orphaned.

    A rule that suspended on every owner deletion would punish a healthy
    workspace for losing one of its administrators.
    """
    target = await _user(db_session, email="one-of-two@example.com")
    tenant = await _workspace(db_session, slug="still-owned", owner=target)
    second = await _user(db_session, email="other-owner@example.com")
    await _join(db_session, tenant=tenant, user=second, role=TenantRole.TENANT_OWNER)
    staff = await _user(
        db_session,
        email="staff-two@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )

    headers = await _staff_headers(http, staff.email)
    assert (
        await http.delete(f"{API}/platform/users/{target.id}", headers=headers)
    ).status_code == 200

    await db_session.refresh(tenant)
    assert tenant.status is TenantStatus.ACTIVE
    assert await _entries(db_session, AuditAction.WORKSPACE_SUSPENDED) == []


async def test_an_already_suspended_workspace_is_not_suspended_twice(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """No second audit entry for a state that did not change.

    An operator reading the trail should see the suspension that happened, not
    one entry per subsequent owner removal.
    """
    target = await _user(db_session, email="already@example.com")
    tenant = await _workspace(db_session, slug="already-suspended", owner=target)
    tenant.status = TenantStatus.SUSPENDED
    await db_session.flush()
    staff = await _user(
        db_session,
        email="staff-three@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )

    headers = await _staff_headers(http, staff.email)
    assert (
        await http.delete(f"{API}/platform/users/{target.id}", headers=headers)
    ).status_code == 200

    assert await _entries(db_session, AuditAction.WORKSPACE_SUSPENDED) == []


async def test_the_suspended_orphan_stops_serving_its_remaining_members(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Suspension has to actually stop the workspace, not merely label it."""
    target = await _user(db_session, email="departing@example.com")
    tenant = await _workspace(db_session, slug="stops-serving", owner=target)
    colleague = await _user(db_session, email="left-behind@example.com")
    await _join(db_session, tenant=tenant, user=colleague, role=TenantRole.TENANT_ADMIN)
    staff = await _user(
        db_session,
        email="staff-four@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )

    # The colleague has a live workspace session before the deletion.
    colleague_login = await http.post(
        f"{API}/auth/login",
        json={"email": colleague.email, "password": PASSWORD},
    )
    switched = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": "stops-serving"},
        headers={"Authorization": f"Bearer {colleague_login.json()['access_token']}"},
    )
    workspace_headers = {"Authorization": f"Bearer {switched.json()['access_token']}"}
    assert (
        await http.get(f"{API}/workspace/members", headers=workspace_headers)
    ).status_code == 200

    headers = await _staff_headers(http, staff.email)
    assert (
        await http.delete(f"{API}/platform/users/{target.id}", headers=headers)
    ).status_code == 200

    # The same token, one request later. Nothing had to expire.
    assert (
        await http.get(f"{API}/workspace/members", headers=workspace_headers)
    ).status_code == 403


# ------------------------------------------------------------- the recovery


async def test_platform_staff_repair_ownership_and_then_restore(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The whole recovery path, in the order an operator performs it."""
    target = await _user(db_session, email="gone-owner@example.com")
    tenant = await _workspace(db_session, slug="recoverable", owner=target)
    colleague = await _user(db_session, email="successor@example.com")
    membership = await _join(db_session, tenant=tenant, user=colleague, role=TenantRole.MEMBER)
    staff = await _user(
        db_session,
        email="staff-five@example.com",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    headers = await _staff_headers(http, staff.email)

    assert (
        await http.delete(f"{API}/platform/users/{target.id}", headers=headers)
    ).status_code == 200

    # Restoring first is refused: the workspace still has nobody in charge.
    premature = await http.post(f"{API}/platform/tenants/{tenant.id}/restore", headers=headers)
    assert premature.status_code == 409, premature.text
    assert premature.json()["error"]["code"] == "workspace_orphaned"

    repaired = await http.post(
        f"{API}/platform/tenants/{tenant.id}/ownership",
        json={"user_id": str(colleague.id)},
        headers=headers,
    )
    assert repaired.status_code == 200, repaired.text
    # Still suspended: repairing ownership and resuming service are two acts.
    assert repaired.json()["status"] == TenantStatus.SUSPENDED.value

    await db_session.refresh(membership)
    assert membership.role is TenantRole.TENANT_OWNER
    assert membership.status is MembershipStatus.ACTIVE

    restored = await http.post(f"{API}/platform/tenants/{tenant.id}/restore", headers=headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == TenantStatus.ACTIVE.value

    entries = await _entries(db_session, AuditAction.WORKSPACE_OWNERSHIP_REPAIRED)
    assert len(entries) == 1
    assert entries[0].meta is not None
    assert entries[0].meta["to_user"] == colleague.email


async def test_ownership_repair_can_reinstate_somebody_removed_alongside_the_owner(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A revoked membership is eligible, and usually the only candidate.

    The colleagues left behind are frequently the ones whose access went with
    the owner - if the repair could only promote *active* members, the common
    case would have nobody to promote.
    """
    target = await _user(db_session, email="owner-removed@example.com")
    tenant = await _workspace(db_session, slug="revoked-candidate", owner=target)
    colleague = await _user(db_session, email="was-removed@example.com")
    membership = await _join(db_session, tenant=tenant, user=colleague, role=TenantRole.MEMBER)
    membership.status = MembershipStatus.REVOKED
    membership.revoked_at = datetime.now(UTC)
    await db_session.flush()
    staff = await _user(
        db_session,
        email="staff-six@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    headers = await _staff_headers(http, staff.email)
    assert (
        await http.delete(f"{API}/platform/users/{target.id}", headers=headers)
    ).status_code == 200

    repaired = await http.post(
        f"{API}/platform/tenants/{tenant.id}/ownership",
        json={"user_id": str(colleague.id)},
        headers=headers,
    )

    assert repaired.status_code == 200, repaired.text
    await db_session.refresh(membership)
    assert membership.status is MembershipStatus.ACTIVE
    assert membership.role is TenantRole.TENANT_OWNER


async def test_repair_refuses_a_workspace_that_still_has_an_owner(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Otherwise this is a general role-editing tool for platform staff.

    Its whole justification is repairing a state customers cannot repair
    themselves. A workspace with an owner is administrable by its own people,
    and staff have no business in its roster.
    """
    owner = await _user(db_session, email="healthy-owner@example.com")
    tenant = await _workspace(db_session, slug="healthy", owner=owner)
    member = await _user(db_session, email="ordinary@example.com")
    await _join(db_session, tenant=tenant, user=member, role=TenantRole.MEMBER)
    staff = await _user(
        db_session,
        email="staff-seven@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )

    headers = await _staff_headers(http, staff.email)
    response = await http.post(
        f"{API}/platform/tenants/{tenant.id}/ownership",
        json={"user_id": str(member.id)},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "workspace_has_owner"


async def test_repair_cannot_admit_somebody_who_was_never_a_member(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The escalation this endpoint would otherwise be.

    Staff must not be able to add an account - their own included - to a
    customer's workspace. This promotes an existing member; it does not admit.
    """
    target = await _user(db_session, email="orphan-owner@example.com")
    tenant = await _workspace(db_session, slug="no-admission", owner=target)
    staff = await _user(
        db_session,
        email="staff-eight@example.com",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    headers = await _staff_headers(http, staff.email)
    assert (
        await http.delete(f"{API}/platform/users/{target.id}", headers=headers)
    ).status_code == 200

    # Staff naming themselves is the case worth being explicit about.
    response = await http.post(
        f"{API}/platform/tenants/{tenant.id}/ownership",
        json={"user_id": str(staff.id)},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "workspace_owner_required"
    memberships = await db_session.execute(
        select(Membership).where(
            Membership.tenant_id == tenant.id,
            Membership.user_id == staff.id,
        )
    )
    assert list(memberships.scalars()) == []


async def test_repair_refuses_a_disabled_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """An owner who cannot sign in satisfies the count and not the invariant."""
    target = await _user(db_session, email="orphan-two@example.com")
    tenant = await _workspace(db_session, slug="disabled-candidate", owner=target)
    candidate = await _user(db_session, email="disabled-member@example.com")
    await _join(db_session, tenant=tenant, user=candidate, role=TenantRole.MEMBER)
    candidate.is_active = False
    await db_session.flush()
    staff = await _user(
        db_session,
        email="staff-nine@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    headers = await _staff_headers(http, staff.email)
    assert (
        await http.delete(f"{API}/platform/users/{target.id}", headers=headers)
    ).status_code == 200

    response = await http.post(
        f"{API}/platform/tenants/{tenant.id}/ownership",
        json={"user_id": str(candidate.id)},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "workspace_owner_required"


async def test_a_workspace_owner_cannot_repair_ownership_anywhere(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Platform authority, not workspace authority. Stated as its own test."""
    attacker = await _user(db_session, email="ambitious@example.com")
    await _workspace(db_session, slug="attacker-own", owner=attacker)
    victim = await _user(db_session, email="victim-owner@example.com")
    target = await _workspace(db_session, slug="victim-workspace", owner=victim)

    headers = await _staff_headers(http, attacker.email)
    response = await http.post(
        f"{API}/platform/tenants/{target.id}/ownership",
        json={"user_id": str(attacker.id)},
        headers=headers,
    )

    assert response.status_code == 403, response.text


async def test_disabling_the_final_owner_also_suspends_the_workspace(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Disabling is reversible and still removes the person from every roster.

    A disabled account cannot sign in, so a workspace whose only owner is
    disabled is exactly as unadministrable as one whose owner was deleted. The
    two paths share `_withdraw_memberships`, so this is really a test that the
    sharing is real rather than two implementations that happen to agree.
    """
    target = await _user(db_session, email="to-disable@example.com")
    tenant = await _workspace(db_session, slug="disabled-owner-co", owner=target)
    staff = await _user(
        db_session,
        email="staff-ten@example.com",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )

    headers = await _staff_headers(http, staff.email)
    response = await http.post(
        f"{API}/platform/users/{target.id}/disable",
        headers=headers,
    )
    assert response.status_code == 200, response.text

    await db_session.refresh(tenant)
    # Disable does not withdraw memberships today - it ends sessions and blocks
    # authentication - so the workspace is not suspended by it. Asserted rather
    # than assumed, because the two paths are easy to conflate: what makes the
    # workspace unusable here is that its only owner cannot sign in, which is a
    # weaker guarantee than the deletion path's and is worth being explicit
    # about in docs/AUTHORIZATION.md rather than silently different.
    assert tenant.status is TenantStatus.ACTIVE
