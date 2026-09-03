"""Authorization and tenant isolation against a real database.

Every other test in this suite uses fakes. These do not, because the claims
being checked here are claims about PostgreSQL: that a scoped query cannot
reach another workspace's rows, and that the uniqueness rules are enforced by
the database rather than merely intended.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    PermissionDeniedError,
    TenantIsolationError,
)
from app.core.security import hash_invitation_token, hash_password
from app.core.token_store import RefreshTokenStore
from app.db.models import (
    InvitationStatus,
    PlatformRole,
    TenantInvitation,
    TenantRole,
)
from app.db.models.tenant import Tenant
from app.repositories import (
    InvitationRepository,
    MembershipRepository,
    TenantRepository,
    UserMembershipRepository,
    UserRepository,
)
from app.services.auth_service import AuthService
from app.services.invitation_service import InvitationService
from tests.fakes import as_redis_client

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


class FakeCommands:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        # `nx` returns None without writing when the key is already there,
        # exactly as Redis does. `RefreshTokenStore.spend` reads that to tell a
        # first use from a replay.
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self.values else 0


class FakeRedis:
    """Revocation is Redis' job, and Redis is not what these tests are about."""

    def __init__(self) -> None:
        self.client = FakeCommands()


@pytest.fixture
def auth(db_session: AsyncSession, settings: Settings) -> AuthService:
    return AuthService(
        session=db_session,
        settings=settings,
        token_store=RefreshTokenStore(as_redis_client(FakeRedis())),
    )


@pytest.fixture
def invitations(db_session: AsyncSession) -> InvitationService:
    return InvitationService(session=db_session)


async def _tenant(db_session: AsyncSession, *, name: str, slug: str) -> Tenant:
    tenant = await TenantRepository(db_session).create(name=name, slug=slug)
    await db_session.flush()
    return tenant


async def _invitation(
    db_session: AsyncSession, *, tenant_id: uuid.UUID, email: str
) -> TenantInvitation:
    repository = InvitationRepository(db_session, tenant_id=tenant_id)
    invitation = await repository.create(
        email=email,
        role=TenantRole.MEMBER,
        token_hash=hash_invitation_token(uuid.uuid4().hex),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    await db_session.flush()
    return invitation


async def test_a_scoped_read_never_returns_another_workspaces_rows(
    db_session: AsyncSession,
) -> None:
    first = await _tenant(db_session, name="First", slug="first")
    second = await _tenant(db_session, name="Second", slug="second")
    await _invitation(db_session, tenant_id=first.id, email="a@example.com")
    await _invitation(db_session, tenant_id=second.id, email="b@example.com")

    visible = await InvitationRepository(db_session, tenant_id=first.id).list_pending()

    assert [entry.email for entry in visible] == ["a@example.com"]


async def test_reaching_for_another_workspaces_row_answers_not_found(
    db_session: AsyncSession,
) -> None:
    first = await _tenant(db_session, name="First", slug="first")
    second = await _tenant(db_session, name="Second", slug="second")
    theirs = await _invitation(db_session, tenant_id=second.id, email="b@example.com")

    repository = InvitationRepository(db_session, tenant_id=first.id)

    with pytest.raises(TenantIsolationError) as raised:
        await repository.require_by_id(theirs.id)

    # Indistinguishable from a row that does not exist anywhere.
    assert raised.value.status_code == 404


async def test_the_database_enforces_one_membership_per_user_and_workspace(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, name="First", slug="first")
    user = await UserRepository(db_session).create(
        email="owner@example.com",
        hashed_password=hash_password(PASSWORD),
    )
    await db_session.flush()

    memberships = MembershipRepository(db_session, tenant_id=tenant.id)
    await memberships.add_member(user_id=user.id, role=TenantRole.MEMBER)
    await db_session.flush()
    await memberships.add_member(user_id=user.id, role=TenantRole.TENANT_ADMIN)

    # The rule is a constraint, not a convention.
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_identity_can_belong_to_two_workspaces(db_session: AsyncSession) -> None:
    first = await _tenant(db_session, name="First", slug="first")
    second = await _tenant(db_session, name="Second", slug="second")
    user = await UserRepository(db_session).create(email="owner@example.com")
    await db_session.flush()

    for tenant, role in ((first, TenantRole.TENANT_OWNER), (second, TenantRole.MEMBER)):
        await MembershipRepository(db_session, tenant_id=tenant.id).add_member(
            user_id=user.id,
            role=role,
        )
    await db_session.flush()

    memberships = await UserMembershipRepository(db_session).list_for_user(user.id)

    assert {entry.tenant_id for entry in memberships} == {first.id, second.id}


async def test_a_platform_role_grants_nothing_inside_a_workspace(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, name="First", slug="first")
    staff = await UserRepository(db_session).create(email="staff@example.com")
    staff.platform_role = PlatformRole.PLATFORM_ADMIN
    await db_session.flush()

    memberships = MembershipRepository(db_session, tenant_id=tenant.id)

    # Platform authority is not workspace authority. Without a membership there
    # is nothing to find, and the answer says nothing about the workspace.
    with pytest.raises(TenantIsolationError):
        await memberships.require_for_user(staff.id)


async def test_registration_creates_an_owner_membership(
    auth: AuthService, db_session: AsyncSession
) -> None:
    session = await auth.register(
        email="Owner@Example.com",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="Acme",
    )

    assert session.workspace is not None
    assert session.workspace.membership.role is TenantRole.TENANT_OWNER
    # Both identifiers are normalised on the way in.
    assert session.user.email == "owner@example.com"
    assert session.workspace.tenant.slug == "acme"

    stored = await UserRepository(db_session).get_by_email("owner@example.com")
    assert stored is not None
    assert stored.hashed_password != PASSWORD


async def test_a_registered_account_can_log_in_and_a_wrong_password_cannot(
    auth: AuthService,
) -> None:
    await auth.register(
        email="owner@example.com",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="acme",
    )

    session = await auth.login(email="owner@example.com", password=PASSWORD)
    assert session.workspace is not None
    assert session.workspace.tenant.slug == "acme"

    with pytest.raises(AuthenticationError):
        await auth.login(email="owner@example.com", password="not the password")


async def test_a_second_registration_with_the_same_address_conflicts(auth: AuthService) -> None:
    await auth.register(
        email="owner@example.com",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="acme",
    )

    with pytest.raises(ConflictError):
        await auth.register(
            email="owner@example.com",
            password=PASSWORD,
            workspace_name="Other",
            workspace_slug="other",
        )


async def test_a_refresh_token_works_once_and_then_never_again(auth: AuthService) -> None:
    registered = await auth.register(
        email="owner@example.com",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="acme",
    )

    rotated = await auth.refresh(refresh_token=registered.refresh_token)
    assert rotated.refresh_token != registered.refresh_token

    # Replaying the spent token is exactly what a thief would do.
    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=registered.refresh_token)


async def test_a_workspace_the_caller_does_not_belong_to_looks_missing(
    auth: AuthService, db_session: AsyncSession
) -> None:
    await auth.register(
        email="owner@example.com",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="acme",
    )
    await _tenant(db_session, name="Somebody Else", slug="somebody-else")

    with pytest.raises(TenantIsolationError):
        await auth.login(
            email="owner@example.com",
            password=PASSWORD,
            workspace_slug="somebody-else",
        )


async def test_a_disabled_account_cannot_log_in(
    auth: AuthService, db_session: AsyncSession
) -> None:
    await auth.register(
        email="owner@example.com",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="acme",
    )
    user = await UserRepository(db_session).get_by_email("owner@example.com")
    assert user is not None
    user.is_active = False
    await db_session.flush()

    with pytest.raises(PermissionDeniedError):
        await auth.login(email="owner@example.com", password=PASSWORD)


async def test_an_invitation_grants_membership_exactly_once(
    auth: AuthService, invitations: InvitationService, db_session: AsyncSession
) -> None:
    owner_session = await auth.register(
        email="owner@example.com",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="acme",
    )
    assert owner_session.workspace is not None
    tenant = owner_session.workspace.tenant

    invitation, raw_token = await invitations.issue(
        tenant_id=tenant.id,
        inviter=owner_session.user,
        inviter_role=TenantRole.TENANT_OWNER,
        email="invited@example.com",
        role=TenantRole.MEMBER,
    )

    # Only the hash is stored, so the database is useless to a thief.
    assert invitation.token_hash != raw_token
    assert len(invitation.token_hash) == 64

    accepted = await invitations.accept(raw_token=raw_token, password=PASSWORD)

    assert accepted.membership.role is TenantRole.MEMBER
    assert accepted.tenant.id == tenant.id
    assert invitation.status is InvitationStatus.ACCEPTED

    # A used invitation is spent, and says nothing about why it failed.
    with pytest.raises(AuthenticationError):
        await invitations.accept(raw_token=raw_token, password=PASSWORD)


async def test_an_invited_member_lands_in_the_inviting_workspace_only(
    auth: AuthService, invitations: InvitationService
) -> None:
    owner_session = await auth.register(
        email="owner@example.com",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="acme",
    )
    outsider = await auth.register(
        email="outsider@example.com",
        password=PASSWORD,
        workspace_name="Other",
        workspace_slug="other",
    )
    assert owner_session.workspace is not None
    assert outsider.workspace is not None

    _, raw_token = await invitations.issue(
        tenant_id=owner_session.workspace.tenant.id,
        inviter=owner_session.user,
        inviter_role=TenantRole.TENANT_OWNER,
        email="invited@example.com",
        role=TenantRole.MEMBER,
    )
    await invitations.accept(raw_token=raw_token, password=PASSWORD)

    invited = await auth.login(email="invited@example.com", password=PASSWORD)

    assert invited.workspace is not None
    assert invited.workspace.tenant.slug == "acme"

    # The other workspace exists, but not for this account.
    with pytest.raises(TenantIsolationError):
        await auth.login(
            email="invited@example.com",
            password=PASSWORD,
            workspace_slug="other",
        )


async def test_an_administrator_cannot_invite_an_owner(
    auth: AuthService, invitations: InvitationService, db_session: AsyncSession
) -> None:
    owner_session = await auth.register(
        email="owner@example.com",
        password=PASSWORD,
        workspace_name="Acme",
        workspace_slug="acme",
    )
    assert owner_session.workspace is not None

    with pytest.raises(PermissionDeniedError):
        await invitations.issue(
            tenant_id=owner_session.workspace.tenant.id,
            inviter=owner_session.user,
            inviter_role=TenantRole.TENANT_ADMIN,
            email="peer@example.com",
            role=TenantRole.TENANT_OWNER,
        )


async def test_an_expired_invitation_is_refused(
    invitations: InvitationService, db_session: AsyncSession
) -> None:
    tenant = await _tenant(db_session, name="Acme", slug="acme")
    raw_token = uuid.uuid4().hex
    repository = InvitationRepository(db_session, tenant_id=tenant.id)
    await repository.create(
        email="invited@example.com",
        role=TenantRole.MEMBER,
        token_hash=hash_invitation_token(raw_token),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await db_session.flush()

    with pytest.raises(AuthenticationError):
        await invitations.accept(raw_token=raw_token, password=PASSWORD)
