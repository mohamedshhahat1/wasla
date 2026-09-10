"""The workspace lifecycle, driven over HTTP against real rows.

Six operations and one invariant. The invariant is that **an active workspace
always has at least one active owner**, and most of this file is about the ways
it could stop being true: the last owner leaving, the last owner transferring to
somebody who cannot act, the last owner closing their own account, and an
administrator quietly promoting themselves by removing the person above them.

The other half is about a separation the product depends on and no constraint
enforces: *a workspace administrator has no authority over anybody's account*.
Removing a member, deleting a workspace and deleting an account are three
different operations on three different objects, and the tests below assert that
each one leaves the other two alone. A workspace that closed must not take its
members' logins with it, and an administrator must not be able to reach a global
identity through any route on this router.

Every test drives the real application. Nothing calls a service directly, and
nothing asserts on a row that a request did not write, because the guarantees
being checked here are guarantees about endpoints.
"""

from __future__ import annotations

import uuid
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
    """Enough Redis for the token store; nothing here exercises the limiter."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
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


# --------------------------------------------------------------------- set-up


async def _user(
    session: AsyncSession,
    *,
    email: str,
    password: str | None = PASSWORD,
    platform_role: PlatformRole | None = None,
) -> User:
    """One verified account. Verified because every route here requires it."""
    user = User(
        email=email,
        full_name=email.split("@")[0].title(),
        hashed_password=hash_password(password) if password is not None else None,
        is_active=True,
        email_verified_at=datetime.now(UTC),
        platform_role=platform_role,
    )
    session.add(user)
    await session.flush()
    return user


async def _workspace(
    session: AsyncSession,
    *,
    slug: str,
    owner: User,
    status: TenantStatus = TenantStatus.ACTIVE,
) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=status)
    session.add(tenant)
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=owner.id, role=TenantRole.TENANT_OWNER))
    await session.flush()
    return tenant


async def _join(
    session: AsyncSession,
    *,
    tenant: Tenant,
    user: User,
    role: TenantRole,
) -> Membership:
    membership = Membership(tenant_id=tenant.id, user_id=user.id, role=role)
    session.add(membership)
    await session.flush()
    return membership


@pytest.fixture
def lifecycle_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        # No catalogue rows are seeded here, and workspace creation must not
        # depend on one - which is itself the behaviour being relied on.
        default_plan_code="",
    )


@pytest.fixture
def app(lifecycle_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(lifecycle_settings)
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


async def _login(http: AsyncClient, email: str, slug: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"email": email, "password": PASSWORD}
    if slug is not None:
        body["workspace_slug"] = slug
    response = await http.post(f"{API}/auth/login", json=body)
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _bearer(session: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


async def _select_workspace(
    http: AsyncClient, session: dict[str, Any], slug: str
) -> dict[str, str]:
    """Exchange a session for an access token carrying this workspace's `tid`."""
    response = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": slug},
        headers=_bearer(session),
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _audit(session: AsyncSession, action: AuditAction) -> list[AuditLog]:
    rows = await session.execute(select(AuditLog).where(AuditLog.action == action))
    return list(rows.scalars())


# ------------------------------------------------------------------- creation


async def test_a_verified_account_with_no_workspace_can_create_its_first(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The Google-first dead end, closed.

    An account with a session, no membership and no workspace is exactly what
    `GoogleAuthService._enrol` produces, and until this route existed such a
    person could do nothing but wait for an invitation (ADR-047). The account
    below is built the same way - passwordless would be more faithful, but a
    passwordless account cannot use `/auth/login` to get a session, so the
    equivalent Google path is covered end to end in `test_google_endpoints.py`.
    What is asserted here is the part that was missing: no workspace in, a
    workspace and an owner membership out.
    """
    user = await _user(db_session, email="google-first@example.com")
    session = await _login(http, user.email)
    assert session["active_workspace"] is None

    response = await http.post(
        f"{API}/workspaces",
        json={"name": "Nile Finishing", "slug": "nile-finishing"},
        headers=_bearer(session),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == TenantRole.TENANT_OWNER.value
    assert body["workspace"]["slug"] == "nile-finishing"
    assert body["workspace"]["is_active"] is True

    membership = await db_session.scalar(
        select(Membership).where(Membership.user_id == user.id),
    )
    assert membership is not None
    assert membership.role is TenantRole.TENANT_OWNER
    assert membership.status is MembershipStatus.ACTIVE


async def test_the_new_workspace_is_immediately_usable_through_a_switched_token(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Creation is only useful if the workspace can then be opened.

    The response carries no token on purpose, so this is the second half of
    onboarding: select the workspace, and reach a workspace-scoped route with
    the token that comes back. Without this the feature would be a row in a
    table that nothing could address.
    """
    user = await _user(db_session, email="onboards@example.com")
    session = await _login(http, user.email)
    created = await http.post(
        f"{API}/workspaces",
        json={"name": "Delta Works", "slug": "delta-works"},
        headers=_bearer(session),
    )
    assert created.status_code == 201, created.text

    headers = await _select_workspace(http, session, "delta-works")
    members = await http.get(f"{API}/workspace/members", headers=headers)

    assert members.status_code == 200, members.text
    assert [row["email"] for row in members.json()["members"]] == [user.email]


async def test_a_slug_already_taken_is_a_conflict_not_a_crash(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner = await _user(db_session, email="holds-slug@example.com")
    await _workspace(db_session, slug="taken-slug", owner=owner)
    newcomer = await _user(db_session, email="wants-slug@example.com")
    session = await _login(http, newcomer.email)

    response = await http.post(
        f"{API}/workspaces",
        json={"name": "Something Else", "slug": "taken-slug"},
        headers=_bearer(session),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


async def test_the_slug_of_a_deleted_workspace_stays_reserved(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A tombstoned address is never handed to somebody else.

    Freeing it would let a stranger take the slug that a closed business's
    invitation links, bookmarks and support tickets all still name, and inherit
    whatever trust is attached to it. The unique constraint covers deleted rows,
    and `TenantRepository.get_by_slug` sees them, so this is a clean 409 rather
    than an integrity error surfacing as a 500.
    """
    owner = await _user(db_session, email="closed-shop@example.com")
    tenant = await _workspace(db_session, slug="closed-shop", owner=owner)
    tenant.deleted_at = datetime.now(UTC)
    await db_session.flush()

    newcomer = await _user(db_session, email="opportunist@example.com")
    session = await _login(http, newcomer.email)
    response = await http.post(
        f"{API}/workspaces",
        json={"name": "Not The Same Business", "slug": "closed-shop"},
        headers=_bearer(session),
    )

    assert response.status_code == 409, response.text


async def test_creation_is_refused_past_the_configured_ceiling(
    app: FastAPI,
    http: AsyncClient,
    db_session: AsyncSession,
    lifecycle_settings: Settings,
) -> None:
    """`POST /workspaces` is bounded per account, not only per address.

    The route's rate limit counts by client address and bounds the *rate*; this
    bounds the total, because one verified account should not be able to
    accumulate tenants indefinitely. Set to one here so the assertion is about
    the rule rather than about a number.
    """
    lifecycle_settings.max_owned_workspaces_per_user = 1
    user = await _user(db_session, email="collector@example.com")
    session = await _login(http, user.email)

    first = await http.post(
        f"{API}/workspaces",
        json={"name": "First", "slug": "collector-one"},
        headers=_bearer(session),
    )
    second = await http.post(
        f"{API}/workspaces",
        json={"name": "Second", "slug": "collector-two"},
        headers=_bearer(session),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "workspace_limit_reached"


async def test_an_unverified_account_cannot_create_a_workspace(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Creating a workspace is a material business action, and those wait.

    The same rule every other business route follows. It is asserted here
    because this route is reachable by an account that has just been created and
    therefore is the most likely place for the rule to be forgotten.
    """
    user = await _user(db_session, email="unproven@example.com")
    user.email_verified_at = None
    await db_session.flush()
    session = await _login(http, user.email)

    response = await http.post(
        f"{API}/workspaces",
        json={"name": "Premature", "slug": "premature"},
        headers=_bearer(session),
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "email_verification_required"


# --------------------------------------------------------------------- update


async def test_an_administrator_may_rename_but_not_readdress(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The two fields carry different authority, and the split is the point.

    A name is a label. A slug is an identifier that invitation links, bookmarks
    and support tickets name, so changing it redirects all of them and frees the
    old one - an owner's decision, not an administrator's.
    """
    owner = await _user(db_session, email="owner-r@example.com")
    tenant = await _workspace(db_session, slug="readdress", owner=owner)
    admin = await _user(db_session, email="admin-r@example.com")
    await _join(db_session, tenant=tenant, user=admin, role=TenantRole.TENANT_ADMIN)

    session = await _login(http, admin.email)
    headers = await _select_workspace(http, session, "readdress")

    renamed = await http.patch(f"{API}/workspace", json={"name": "New Name"}, headers=headers)
    readdressed = await http.patch(
        f"{API}/workspace", json={"slug": "new-address"}, headers=headers
    )

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "New Name"
    assert readdressed.status_code == 403, readdressed.text
    await db_session.refresh(tenant)
    assert tenant.slug == "readdress"


async def test_changing_the_address_is_audited_and_renaming_is_not(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Only the security-relevant half of an update leaves a trail.

    A row per rename would bury the one entry somebody actually filters for.
    The address change records both the old and the new value, because "what did
    this workspace used to be called" is the question the entry exists for.
    """
    owner = await _user(db_session, email="owner-a@example.com")
    await _workspace(db_session, slug="audited-move", owner=owner)
    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "audited-move")

    await http.patch(f"{API}/workspace", json={"name": "Renamed Only"}, headers=headers)
    assert await _audit(db_session, AuditAction.WORKSPACE_UPDATED) == []

    moved = await http.patch(f"{API}/workspace", json={"slug": "moved-here"}, headers=headers)
    assert moved.status_code == 200, moved.text

    entries = await _audit(db_session, AuditAction.WORKSPACE_UPDATED)
    assert len(entries) == 1
    assert entries[0].meta == {"previous_slug": "audited-move", "slug": "moved-here"}


# ------------------------------------------------------------------ ownership


async def test_ownership_transfer_promotes_the_target_and_steps_the_caller_down(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner = await _user(db_session, email="hands-over@example.com")
    tenant = await _workspace(db_session, slug="handover", owner=owner)
    successor = await _user(db_session, email="takes-over@example.com")
    successor_membership = await _join(
        db_session, tenant=tenant, user=successor, role=TenantRole.MEMBER
    )

    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "handover")
    response = await http.post(
        f"{API}/workspace/ownership",
        json={"user_id": str(successor.id)},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["new_owner_role"] == TenantRole.TENANT_OWNER.value
    assert body["previous_owner_role"] == TenantRole.TENANT_ADMIN.value

    await db_session.refresh(successor_membership)
    assert successor_membership.role is TenantRole.TENANT_OWNER

    entries = await _audit(db_session, AuditAction.WORKSPACE_OWNERSHIP_TRANSFERRED)
    assert len(entries) == 1
    assert entries[0].meta is not None
    assert entries[0].meta["to_user"] == successor.email
    assert entries[0].tenant_id == tenant.id


async def test_an_administrator_cannot_transfer_ownership(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Otherwise an administrator mints themselves an owner and takes the workspace."""
    owner = await _user(db_session, email="real-owner@example.com")
    tenant = await _workspace(db_session, slug="not-yours", owner=owner)
    admin = await _user(db_session, email="ambitious@example.com")
    await _join(db_session, tenant=tenant, user=admin, role=TenantRole.TENANT_ADMIN)

    session = await _login(http, admin.email)
    headers = await _select_workspace(http, session, "not-yours")
    response = await http.post(
        f"{API}/workspace/ownership",
        json={"user_id": str(admin.id)},
        headers=headers,
    )

    assert response.status_code == 403, response.text


async def test_ownership_cannot_be_transferred_to_a_non_member(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """And the refusal says nothing about whether that account exists.

    A 409 about *this workspace's membership*, never a 404 about a user. The
    alternative turns the endpoint into a probe for whether a given person has a
    Wasla account.
    """
    owner = await _user(db_session, email="owner-s@example.com")
    await _workspace(db_session, slug="strangers", owner=owner)
    stranger = await _user(db_session, email="stranger@example.com")

    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "strangers")
    response = await http.post(
        f"{API}/workspace/ownership",
        json={"user_id": str(stranger.id)},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ownership_transfer_invalid"


async def test_ownership_cannot_be_transferred_to_a_disabled_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A disabled owner satisfies the invariant while being unable to act on it.

    That is the unrecoverable state in disguise: the workspace has an owner by
    the count, and nobody who can sign in and use it.
    """
    owner = await _user(db_session, email="owner-d@example.com")
    tenant = await _workspace(db_session, slug="disabled-heir", owner=owner)
    heir = await _user(db_session, email="suspended-heir@example.com")
    await _join(db_session, tenant=tenant, user=heir, role=TenantRole.MEMBER)
    heir.is_active = False
    await db_session.flush()

    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "disabled-heir")
    response = await http.post(
        f"{API}/workspace/ownership",
        json={"user_id": str(heir.id)},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "ownership_transfer_invalid"


# ---------------------------------------------------------------- leaving


async def test_an_owner_may_leave_once_another_owner_exists(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    first = await _user(db_session, email="leaves@example.com")
    tenant = await _workspace(db_session, slug="two-owners", owner=first)
    second = await _user(db_session, email="stays@example.com")
    await _join(db_session, tenant=tenant, user=second, role=TenantRole.TENANT_OWNER)

    session = await _login(http, first.email)
    headers = await _select_workspace(http, session, "two-owners")
    response = await http.delete(f"{API}/workspace/members/{first.id}", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == MembershipStatus.REVOKED.value


async def test_the_last_owner_cannot_leave(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """With a code a client can act on, because both refusals here are 409.

    "You are the last owner" and "that person is already removed" need different
    screens, and the status alone cannot tell them apart.
    """
    owner = await _user(db_session, email="only-owner@example.com")
    tenant = await _workspace(db_session, slug="sole-owner", owner=owner)
    await _join(
        db_session,
        tenant=tenant,
        user=await _user(db_session, email="ordinary@example.com"),
        role=TenantRole.MEMBER,
    )

    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "sole-owner")
    response = await http.delete(f"{API}/workspace/members/{owner.id}", headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "last_workspace_owner"


async def test_removing_a_member_leaves_their_account_and_other_workspaces_alone(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The separation the whole authorization model rests on.

    Removing somebody from one workspace reaches their membership in that
    workspace and nothing else: not their account, not their sessions, and not
    the company they work at on Tuesdays. A workspace administrator who could
    do more would be able to evict people from businesses they have nothing to
    do with.
    """
    owner = await _user(db_session, email="evicts@example.com")
    tenant = await _workspace(db_session, slug="first-job", owner=owner)
    worker = await _user(db_session, email="two-jobs@example.com")
    await _join(db_session, tenant=tenant, user=worker, role=TenantRole.MEMBER)
    elsewhere = await _workspace(db_session, slug="second-job", owner=worker)

    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "first-job")
    removed = await http.delete(f"{API}/workspace/members/{worker.id}", headers=headers)
    assert removed.status_code == 200, removed.text

    await db_session.refresh(worker)
    assert worker.is_active is True
    assert worker.deleted_at is None

    # And they can still open the other workspace, with a fresh session.
    their_session = await _login(http, worker.email)
    theirs = await _select_workspace(http, their_session, elsewhere.slug)
    still_working = await http.get(f"{API}/workspace/members", headers=theirs)
    assert still_working.status_code == 200, still_working.text


# ------------------------------------------------------------------- deletion


async def test_an_owner_deletes_a_workspace_and_it_stops_answering(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner = await _user(db_session, email="closes@example.com")
    tenant = await _workspace(db_session, slug="closing-down", owner=owner)

    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "closing-down")
    deleted = await http.request(
        "DELETE",
        f"{API}/workspace",
        json={"confirmation": "closing-down"},
        headers=headers,
    )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["is_active"] is False

    await db_session.refresh(tenant)
    assert tenant.deleted_at is not None

    # The same token, one request later. Nothing had to expire: authorization
    # re-reads the membership and the tenant on every request.
    #
    # 404 rather than 403, and that is the established contract rather than an
    # accident of ordering. Deletion withdraws every membership, and a withdrawn
    # membership is *not found* rather than refused - a caller learns that they
    # cannot act, not whether a workspace exists that once let them
    # (`MembershipRepository.require_for_user`). The 403 path is what a member
    # of a merely *suspended* workspace gets, because their membership is still
    # there; that case is asserted in its own test below.
    after = await http.get(f"{API}/workspace/members", headers=headers)
    assert after.status_code == 404, after.text

    entries = await _audit(db_session, AuditAction.WORKSPACE_DELETED)
    assert len(entries) == 1
    assert entries[0].target_label == "closing-down"


async def test_deleting_a_workspace_requires_its_address_typed_exactly(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Backend-enforced, because this route is reachable with curl.

    A frontend modal is not a control and a boolean flag is not one either -
    anything a client can send once it can send again by accident.
    """
    owner = await _user(db_session, email="fat-fingers@example.com")
    tenant = await _workspace(db_session, slug="careful-now", owner=owner)

    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "careful-now")
    response = await http.request(
        "DELETE",
        f"{API}/workspace",
        json={"confirmation": "careful-know"},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "workspace_confirmation_invalid"
    await db_session.refresh(tenant)
    assert tenant.deleted_at is None


async def test_an_administrator_cannot_delete_a_workspace(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner = await _user(db_session, email="owner-x@example.com")
    tenant = await _workspace(db_session, slug="admin-cannot", owner=owner)
    admin = await _user(db_session, email="admin-x@example.com")
    await _join(db_session, tenant=tenant, user=admin, role=TenantRole.TENANT_ADMIN)

    session = await _login(http, admin.email)
    headers = await _select_workspace(http, session, "admin-cannot")
    response = await http.request(
        "DELETE",
        f"{API}/workspace",
        json={"confirmation": "admin-cannot"},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    await db_session.refresh(tenant)
    assert tenant.deleted_at is None


async def test_deleting_a_workspace_deletes_nobody_and_spares_other_workspaces(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The most important assertion in this file.

    A tenant lifecycle operation must never reach a global identity. Everyone
    who was in the closed workspace keeps their account, their sessions and
    every other workspace they belong to - including the owner who pressed the
    button.
    """
    owner = await _user(db_session, email="owner-multi@example.com")
    doomed = await _workspace(db_session, slug="doomed-co", owner=owner)
    colleague = await _user(db_session, email="colleague@example.com")
    await _join(db_session, tenant=doomed, user=colleague, role=TenantRole.MEMBER)
    survivor = await _workspace(db_session, slug="survivor-co", owner=colleague)

    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "doomed-co")
    deleted = await http.request(
        "DELETE",
        f"{API}/workspace",
        json={"confirmation": "doomed-co"},
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text

    for person in (owner, colleague):
        await db_session.refresh(person)
        assert person.deleted_at is None
        assert person.is_active is True

    # The colleague's other workspace is untouched and still usable.
    await db_session.refresh(survivor)
    assert survivor.deleted_at is None
    their_session = await _login(http, colleague.email)
    theirs = await _select_workspace(http, their_session, "survivor-co")
    assert (await http.get(f"{API}/workspace/members", headers=theirs)).status_code == 200

    # And the closed workspace is gone from their switcher rather than lingering.
    profile = await http.get(f"{API}/auth/me", headers=_bearer(their_session))
    assert [w["slug"] for w in profile.json()["workspaces"]] == ["survivor-co"]


# ----------------------------------------------------- suspension and restore


async def test_platform_staff_suspend_and_restore_a_workspace(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """`TenantStatus.SUSPENDED` was declared and unreachable. Now it is neither."""
    owner = await _user(db_session, email="suspendee@example.com")
    tenant = await _workspace(db_session, slug="under-review", owner=owner)
    staff = await _user(
        db_session,
        email="staff@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )

    staff_session = await _login(http, staff.email)
    suspended = await http.post(
        f"{API}/platform/tenants/{tenant.id}/suspend",
        json={"reason": "abuse report under investigation"},
        headers=_bearer(staff_session),
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["status"] == TenantStatus.SUSPENDED.value
    assert suspended.json()["is_active"] is False

    # The member's own access stops on the next request.
    owner_session = await _login(http, owner.email)
    blocked = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": "under-review"},
        headers=_bearer(owner_session),
    )
    assert blocked.status_code == 403, blocked.text

    restored = await http.post(
        f"{API}/platform/tenants/{tenant.id}/restore",
        headers=_bearer(staff_session),
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == TenantStatus.ACTIVE.value

    headers = await _select_workspace(http, owner_session, "under-review")
    assert (await http.get(f"{API}/workspace/members", headers=headers)).status_code == 200

    assert len(await _audit(db_session, AuditAction.WORKSPACE_SUSPENDED)) == 1
    assert len(await _audit(db_session, AuditAction.WORKSPACE_RESTORED)) == 1


async def test_a_suspended_workspace_refuses_business_operations(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Enforcement is centralised, so a route cannot forget it.

    `get_active_workspace` reads `Tenant.is_active` on every request, which is
    why suspension needs no per-route change and why a token minted *before* the
    suspension stops working at once rather than at expiry.
    """
    owner = await _user(db_session, email="mid-session@example.com")
    tenant = await _workspace(db_session, slug="mid-session-co", owner=owner)
    session = await _login(http, owner.email)
    headers = await _select_workspace(http, session, "mid-session-co")
    assert (await http.get(f"{API}/workspace/members", headers=headers)).status_code == 200

    tenant.status = TenantStatus.SUSPENDED
    await db_session.flush()

    for path in ("/workspace/members", "/workspace", "/leads", "/agents"):
        response = await http.get(f"{API}{path}", headers=headers)
        assert response.status_code == 403, f"{path}: {response.text}"


async def test_a_workspace_member_cannot_suspend_their_own_workspace(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Platform authority is a property of the account, never of a membership.

    Owning a workspace grants nothing on the platform router - which is what
    stops an owner from suspending a competitor's workspace as readily as their
    own, since neither is theirs to suspend.
    """
    owner = await _user(db_session, email="not-staff@example.com")
    tenant = await _workspace(db_session, slug="self-suspend", owner=owner)
    session = await _login(http, owner.email)

    response = await http.post(
        f"{API}/platform/tenants/{tenant.id}/suspend",
        json={},
        headers=_bearer(session),
    )

    assert response.status_code == 403, response.text


async def test_an_owner_cannot_suspend_another_tenants_workspace(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Tenant isolation across the platform surface, stated as its own test."""
    attacker = await _user(db_session, email="attacker@example.com")
    await _workspace(db_session, slug="attacker-co", owner=attacker)
    victim = await _user(db_session, email="victim@example.com")
    target = await _workspace(db_session, slug="victim-co", owner=victim)

    session = await _login(http, attacker.email)
    response = await http.post(
        f"{API}/platform/tenants/{target.id}/suspend",
        json={},
        headers=_bearer(session),
    )

    assert response.status_code == 403, response.text
    await db_session.refresh(target)
    assert target.status is TenantStatus.ACTIVE


async def test_a_deleted_workspace_cannot_be_restored_by_platform_staff(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Undoing a customer's decision to close their business is not a button.

    Suspension is the reversible state; deletion is the customer's. Reversing
    one made in error is an operator action against the database, with the
    deliberation that implies.
    """
    owner = await _user(db_session, email="gone@example.com")
    tenant = await _workspace(db_session, slug="gone-co", owner=owner)
    tenant.deleted_at = datetime.now(UTC)
    await db_session.flush()
    staff = await _user(
        db_session,
        email="staff-r@example.com",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )

    session = await _login(http, staff.email)
    restored = await http.post(
        f"{API}/platform/tenants/{tenant.id}/restore",
        headers=_bearer(session),
    )
    suspended = await http.post(
        f"{API}/platform/tenants/{tenant.id}/suspend",
        json={},
        headers=_bearer(session),
    )

    assert restored.status_code == 409, restored.text
    assert restored.json()["error"]["code"] == "workspace_deleted"
    assert suspended.status_code == 409, suspended.text


async def test_suspending_an_unknown_workspace_is_not_found(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff = await _user(
        db_session,
        email="staff-n@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    session = await _login(http, staff.email)

    response = await http.post(
        f"{API}/platform/tenants/{uuid.uuid4()}/suspend",
        json={},
        headers=_bearer(session),
    )

    assert response.status_code == 404, response.text


async def test_restoring_leaves_a_removed_member_removed(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Restoration is not a general-purpose undo.

    A lifecycle operation that quietly gave access back to somebody a customer
    had deliberately removed would be a security defect wearing the shape of a
    convenience, so it is asserted rather than assumed.
    """
    owner = await _user(db_session, email="owner-rr@example.com")
    tenant = await _workspace(db_session, slug="restore-scope", owner=owner)
    removed = await _user(db_session, email="removed-before@example.com")
    membership = await _join(db_session, tenant=tenant, user=removed, role=TenantRole.MEMBER)
    membership.status = MembershipStatus.REVOKED
    membership.revoked_at = datetime.now(UTC)
    tenant.status = TenantStatus.SUSPENDED
    await db_session.flush()

    staff = await _user(
        db_session,
        email="staff-s@example.com",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    session = await _login(http, staff.email)
    restored = await http.post(
        f"{API}/platform/tenants/{tenant.id}/restore",
        headers=_bearer(session),
    )
    assert restored.status_code == 200, restored.text

    await db_session.refresh(membership)
    assert membership.status is MembershipStatus.REVOKED

    their_session = await _login(http, removed.email)
    blocked = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": "restore-scope"},
        headers=_bearer(their_session),
    )
    assert blocked.status_code == 404, blocked.text
