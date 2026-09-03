"""Withdrawing somebody's access to a workspace, end to end.

Two things are proven here, and the second is the one that matters.

**The rules.** Who may remove whom, what happens to the last owner, what
readmission does. These are ordinary service tests.

**The enforcement.** A revoked member holds a *genuine, signed, unexpired*
access token naming the workspace they were removed from. Every workspace-scoped
route in the API is then called with it. Revocation that only removes a row from
a member list, while the token keeps working until it expires, is not
revocation - it is a fifteen-minute window in which somebody who was just fired
still has the inbox open.

The route list is not hand-written. It is walked out of FastAPI's dependency
graph, so a route added later is covered without anybody remembering to add it
here (ADR-038).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute, _IncludedRouter
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_active_workspace, get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.exceptions import ConflictError, PermissionDeniedError, ValidationError
from app.core.security import create_access_token
from app.db.models import (
    Membership,
    MembershipStatus,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
)
from app.db.models.audit import AuditAction, AuditLog
from app.main import create_app
from app.repositories import MembershipRepository, UserMembershipRepository
from app.services.membership_service import MembershipService
from tests.conftest import AllowingEntitlements, FakeDependency

pytestmark = pytest.mark.integration

API = "/api/v1"


# ------------------------------------------------------------------- fixtures


async def _person(session: AsyncSession, email: str) -> User:
    user = User(email=email, hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()
    return user


async def _workspace(session: AsyncSession, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    return tenant


async def _member(
    session: AsyncSession,
    tenant: Tenant,
    user: User,
    role: TenantRole,
) -> Membership:
    membership = Membership(
        tenant_id=tenant.id,
        user_id=user.id,
        role=role,
        status=MembershipStatus.ACTIVE,
    )
    session.add(membership)
    await session.flush()
    return membership


def _service(session: AsyncSession, tenant: Tenant) -> MembershipService:
    return MembershipService(session=session, tenant_id=tenant.id)


# ------------------------------------------------------------------- the rules


async def test_an_admin_can_remove_a_member(db_session: AsyncSession) -> None:
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    admin = await _person(db_session, "admin@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, admin, TenantRole.TENANT_ADMIN)
    await _member(db_session, tenant, member, TenantRole.MEMBER)

    revoked = await _service(db_session, tenant).revoke(
        actor=admin,
        actor_role=TenantRole.TENANT_ADMIN,
        user_id=member.id,
    )

    assert revoked.status is MembershipStatus.REVOKED
    assert revoked.revoked_by_id == admin.id
    assert revoked.revoked_at is not None


async def test_a_member_cannot_remove_a_colleague(db_session: AsyncSession) -> None:
    """Otherwise the role boundary is decorative."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    one = await _person(db_session, "one@acme.test")
    two = await _person(db_session, "two@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, one, TenantRole.MEMBER)
    await _member(db_session, tenant, two, TenantRole.MEMBER)

    with pytest.raises(PermissionDeniedError):
        await _service(db_session, tenant).revoke(
            actor=one,
            actor_role=TenantRole.MEMBER,
            user_id=two.id,
        )


async def test_a_member_can_leave_without_permission(db_session: AsyncSession) -> None:
    """Leaving is not an administrative act."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, member, TenantRole.MEMBER)

    revoked = await _service(db_session, tenant).revoke(
        actor=member,
        actor_role=TenantRole.MEMBER,
        user_id=member.id,
    )

    assert revoked.status is MembershipStatus.REVOKED


async def test_an_admin_cannot_remove_an_owner(db_session: AsyncSession) -> None:
    """Otherwise an administrator promotes themselves by subtraction."""
    tenant = await _workspace(db_session, "acme")
    first_owner = await _person(db_session, "owner@acme.test")
    second_owner = await _person(db_session, "owner2@acme.test")
    admin = await _person(db_session, "admin@acme.test")
    await _member(db_session, tenant, first_owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, second_owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, admin, TenantRole.TENANT_ADMIN)

    with pytest.raises(PermissionDeniedError):
        await _service(db_session, tenant).revoke(
            actor=admin,
            actor_role=TenantRole.TENANT_ADMIN,
            user_id=first_owner.id,
        )


async def test_an_owner_can_remove_another_owner(db_session: AsyncSession) -> None:
    tenant = await _workspace(db_session, "acme")
    first = await _person(db_session, "owner@acme.test")
    second = await _person(db_session, "owner2@acme.test")
    await _member(db_session, tenant, first, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, second, TenantRole.TENANT_OWNER)

    revoked = await _service(db_session, tenant).revoke(
        actor=first,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=second.id,
    )

    assert revoked.status is MembershipStatus.REVOKED


async def test_the_last_owner_cannot_be_removed(db_session: AsyncSession) -> None:
    """A workspace with no owner has nobody who can invite one. It is not
    recoverable from inside, and the person doing it rarely means to."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    admin = await _person(db_session, "admin@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, admin, TenantRole.TENANT_ADMIN)

    with pytest.raises(ConflictError):
        await _service(db_session, tenant).revoke(
            actor=owner,
            actor_role=TenantRole.TENANT_OWNER,
            user_id=owner.id,
        )


async def test_the_last_owner_cannot_leave_either(db_session: AsyncSession) -> None:
    """Self-removal is not a way around the rule above."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)

    with pytest.raises(ConflictError):
        await _service(db_session, tenant).revoke(
            actor=owner,
            actor_role=TenantRole.TENANT_OWNER,
            user_id=owner.id,
        )


async def test_a_revoked_owner_no_longer_counts_towards_the_last_owner_rule(
    db_session: AsyncSession,
) -> None:
    """The count reads active owners. A revoked one propping the rule up would
    make the workspace unrecoverable in the opposite direction."""
    tenant = await _workspace(db_session, "acme")
    first = await _person(db_session, "owner@acme.test")
    second = await _person(db_session, "owner2@acme.test")
    await _member(db_session, tenant, first, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, second, TenantRole.TENANT_OWNER)
    service = _service(db_session, tenant)

    await service.revoke(
        actor=first,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=second.id,
    )

    with pytest.raises(ConflictError):
        await service.revoke(
            actor=first,
            actor_role=TenantRole.TENANT_OWNER,
            user_id=first.id,
        )


async def test_removing_somebody_already_removed_is_a_conflict(db_session: AsyncSession) -> None:
    """The caller is looking at a stale roster and should see it refreshed."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, member, TenantRole.MEMBER)
    service = _service(db_session, tenant)
    await service.revoke(actor=owner, actor_role=TenantRole.TENANT_OWNER, user_id=member.id)

    with pytest.raises(ConflictError):
        await service.revoke(actor=owner, actor_role=TenantRole.TENANT_OWNER, user_id=member.id)


async def test_somebody_from_another_workspace_cannot_be_removed_from_this_one(
    db_session: AsyncSession,
) -> None:
    """The service is workspace-scoped, so a stranger's id is simply not a
    member here - and the answer says nothing about whether they exist."""
    acme = await _workspace(db_session, "acme")
    other = await _workspace(db_session, "other")
    owner = await _person(db_session, "owner@acme.test")
    stranger = await _person(db_session, "stranger@other.test")
    await _member(db_session, acme, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, other, stranger, TenantRole.TENANT_OWNER)

    with pytest.raises(ValidationError):
        await _service(db_session, acme).revoke(
            actor=owner,
            actor_role=TenantRole.TENANT_OWNER,
            user_id=stranger.id,
        )

    # And the stranger still holds their own workspace.
    theirs = await MembershipRepository(db_session, tenant_id=other.id).get_for_user(stranger.id)
    assert theirs is not None
    assert theirs.is_active


# --------------------------------------------------------------- readmission


async def test_a_removed_member_can_be_reinstated(db_session: AsyncSession) -> None:
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, member, TenantRole.MEMBER)
    service = _service(db_session, tenant)
    await service.revoke(actor=owner, actor_role=TenantRole.TENANT_OWNER, user_id=member.id)

    restored = await service.reinstate(
        actor=owner,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=member.id,
        role=TenantRole.TENANT_ADMIN,
    )

    assert restored.status is MembershipStatus.ACTIVE
    assert restored.role is TenantRole.TENANT_ADMIN
    # The removal is cleared from the row, but the audit trail keeps it.
    assert restored.revoked_at is None
    assert restored.revoked_by_id is None


async def test_reinstating_reuses_the_existing_row(db_session: AsyncSession) -> None:
    """`UNIQUE(user_id, tenant_id)` requires it, and a second row would give
    authorization two grants to rank."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    original = await _member(db_session, tenant, member, TenantRole.MEMBER)
    service = _service(db_session, tenant)
    await service.revoke(actor=owner, actor_role=TenantRole.TENANT_OWNER, user_id=member.id)

    restored = await service.reinstate(
        actor=owner,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=member.id,
        role=TenantRole.MEMBER,
    )

    assert restored.id == original.id
    everyone = await MembershipRepository(db_session, tenant_id=tenant.id).list_members(
        include_revoked=True
    )
    assert len(everyone) == 2


async def test_an_admin_cannot_reinstate_somebody_as_an_owner(db_session: AsyncSession) -> None:
    """Matches the invitation path exactly: an administrator who could do this
    could mint themselves a peer with authority they do not have."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    admin = await _person(db_session, "admin@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, admin, TenantRole.TENANT_ADMIN)
    await _member(db_session, tenant, member, TenantRole.MEMBER)
    service = _service(db_session, tenant)
    await service.revoke(actor=admin, actor_role=TenantRole.TENANT_ADMIN, user_id=member.id)

    with pytest.raises(PermissionDeniedError):
        await service.reinstate(
            actor=admin,
            actor_role=TenantRole.TENANT_ADMIN,
            user_id=member.id,
            role=TenantRole.TENANT_OWNER,
        )


async def test_a_member_cannot_reinstate_anybody(db_session: AsyncSession) -> None:
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    one = await _person(db_session, "one@acme.test")
    two = await _person(db_session, "two@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, one, TenantRole.MEMBER)
    await _member(db_session, tenant, two, TenantRole.MEMBER)
    service = _service(db_session, tenant)
    await service.revoke(actor=owner, actor_role=TenantRole.TENANT_OWNER, user_id=two.id)

    with pytest.raises(PermissionDeniedError):
        await service.reinstate(
            actor=one,
            actor_role=TenantRole.MEMBER,
            user_id=two.id,
            role=TenantRole.MEMBER,
        )


# ------------------------------------------------------------- what it records


async def test_a_removal_is_audited_as_a_removal_and_a_departure_as_a_departure(
    db_session: AsyncSession,
) -> None:
    """ "Who threw them out" and "they walked" are different answers."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    removed = await _person(db_session, "removed@acme.test")
    leaver = await _person(db_session, "leaver@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, removed, TenantRole.MEMBER)
    await _member(db_session, tenant, leaver, TenantRole.MEMBER)
    service = _service(db_session, tenant)

    await service.revoke(actor=owner, actor_role=TenantRole.TENANT_OWNER, user_id=removed.id)
    await service.revoke(actor=leaver, actor_role=TenantRole.MEMBER, user_id=leaver.id)
    await db_session.flush()

    from sqlalchemy import select

    entries = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant.id)
                .order_by(AuditLog.occurred_at)
            )
        )
        .scalars()
        .all()
    )
    actions = [entry.action for entry in entries]
    assert AuditAction.MEMBER_REMOVED in actions
    assert AuditAction.MEMBER_LEFT in actions
    # The address a person reading the trail would recognise, not a UUID.
    labels = {entry.target_label for entry in entries}
    assert "removed@acme.test" in labels


# ------------------------------------------------------- what it stops working


async def test_the_workspace_switcher_stops_offering_a_revoked_workspace(
    db_session: AsyncSession,
) -> None:
    """It has to disappear at the same moment it stops answering, or somebody
    keeps a dead entry in their sidebar and reads the 404 as a bug."""
    acme = await _workspace(db_session, "acme")
    other = await _workspace(db_session, "other")
    owner = await _person(db_session, "owner@acme.test")
    person = await _person(db_session, "person@example.test")
    await _member(db_session, acme, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, acme, person, TenantRole.MEMBER)
    await _member(db_session, other, person, TenantRole.TENANT_OWNER)

    await _service(db_session, acme).revoke(
        actor=owner,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=person.id,
    )

    open_to_them = await UserMembershipRepository(db_session).list_for_user(person.id)
    assert [membership.tenant_id for membership in open_to_them] == [other.id]


# ------------------------------------------------- the whole API, over HTTP


@pytest.fixture
def revocation_app(
    settings: Settings,
    db_session: AsyncSession,
    fake_redis: FakeDependency,
) -> Iterator[FastAPI]:
    """The real application on the test's transaction.

    Only entitlements are faked, so a plan limit can never be mistaken for a
    revocation working.
    """
    application = create_app(settings)
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = fake_redis

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(revocation_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=revocation_app),
        base_url="http://wasla.test",
    ) as client:
        yield client


def _workspace_scoped_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every route whose dependency tree resolves the workspace guard.

    Walked rather than listed, so a route added next month is covered without
    anybody remembering this file exists.
    """

    def effective(routes: Sequence[Any]) -> Iterator[Any]:
        for route in routes:
            if isinstance(route, _IncludedRouter):
                candidates = list(route.effective_candidates())
                candidates += list(route.effective_low_priority_routes())
                for candidate in candidates:
                    if isinstance(candidate, _IncludedRouter):
                        yield from effective([candidate])
                    else:
                        yield candidate
            elif isinstance(route, APIRoute):
                yield route

    def flatten(dependant: Any, acc: list[Any] | None = None) -> list[Any]:
        acc = [] if acc is None else acc
        assert acc is not None
        acc.append(dependant)
        for sub in dependant.dependencies:
            flatten(sub, acc)
        found = acc
        assert found is not None
        return found

    app.openapi()
    found: list[tuple[str, str]] = []
    for route in effective(app.routes):
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        if get_active_workspace not in [d.call for d in flatten(dependant)]:
            continue
        path = getattr(route, "path", "") or getattr(route, "path_format", "")
        for method in sorted(set(route.methods or set()) - {"HEAD", "OPTIONS"}):
            found.append((method, path))
    return sorted(set(found))


async def test_a_revoked_member_is_refused_by_every_workspace_route(
    db_session: AsyncSession,
    settings: Settings,
    revocation_app: FastAPI,
    http: AsyncClient,
) -> None:
    """The test this whole feature exists for.

    The token is real: signed by the application, unexpired, carrying the right
    subject, the right workspace and the current token version. Nothing about it
    is invalid. The *only* thing that changed is a status column - and that has
    to be enough, across every route, or revocation is a fifteen-minute grace
    period for somebody who was just removed.
    """
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, member, TenantRole.MEMBER)
    token, _ = create_access_token(
        settings=settings,
        subject=member.id,
        tenant_id=tenant.id,
        token_version=member.token_version,
    )
    headers = {"Authorization": f"Bearer {token}"}

    routes = _workspace_scoped_routes(revocation_app)
    # A control: the same token works before the removal.
    before = await http.get(f"{API}/conversations", headers=headers)
    assert before.status_code == 200

    await _service(db_session, tenant).revoke(
        actor=owner,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=member.id,
    )
    await db_session.flush()

    assert len(routes) > 50, "the dependency walk found suspiciously few routes"
    survivors = []
    for method, path in routes:
        concrete = path
        for placeholder in ("{account_id}", "{agent_id}", "{campaign_id}", "{contact_id}"):
            concrete = concrete.replace(placeholder, str(uuid.uuid4()))
        for placeholder in ("{conversation_id}", "{document_id}", "{follow_up_id}"):
            concrete = concrete.replace(placeholder, str(uuid.uuid4()))
        for placeholder in ("{invitation_id}", "{invoice_id}", "{knowledge_base_id}"):
            concrete = concrete.replace(placeholder, str(uuid.uuid4()))
        for placeholder in ("{lead_id}", "{media_id}", "{template_id}", "{user_id}"):
            concrete = concrete.replace(placeholder, str(uuid.uuid4()))
        concrete = concrete.replace("{name}", "search_knowledge")

        response = await http.request(method, concrete, headers=headers, json={})
        # 403 is the workspace guard refusing; 404 is the tenant-scoped lookup
        # missing. Anything in the 2xx range means the route served a person who
        # is no longer a member.
        if response.status_code < 400:
            survivors.append((method, concrete, response.status_code))

    assert survivors == []


async def test_revocation_does_not_disturb_the_persons_other_workspaces(
    db_session: AsyncSession,
    settings: Settings,
    http: AsyncClient,
) -> None:
    """Being removed from one company is not a reason to be signed out of
    another. This is why revocation is a membership status rather than a bump
    of `users.token_version` - a bump would end every session everywhere."""
    acme = await _workspace(db_session, "acme")
    other = await _workspace(db_session, "other")
    owner = await _person(db_session, "owner@acme.test")
    person = await _person(db_session, "person@example.test")
    await _member(db_session, acme, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, acme, person, TenantRole.MEMBER)
    await _member(db_session, other, person, TenantRole.TENANT_OWNER)
    elsewhere, _ = create_access_token(
        settings=settings,
        subject=person.id,
        tenant_id=other.id,
        token_version=person.token_version,
    )

    await _service(db_session, acme).revoke(
        actor=owner,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=person.id,
    )
    await db_session.flush()

    response = await http.get(
        f"{API}/conversations",
        headers={"Authorization": f"Bearer {elsewhere}"},
    )

    assert response.status_code == 200
    # And the account itself is untouched.
    assert person.token_version == 1


async def test_a_revoked_member_can_be_invited_back(db_session: AsyncSession) -> None:
    """`issue` reads active memberships, so a removed person is invitable; and
    `accept` reactivates the row rather than inserting a second one."""
    from app.services.invitation_service import InvitationService

    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    original = await _member(db_session, tenant, member, TenantRole.MEMBER)
    await _service(db_session, tenant).revoke(
        actor=owner,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=member.id,
    )

    invitations = InvitationService(session=db_session)
    _, raw_token = await invitations.issue(
        tenant_id=tenant.id,
        inviter=owner,
        inviter_role=TenantRole.TENANT_OWNER,
        email=member.email,
        role=TenantRole.TENANT_ADMIN,
    )
    accepted = await invitations.accept(raw_token=raw_token)

    assert accepted.membership.id == original.id
    assert accepted.membership.is_active
    # The invitation's role wins: whoever issued it decided what they return as.
    assert accepted.membership.role is TenantRole.TENANT_ADMIN


async def test_an_active_member_still_cannot_be_invited_twice(db_session: AsyncSession) -> None:
    """The readmission path must not have loosened the ordinary conflict."""
    from app.services.invitation_service import InvitationService

    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, member, TenantRole.MEMBER)

    with pytest.raises(ConflictError):
        await InvitationService(session=db_session).issue(
            tenant_id=tenant.id,
            inviter=owner,
            inviter_role=TenantRole.TENANT_OWNER,
            email=member.email,
            role=TenantRole.MEMBER,
        )


# --------------------------------------------------------------- the endpoints


async def _token(settings: Settings, user: User, tenant: Tenant) -> dict[str, str]:
    token, _ = create_access_token(
        settings=settings,
        subject=user.id,
        tenant_id=tenant.id,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def test_the_roster_excludes_removed_people_unless_asked(
    db_session: AsyncSession, settings: Settings, http: AsyncClient
) -> None:
    """A member list that silently counts former colleagues is how somebody
    ends up believing a removed person still has access."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, member, TenantRole.MEMBER)
    await _service(db_session, tenant).revoke(
        actor=owner,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=member.id,
    )
    await db_session.flush()
    headers = await _token(settings, owner, tenant)

    default = await http.get(f"{API}/workspace/members", headers=headers)
    everyone = await http.get(
        f"{API}/workspace/members",
        headers=headers,
        params={"include_revoked": "true"},
    )

    assert [row["email"] for row in default.json()["members"]] == ["owner@acme.test"]
    assert len(everyone.json()["members"]) == 2
    revoked = next(row for row in everyone.json()["members"] if row["email"] == "member@acme.test")
    assert revoked["status"] == "revoked"
    assert revoked["revoked_at"] is not None


async def test_removing_a_member_over_http(
    db_session: AsyncSession, settings: Settings, http: AsyncClient
) -> None:
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, member, TenantRole.MEMBER)

    response = await http.delete(
        f"{API}/workspace/members/{member.id}",
        headers=await _token(settings, owner, tenant),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "revoked"


async def test_a_member_removing_a_colleague_over_http_is_forbidden(
    db_session: AsyncSession, settings: Settings, http: AsyncClient
) -> None:
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    one = await _person(db_session, "one@acme.test")
    two = await _person(db_session, "two@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, one, TenantRole.MEMBER)
    await _member(db_session, tenant, two, TenantRole.MEMBER)

    response = await http.delete(
        f"{API}/workspace/members/{two.id}",
        headers=await _token(settings, one, tenant),
    )

    assert response.status_code == 403


async def test_a_member_can_leave_over_http(
    db_session: AsyncSession, settings: Settings, http: AsyncClient
) -> None:
    """The reason the route is not behind the admin guard: a dependency
    evaluated before the path parameter is bound cannot tell self-removal from
    removing somebody else."""
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    member = await _person(db_session, "member@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, member, TenantRole.MEMBER)

    response = await http.delete(
        f"{API}/workspace/members/{member.id}",
        headers=await _token(settings, member, tenant),
    )

    assert response.status_code == 200


async def test_a_member_of_another_workspace_cannot_be_removed_over_http(
    db_session: AsyncSession,
    settings: Settings,
    http: AsyncClient,
) -> None:
    acme = await _workspace(db_session, "acme")
    other = await _workspace(db_session, "other")
    owner = await _person(db_session, "owner@acme.test")
    stranger = await _person(db_session, "stranger@other.test")
    await _member(db_session, acme, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, other, stranger, TenantRole.TENANT_OWNER)

    response = await http.delete(
        f"{API}/workspace/members/{stranger.id}",
        headers=await _token(settings, owner, acme),
    )

    assert response.status_code == 422
    assert str(other.id) not in response.text


async def test_reinstating_over_http_requires_an_administrator(
    db_session: AsyncSession, settings: Settings, http: AsyncClient
) -> None:
    tenant = await _workspace(db_session, "acme")
    owner = await _person(db_session, "owner@acme.test")
    one = await _person(db_session, "one@acme.test")
    two = await _person(db_session, "two@acme.test")
    await _member(db_session, tenant, owner, TenantRole.TENANT_OWNER)
    await _member(db_session, tenant, one, TenantRole.MEMBER)
    await _member(db_session, tenant, two, TenantRole.MEMBER)
    await _service(db_session, tenant).revoke(
        actor=owner,
        actor_role=TenantRole.TENANT_OWNER,
        user_id=two.id,
    )
    await db_session.flush()

    refused = await http.post(
        f"{API}/workspace/members/{two.id}/reinstate",
        headers=await _token(settings, one, tenant),
        json={"role": "member"},
    )
    allowed = await http.post(
        f"{API}/workspace/members/{two.id}/reinstate",
        headers=await _token(settings, owner, tenant),
        json={"role": "member"},
    )

    assert refused.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "active"
