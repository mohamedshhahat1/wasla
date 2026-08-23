"""Adversarial tests for session revocation and account lifecycle.

The threat these exist for, stated once: **a refresh token leaked, and nothing
could kill it.** Rotation only spends the copy that is presented, which is the
victim's rather than the thief's; `users.is_active` was checked on every request
but written by exactly one line (`is_active=True` at creation), so the check
guarded a column no code path could change; and the only remaining lever was
rotating `JWT_SECRET`, which signs out every user of every tenant at once.

Every test below drives the real application over HTTP against real rows, with
real signed tokens. Each one asserts that a credential which *was* valid stops
being valid, and each is paired with a control proving the mechanism did not
simply break authentication for everybody.

The token version is checked in two places and both are covered: on the access
token by `get_current_user`, riding the user row it already loads, and on the
refresh token by `AuthService.refresh`. The second is the one that matters most
- an access token lives fifteen minutes, a refresh token fourteen days.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import create_access_token, hash_password
from app.db.models import Membership, PlatformRole, Tenant, TenantRole, User
from app.db.models.enums import TenantStatus
from app.main import create_app
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a quite different passphrase"


class _Redis:
    """Enough Redis for the token denylist and the limiter.

    The real `RefreshTokenStore` and `RateLimiter` run against this, so rotation
    and revocation behave exactly as they do in production - only the storage is
    in memory.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, int] = {}

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        """`nx` behaves as Redis does: no write, and None, if the key exists.

        That return value is what makes `RefreshTokenStore.spend` able to
        detect a replay, so a fake that ignored `nx` would report every test
        green while the real detection was broken.
        """
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self.values else 0

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.expiries.get(key, -1)

    async def rpush(self, key: str, value: str) -> int:
        return 1


class _Infra:
    """Stands in for the database and Redis clients on application state."""

    def __init__(self) -> None:
        self.commands = _Redis()

    @property
    def client(self) -> _Redis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


# --------------------------------------------------------------------- set-up


async def _account(
    session: AsyncSession,
    *,
    email: str,
    slug: str,
    platform_role: PlatformRole | None = None,
) -> tuple[User, Tenant]:
    """One user, one workspace, one owner membership."""
    user = User(
        email=email,
        full_name=email.split("@")[0].title(),
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        platform_role=platform_role,
    )
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add_all([user, tenant])
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=TenantRole.TENANT_OWNER))
    await session.flush()
    return user, tenant


@pytest.fixture
def lifecycle_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
    )


@pytest.fixture
def app(
    lifecycle_settings: Settings,
    db_session: AsyncSession,
) -> Iterator[FastAPI]:
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


async def _login(http: AsyncClient, email: str, password: str = PASSWORD) -> dict:
    response = await http.post(
        f"{API}/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


async def _refresh(http: AsyncClient, token: str):
    return await http.post(f"{API}/auth/refresh", json={"refresh_token": token})


# ------------------------------------------------- the threat, closed


async def test_a_leaked_refresh_token_dies_when_the_user_revokes_sessions(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The headline case.

    The thief holds a refresh token and never touches the victim's session, so
    rotation - which only spends the token that is presented - never reaches it.
    Before `token_version` existed there was nothing else that could.
    """
    user, _ = await _account(db_session, email="victim@example.com", slug="victim")
    victim = await _login(http, "victim@example.com")
    stolen = victim["refresh_token"]

    # It works: this is a real credential, not a broken one.
    assert (await _refresh(http, stolen)).status_code == 200

    # The victim signs out everywhere from their own session.
    revoked = await http.post(f"{API}/auth/logout-all", headers=_bearer(victim))
    assert revoked.status_code == 200
    assert revoked.json()["token_version"] == 2

    # A fresh login is needed because logout-all ends the calling session too.
    assert (await _refresh(http, stolen)).status_code == 401
    again = await _login(http, "victim@example.com")
    assert (await _refresh(http, again["refresh_token"])).status_code == 200


async def test_the_calling_session_is_revoked_too(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Exempting the caller would leave the session an attacker is most likely
    to be holding - the one they used to press the button."""
    await _account(db_session, email="caller@example.com", slug="caller")
    session = await _login(http, "caller@example.com")

    await http.post(f"{API}/auth/logout-all", headers=_bearer(session))

    assert (await http.get(f"{API}/auth/me", headers=_bearer(session))).status_code == 401
    assert (await _refresh(http, session["refresh_token"])).status_code == 401


async def test_an_access_token_stops_at_once_rather_than_at_expiry(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Revocation is immediate, not eventual.

    The access token is fifteen minutes long and still signed. It stops working
    because `get_current_user` compares the version on the user row it already
    had to load, so the check costs no extra query.
    """
    user, _ = await _account(db_session, email="immediate@example.com", slug="immediate")
    session = await _login(http, "immediate@example.com")

    assert (await http.get(f"{API}/auth/me", headers=_bearer(session))).status_code == 200

    user.token_version += 1
    await db_session.flush()

    assert (await http.get(f"{API}/auth/me", headers=_bearer(session))).status_code == 401


# ---------------------------------------------------- disable and re-enable


async def test_a_token_issued_before_a_disable_stops_working(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    user, _ = await _account(db_session, email="target@example.com", slug="target")
    staff, _ = await _account(
        db_session,
        email="staff@example.com",
        slug="staffspace",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    victim = await _login(http, "target@example.com")
    operator = await _login(http, "staff@example.com")

    disabled = await http.post(
        f"{API}/platform/users/{user.id}/disable",
        headers=_bearer(operator),
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False

    assert (await http.get(f"{API}/auth/me", headers=_bearer(victim))).status_code == 401
    assert (await _refresh(http, victim["refresh_token"])).status_code == 401
    # And they cannot simply sign in again.
    denied = await http.post(
        f"{API}/auth/login",
        json={"email": "target@example.com", "password": PASSWORD},
    )
    assert denied.status_code == 403


async def test_re_enabling_does_not_resurrect_the_old_tokens(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The subtle one, and the reason `enable` bumps the version too.

    A token minted before the suspension is still signed and may still be inside
    its fifteen minutes. Without the bump on the way back, restoring the account
    would hand that token its authority back - so a disable/enable cycle would
    resurrect exactly the credentials the disable existed to kill.
    """
    user, _ = await _account(db_session, email="returning@example.com", slug="returning")
    staff, _ = await _account(
        db_session,
        email="op@example.com",
        slug="opspace",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    before = await _login(http, "returning@example.com")
    operator = await _login(http, "op@example.com")

    await http.post(f"{API}/platform/users/{user.id}/disable", headers=_bearer(operator))
    restored = await http.post(
        f"{API}/platform/users/{user.id}/enable",
        headers=_bearer(operator),
    )
    assert restored.status_code == 200
    assert restored.json()["is_active"] is True
    # Disable bumped once, enable bumped again.
    assert restored.json()["token_version"] == 3

    assert (await http.get(f"{API}/auth/me", headers=_bearer(before))).status_code == 401
    assert (await _refresh(http, before["refresh_token"])).status_code == 401

    # The control: the account genuinely works again.
    after = await _login(http, "returning@example.com")
    assert (await http.get(f"{API}/auth/me", headers=_bearer(after))).status_code == 200


async def test_disabling_is_deterministic_when_repeated(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Pressing it twice is not an error, and still moves the version - the
    reason somebody presses it twice is usually doubt that the first took."""
    user, _ = await _account(db_session, email="twice@example.com", slug="twice")
    staff, _ = await _account(
        db_session,
        email="twiceop@example.com",
        slug="twiceop",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    operator = await _login(http, "twiceop@example.com")

    first = await http.post(f"{API}/platform/users/{user.id}/disable", headers=_bearer(operator))
    second = await http.post(f"{API}/platform/users/{user.id}/disable", headers=_bearer(operator))

    assert first.status_code == second.status_code == 200
    assert second.json()["token_version"] > first.json()["token_version"]
    assert second.json()["is_active"] is False


# -------------------------------------------------------- password change


async def test_changing_a_password_ends_every_session(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The reason to change a password is usually that something was taken, so
    a change that left the taken thing working would miss the point."""
    await _account(db_session, email="rotate@example.com", slug="rotate")
    session = await _login(http, "rotate@example.com")
    stolen = session["refresh_token"]

    changed = await http.post(
        f"{API}/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=_bearer(session),
    )
    assert changed.status_code == 200
    assert changed.json()["token_version"] == 2

    assert (await _refresh(http, stolen)).status_code == 401
    assert (await http.get(f"{API}/auth/me", headers=_bearer(session))).status_code == 401

    # The new password works and the old one does not.
    assert (await _login(http, "rotate@example.com", NEW_PASSWORD))["access_token"]
    refused = await http.post(
        f"{API}/auth/login",
        json={"email": "rotate@example.com", "password": PASSWORD},
    )
    assert refused.status_code == 401


async def test_a_password_change_needs_the_current_password(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """An access token is not enough. Somebody holding a stolen token but not
    the password is exactly who this is defending against."""
    await _account(db_session, email="proof@example.com", slug="proof")
    session = await _login(http, "proof@example.com")

    refused = await http.post(
        f"{API}/auth/password",
        json={"current_password": "not the password", "new_password": NEW_PASSWORD},
        headers=_bearer(session),
    )

    assert refused.status_code == 401
    # Nothing changed: the session still works and the old password still does.
    assert (await http.get(f"{API}/auth/me", headers=_bearer(session))).status_code == 200


async def test_a_password_change_is_refused_without_credentials(http: AsyncClient) -> None:
    response = await http.post(
        f"{API}/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 401


async def test_no_endpoint_returns_or_echoes_a_password(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Nothing here may hand a credential back, in a body or in an error."""
    await _account(db_session, email="quiet@example.com", slug="quiet")
    session = await _login(http, "quiet@example.com")

    changed = await http.post(
        f"{API}/auth/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=_bearer(session),
    )

    body = changed.text
    assert PASSWORD not in body
    assert NEW_PASSWORD not in body


# ------------------------------------------------------------- adversarial


async def test_a_token_cannot_be_substituted_between_users(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Cross-user substitution.

    The version is a per-user counter, so two accounts routinely share the same
    value. That must not make one account's token usable as another's - the
    subject is what binds a token to a person, and the version only ever narrows
    it further.
    """
    first, first_tenant = await _account(db_session, email="one@example.com", slug="one")
    second, second_tenant = await _account(db_session, email="two@example.com", slug="two")
    assert first.token_version == second.token_version

    one = await _login(http, "one@example.com")
    two = await _login(http, "two@example.com")

    # Each sees only themselves.
    assert (await http.get(f"{API}/auth/me", headers=_bearer(one))).json()["email"] == (
        "one@example.com"
    )
    assert (await http.get(f"{API}/auth/me", headers=_bearer(two))).json()["email"] == (
        "two@example.com"
    )

    # Revoking one leaves the other untouched.
    await http.post(f"{API}/auth/logout-all", headers=_bearer(one))
    assert (await http.get(f"{API}/auth/me", headers=_bearer(one))).status_code == 401
    assert (await http.get(f"{API}/auth/me", headers=_bearer(two))).status_code == 200


async def test_a_token_forged_with_another_users_version_is_still_refused(
    http: AsyncClient,
    db_session: AsyncSession,
    lifecycle_settings: Settings,
) -> None:
    """The version is not an authorization claim on its own.

    Minting a token with a correct-looking version but a subject whose account
    has moved on must fail. This is what stops the counter being replayed.
    """
    user, tenant = await _account(db_session, email="forge@example.com", slug="forge")
    user.token_version = 5
    await db_session.flush()

    stale, _ = create_access_token(
        settings=lifecycle_settings,
        subject=user.id,
        tenant_id=tenant.id,
        token_version=4,
    )
    current, _ = create_access_token(
        settings=lifecycle_settings,
        subject=user.id,
        tenant_id=tenant.id,
        token_version=5,
    )

    assert (
        await http.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {stale}"})
    ).status_code == 401
    assert (
        await http.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {current}"})
    ).status_code == 200


async def test_a_token_carrying_no_version_at_all_is_refused(
    http: AsyncClient,
    db_session: AsyncSession,
    lifecycle_settings: Settings,
) -> None:
    """Tokens minted before the column existed carry no `ver` claim.

    They must fail rather than be waved through, which is the whole point of the
    deploy-time sign-out the migration documents: treating an unversioned token
    as current would leave exactly the tokens this mechanism exists to revoke
    permanently exempt from it.
    """
    user, tenant = await _account(db_session, email="legacy@example.com", slug="legacy")

    unversioned, _ = create_access_token(
        settings=lifecycle_settings,
        subject=user.id,
        tenant_id=tenant.id,
    )

    response = await http.get(
        f"{API}/auth/me",
        headers={"Authorization": f"Bearer {unversioned}"},
    )

    assert response.status_code == 401


async def test_a_refresh_racing_a_revocation_does_not_survive_it(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Concurrent refresh and revocation.

    The dangerous ordering is a refresh that lands just after the bump and mints
    a pair carrying the *new* version - which would hand the thief a fresh,
    valid chain. The check reads the user row inside the same transaction as the
    refresh, so the bump is either visible (refused) or not yet applied (spent,
    and then the next attempt fails).
    """
    user, _ = await _account(db_session, email="race@example.com", slug="race")
    session = await _login(http, "race@example.com")
    stolen = session["refresh_token"]

    user.token_version += 1
    await db_session.flush()

    first = await _refresh(http, stolen)
    second = await _refresh(http, stolen)

    assert first.status_code == 401
    assert second.status_code == 401


async def test_revocation_does_not_disturb_workspace_isolation(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Cross-workspace access is unaffected by any of this.

    Revocation acts on an identity, not on a workspace, so it must neither widen
    nor narrow what a surviving session can reach.
    """
    _, mine = await _account(db_session, email="mine@example.com", slug="mine")
    other_user, theirs = await _account(db_session, email="theirs@example.com", slug="theirs")

    session = await _login(http, "mine@example.com")
    profile = (await http.get(f"{API}/auth/me", headers=_bearer(session))).json()
    assert [w["slug"] for w in profile["workspaces"]] == ["mine"]

    # Revoking the *other* account changes nothing here.
    await http.post(
        f"{API}/auth/logout-all", headers=_bearer(await _login(http, "theirs@example.com"))
    )

    still = await http.get(f"{API}/auth/me", headers=_bearer(session))
    assert still.status_code == 200
    assert [w["slug"] for w in still.json()["workspaces"]] == ["mine"]

    # And switching into a workspace they do not belong to is still refused.
    switched = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": "theirs"},
        headers=_bearer(session),
    )
    assert switched.status_code == 404


# ------------------------------------------------------- who may do what


async def test_a_workspace_owner_cannot_disable_an_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """An account is a global identity.

    A tenant administrator able to disable one could evict somebody from
    workspaces that administrator has nothing to do with. Removing a person from
    *one* workspace is a different operation against a different object - and it
    is still missing, which is recorded rather than hidden.
    """
    target, _ = await _account(db_session, email="innocent@example.com", slug="innocent")
    owner, _ = await _account(db_session, email="owner@example.com", slug="ownerspace")
    session = await _login(http, "owner@example.com")

    for action in ("disable", "enable"):
        response = await http.post(
            f"{API}/platform/users/{target.id}/{action}",
            headers=_bearer(session),
        )
        assert response.status_code == 403, f"{action} -> {response.status_code}"

    # The target is untouched.
    assert (await _login(http, "innocent@example.com"))["access_token"]


async def test_platform_staff_may_disable_an_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The control for the test above, so its 403s mean something."""
    target, _ = await _account(db_session, email="subject@example.com", slug="subject")
    staff, _ = await _account(
        db_session,
        email="admin@example.com",
        slug="adminspace",
        platform_role=PlatformRole.PLATFORM_ADMIN,
    )
    operator = await _login(http, "admin@example.com")

    response = await http.post(
        f"{API}/platform/users/{target.id}/disable",
        headers=_bearer(operator),
    )

    assert response.status_code == 200


async def test_an_administrator_cannot_disable_their_own_account(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """There may be no other administrator to undo it."""
    staff, _ = await _account(
        db_session,
        email="self@example.com",
        slug="selfspace",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    operator = await _login(http, "self@example.com")

    response = await http.post(
        f"{API}/platform/users/{staff.id}/disable",
        headers=_bearer(operator),
    )

    assert response.status_code == 422
    assert (await http.get(f"{API}/auth/me", headers=_bearer(operator))).status_code == 200


async def test_disabling_an_account_that_does_not_exist_is_not_found(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff, _ = await _account(
        db_session,
        email="nf@example.com",
        slug="nfspace",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    operator = await _login(http, "nf@example.com")

    response = await http.post(
        f"{API}/platform/users/{uuid.uuid4()}/disable",
        headers=_bearer(operator),
    )

    assert response.status_code == 404


async def test_the_platform_role_survives_a_revocation(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Revocation ends sessions; it does not demote anybody.

    Platform authority lives on the user row, not in the token, so signing out
    everywhere and back in must return the same authority.
    """
    staff, _ = await _account(
        db_session,
        email="keeps@example.com",
        slug="keepsspace",
        platform_role=PlatformRole.PLATFORM_OWNER,
    )
    first = await _login(http, "keeps@example.com")
    assert (await http.get(f"{API}/platform/tenants", headers=_bearer(first))).status_code == 200

    await http.post(f"{API}/auth/logout-all", headers=_bearer(first))
    assert (await http.get(f"{API}/platform/tenants", headers=_bearer(first))).status_code == 401

    second = await _login(http, "keeps@example.com")
    assert (await http.get(f"{API}/platform/tenants", headers=_bearer(second))).status_code == 200
