"""Closing your own account, and everything that must not happen when you do.

`DELETE /auth/me` is the most irreversible request in the product, so the tests
here are mostly about refusals and about blast radius.

**Refusals.** It needs the current password, and an account that has none is
turned away rather than waved through - a "recent authentication" check in its
place would assert something already true, since an access token is at most
fifteen minutes old by construction. And it is refused entirely while the caller
is the last owner of a live workspace, because deleting the account anyway would
leave that workspace with nobody able to invite an owner or close it.

**Blast radius.** Afterwards the identity must be unreachable by every route
that could open a session - password login, refresh, the access token already in
the caller's hand, and Google - while every *other* account, workspace and
session in the system is untouched. Half the file is that second sentence.

The suite runs against real rows and real signed tokens, driven over HTTP.
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
    Tenant,
    TenantRole,
    User,
)
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.enums import TenantStatus
from app.db.models.identity import FederatedIdentity, IdentityProvider
from app.main import create_app
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"


class _Redis:
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


async def _user(
    session: AsyncSession,
    *,
    email: str,
    password: str | None = PASSWORD,
) -> User:
    user = User(
        email=email,
        full_name=email.split("@")[0].title(),
        hashed_password=hash_password(password) if password is not None else None,
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    return user


async def _workspace(
    session: AsyncSession,
    *,
    slug: str,
    owner: User,
    role: TenantRole = TenantRole.TENANT_OWNER,
) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=owner.id, role=role))
    await session.flush()
    return tenant


@pytest.fixture
def deletion_settings() -> Settings:
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
def app(deletion_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(deletion_settings)
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


async def _login(http: AsyncClient, email: str) -> dict[str, Any]:
    response = await http.post(
        f"{API}/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _bearer(session: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


async def _delete_self(
    http: AsyncClient,
    session: dict[str, Any],
    password: str = PASSWORD,
) -> Any:
    return await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"current_password": password},
        headers=_bearer(session),
    )


# ------------------------------------------------------------------ the happy path


async def test_closing_an_account_kills_every_way_back_in(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The whole point, in one test: four doors, all shut.

    The access token in the caller's hand, the refresh token they were issued,
    a fresh password login, and the row itself. Each is a separate mechanism -
    `token_version` on the access path, `token_version` and `deleted_at` on the
    refresh path, and a soft-delete-aware lookup on login - so all four are
    checked rather than one being taken as evidence for the rest.
    """
    user = await _user(db_session, email="closes-up@example.com")
    session = await _login(http, user.email)

    response = await _delete_self(http, session)
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False

    await db_session.refresh(user)
    assert user.deleted_at is not None
    assert user.is_active is False

    # The access token that made the call.
    assert (await http.get(f"{API}/auth/me", headers=_bearer(session))).status_code == 401
    # The refresh token issued alongside it.
    refreshed = await http.post(
        f"{API}/auth/refresh",
        json={"refresh_token": session["refresh_token"]},
    )
    assert refreshed.status_code == 401, refreshed.text
    # And a fresh sign-in with credentials that are still, technically, correct.
    again = await http.post(
        f"{API}/auth/login",
        json={"email": user.email, "password": PASSWORD},
    )
    assert again.status_code == 401, again.text


async def test_the_deletion_is_audited_as_the_person_s_own_act(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """`USER_DELETED` by a `USER` actor, which is what separates it from a ban.

    Both routes that tombstone an identity write the same action, and
    `actor_kind` is what tells "they closed their account" apart from "an
    administrator closed it". An investigation filters on exactly that, so it is
    asserted rather than assumed - and the label must exist in the PostgreSQL
    enum for the request to commit at all, which is AUTH-01's regression in this
    file's terms.
    """
    user = await _user(db_session, email="audited-exit@example.com")
    session = await _login(http, user.email)

    assert (await _delete_self(http, session)).status_code == 200

    rows = await db_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.USER_DELETED)
    )
    entries = list(rows.scalars())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_kind is AuditActorKind.USER
    assert entry.actor_id == user.id
    # Copied at write time, so the trail stays readable now the account is gone.
    assert entry.actor_label == user.email
    assert entry.meta is not None
    assert entry.meta["self_service"] is True
    # And nothing resembling a credential went anywhere near it.
    assert "password" not in str(entry.meta).lower()


async def test_memberships_are_withdrawn_rather_than_deleted(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A revoked row keeps the answer to "when did they leave"; a deleted one does not.

    It also matters for the unique constraint on `(user_id, tenant_id)`: a
    deleted row would make a later re-invitation indistinguishable from a first
    one, which is ADR-038's argument applied to account closure.
    """
    departing = await _user(db_session, email="departs@example.com")
    owner = await _user(db_session, email="stays-behind@example.com")
    tenant = await _workspace(db_session, slug="carries-on", owner=owner)
    membership = Membership(
        tenant_id=tenant.id,
        user_id=departing.id,
        role=TenantRole.MEMBER,
    )
    db_session.add(membership)
    await db_session.flush()

    session = await _login(http, departing.email)
    assert (await _delete_self(http, session)).status_code == 200

    await db_session.refresh(membership)
    assert membership.status is MembershipStatus.REVOKED
    assert membership.revoked_at is not None
    # Themselves. Nobody threw them out, and the trail should not suggest one did.
    assert membership.revoked_by_id == departing.id


# ----------------------------------------------------------------- the refusals


async def test_the_current_password_is_required(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A live access token is not enough, because a stolen one is a live token."""
    user = await _user(db_session, email="needs-proof@example.com")
    session = await _login(http, user.email)

    response = await _delete_self(http, session, password="not the password")

    assert response.status_code == 401, response.text
    await db_session.refresh(user)
    assert user.deleted_at is None


async def test_a_passwordless_account_is_told_to_set_one(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The Google-only case, refused rather than exempted.

    Setting a password bumps the token version and emails the address on the
    account, so an attacker holding only a stolen session cannot get through
    this door without the real owner being told. It is the same rule
    disconnecting Google already follows (ADR-057), and it is why there is no
    weaker branch here for accounts that cannot prove a password.

    Driven with a hand-minted session because a passwordless account is, by
    design, unable to use `/auth/login` at all.
    """
    from app.core.security import create_access_token

    user = await _user(db_session, email="google-only@example.com", password=None)
    token, _ = create_access_token(
        settings=Settings(
            _env_file=None,
            environment="test",
            cors_origins=[],
            rate_limit_enabled=False,
        ),
        subject=user.id,
        tenant_id=None,
        token_version=user.token_version,
    )

    response = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"current_password": "anything at all"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "password_required"
    await db_session.refresh(user)
    assert user.deleted_at is None


async def test_the_last_owner_of_a_workspace_cannot_close_their_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The refusal carries the workspaces, because the caller has to act on them.

    A bare 409 would tell somebody they cannot leave without telling them what
    to do about it. The payload names each workspace so a client can offer the
    two real options: transfer ownership, or delete the workspace.
    """
    owner = await _user(db_session, email="sole-owner@example.com")
    first = await _workspace(db_session, slug="alpha-co", owner=owner)
    second = await _workspace(db_session, slug="beta-co", owner=owner)

    session = await _login(http, owner.email)
    response = await _delete_self(http, session)

    assert response.status_code == 409, response.text
    body = response.json()["error"]
    assert body["code"] == "account_owns_workspaces"
    slugs = {row["slug"] for row in body["details"]["workspaces"]}
    assert slugs == {"alpha-co", "beta-co"}
    assert {row["id"] for row in body["details"]["workspaces"]} == {
        str(first.id),
        str(second.id),
    }

    await db_session.refresh(owner)
    assert owner.deleted_at is None


async def test_the_refusal_names_only_workspaces_the_caller_belongs_to(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A conflict payload is still a response, and must disclose nothing extra."""
    owner = await _user(db_session, email="owner-scope@example.com")
    await _workspace(db_session, slug="mine-only", owner=owner)
    stranger = await _user(db_session, email="unrelated@example.com")
    await _workspace(db_session, slug="none-of-yours", owner=stranger)

    session = await _login(http, owner.email)
    response = await _delete_self(http, session)

    assert response.status_code == 409, response.text
    slugs = {row["slug"] for row in response.json()["error"]["details"]["workspaces"]}
    assert slugs == {"mine-only"}


async def test_an_owner_may_close_their_account_once_somebody_else_owns_it(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The way out the refusal points at, proven to actually work.

    Transfer ownership, then close the account. Without this the conflict above
    would be a dead end wearing the shape of guidance.
    """
    leaving = await _user(db_session, email="hands-over-then-goes@example.com")
    tenant = await _workspace(db_session, slug="handover-co", owner=leaving)
    successor = await _user(db_session, email="new-owner@example.com")
    db_session.add(
        Membership(tenant_id=tenant.id, user_id=successor.id, role=TenantRole.MEMBER),
    )
    await db_session.flush()

    session = await _login(http, leaving.email)
    switched = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": "handover-co"},
        headers=_bearer(session),
    )
    assert switched.status_code == 200, switched.text
    transferred = await http.post(
        f"{API}/workspace/ownership",
        json={"user_id": str(successor.id)},
        headers={"Authorization": f"Bearer {switched.json()['access_token']}"},
    )
    assert transferred.status_code == 200, transferred.text

    closed = await _delete_self(http, session)
    assert closed.status_code == 200, closed.text

    # The workspace outlives its founder, with an owner who can still run it.
    await db_session.refresh(tenant)
    assert tenant.deleted_at is None
    assert (await _login(http, successor.email)) is not None


async def test_owning_an_already_deleted_workspace_does_not_block_closure(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Otherwise closing an account would be impossible for the people most likely
    to want it: somebody who has already shut their business down.
    """
    owner = await _user(db_session, email="wound-up@example.com")
    tenant = await _workspace(db_session, slug="wound-up-co", owner=owner)
    tenant.deleted_at = datetime.now(UTC)
    await db_session.flush()

    session = await _login(http, owner.email)
    response = await _delete_self(http, session)

    assert response.status_code == 200, response.text


async def test_a_second_deletion_attempt_is_refused_rather_than_repeated(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Predictable, and with no new side effect.

    The account is already gone, so authentication refuses before the route is
    reached. What matters is that it is a clean 401 rather than a 500 from a
    second tombstone being written over the first.
    """
    user = await _user(db_session, email="twice@example.com")
    session = await _login(http, user.email)
    assert (await _delete_self(http, session)).status_code == 200
    first_version = user.token_version

    second = await _delete_self(http, session)

    assert second.status_code == 401, second.text
    await db_session.refresh(user)
    assert user.token_version == first_version


# ------------------------------------------------------------- blast radius


async def test_google_can_no_longer_open_the_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The identity row survives, and that is what makes the refusal reliable.

    Deleting the federated identity would free the Google subject, and the next
    sign-in would try to create an account on an address the tombstone still
    holds - a uniqueness error surfacing as a 500. Keeping it means
    `GoogleAuthService._resume` finds the identity, sees `deleted_at`, and
    refuses. The identity also stays attached to the account it always belonged
    to, so nothing can migrate it to somebody else.
    """
    user = await _user(db_session, email="had-google@example.com")
    identity = FederatedIdentity(
        user_id=user.id,
        provider=IdentityProvider.GOOGLE,
        # The stable subject Google issues, which is what an identity is keyed
        # by - never the email address (docs/GOOGLE_OAUTH.md).
        provider_subject="google-subject-for-deleted-account",
    )
    db_session.add(identity)
    await db_session.flush()

    session = await _login(http, user.email)
    assert (await _delete_self(http, session)).status_code == 200

    await db_session.refresh(identity)
    assert identity.user_id == user.id
    surviving = await db_session.scalar(
        select(FederatedIdentity).where(
            FederatedIdentity.provider_subject == "google-subject-for-deleted-account"
        ),
    )
    assert surviving is not None
    await db_session.refresh(user)
    assert user.deleted_at is not None


async def test_the_address_is_not_released_for_re_registration(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The tombstone keeps the address, deliberately.

    Releasing it would let a stranger register the email a closed account used,
    and inherit whatever a colleague's memory, an old invitation or a support
    ticket still associates with it. Documented in docs/AUTH.md as a permanent
    reservation rather than left as a side effect nobody decided on.
    """
    user = await _user(db_session, email="reserved@example.com")
    session = await _login(http, user.email)
    assert (await _delete_self(http, session)).status_code == 200

    response = await http.post(
        f"{API}/auth/register",
        json={
            "email": "reserved@example.com",
            "password": PASSWORD,
            "workspace_name": "Second Attempt",
            "workspace_slug": "second-attempt",
        },
    )

    assert response.status_code == 409, response.text


async def test_closing_one_account_leaves_everybody_else_signed_in(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Blast radius, stated as a test.

    A colleague in the same workspace keeps their account, their session and
    their membership. Nothing about one person's decision reaches another's.
    """
    leaving = await _user(db_session, email="goes@example.com")
    owner = await _user(db_session, email="remains@example.com")
    tenant = await _workspace(db_session, slug="shared-desk", owner=owner)
    db_session.add(
        Membership(tenant_id=tenant.id, user_id=leaving.id, role=TenantRole.MEMBER),
    )
    await db_session.flush()

    colleague_session = await _login(http, owner.email)
    leaving_session = await _login(http, leaving.email)
    assert (await _delete_self(http, leaving_session)).status_code == 200

    # The colleague's *existing* token, minted before the deletion.
    still_working = await http.get(f"{API}/auth/me", headers=_bearer(colleague_session))
    assert still_working.status_code == 200, still_working.text
    await db_session.refresh(owner)
    assert owner.deleted_at is None
    assert owner.token_version == 1


async def test_a_workspace_owner_has_no_route_to_another_persons_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The boundary this whole design rests on, checked at the routes.

    A workspace owner may withdraw somebody's membership and may not touch their
    identity. `DELETE /auth/me` names no target, and `DELETE /platform/users/{id}`
    needs a platform role that owning a workspace does not confer - so there is
    no combination of the two that reaches a global account.
    """
    owner = await _user(db_session, email="powerful-owner@example.com")
    tenant = await _workspace(db_session, slug="power-co", owner=owner)
    member = await _user(db_session, email="ordinary-member@example.com")
    db_session.add(
        Membership(tenant_id=tenant.id, user_id=member.id, role=TenantRole.MEMBER),
    )
    await db_session.flush()

    session = await _login(http, owner.email)
    attempted = await http.delete(
        f"{API}/platform/users/{member.id}",
        headers=_bearer(session),
    )

    assert attempted.status_code == 403, attempted.text
    await db_session.refresh(member)
    assert member.deleted_at is None
    assert member.is_active is True

    # And the payload has nowhere to name somebody else either: an extra field
    # is refused outright rather than ignored.
    smuggled = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"current_password": PASSWORD, "user_id": str(member.id)},
        headers=_bearer(session),
    )
    assert smuggled.status_code == 422, smuggled.text
    await db_session.refresh(member)
    assert member.deleted_at is None


async def test_platform_deletion_withdraws_memberships_and_names_orphaned_workspaces(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The platform route does not refuse for owned workspaces, and says so.

    The asymmetry with `DELETE /auth/me` is deliberate. Self-deletion is
    somebody tidying up and there is always a way to hand a workspace over
    first, so refusing costs one step and saves a workspace. This route is how
    an abusive or compromised account is shut down *now* - a refusal here would
    mean the account most worth removing is the one that cannot be, since an
    attacker would only have to own a workspace to become undeletable.

    So the consequence is made visible rather than prevented: the workspace left
    ownerless is named in the audit entry for an operator to act on, and the
    memberships are withdrawn so a deleted person does not linger on rosters as
    somebody who still appears to have access.
    """
    from app.db.models import PlatformRole

    target = await _user(db_session, email="abusive@example.com")
    stranded = await _workspace(db_session, slug="left-ownerless", owner=target)
    # A second workspace where somebody else is also an owner: not orphaned, so
    # it must not appear in the entry.
    colleague = await _user(db_session, email="co-owner@example.com")
    shared = await _workspace(db_session, slug="co-owned", owner=colleague)
    membership = Membership(
        tenant_id=shared.id,
        user_id=target.id,
        role=TenantRole.TENANT_OWNER,
    )
    db_session.add(membership)
    await db_session.flush()

    staff = await _user(db_session, email="platform-staff@example.com")
    staff.platform_role = PlatformRole.PLATFORM_OWNER
    await db_session.flush()

    session = await _login(http, staff.email)
    response = await http.delete(
        f"{API}/platform/users/{target.id}",
        headers=_bearer(session),
    )
    assert response.status_code == 200, response.text

    rows = await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == AuditAction.USER_DELETED,
            AuditLog.target_id == target.id,
        )
    )
    entry = next(iter(rows.scalars()))
    assert entry.meta is not None
    assert entry.meta["self_service"] is False
    assert entry.meta["orphaned_workspaces"] == [stranded.slug]

    # Every membership withdrawn, including the one in the co-owned workspace.
    await db_session.refresh(membership)
    assert membership.status is MembershipStatus.REVOKED
    # And the co-owned workspace still has its other owner.
    surviving = await db_session.execute(
        select(Membership).where(
            Membership.tenant_id == shared.id,
            Membership.user_id == colleague.id,
        )
    )
    assert surviving.scalars().one().status is MembershipStatus.ACTIVE
