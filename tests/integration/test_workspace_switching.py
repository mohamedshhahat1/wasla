"""Switching the active workspace, and then actually using the token.

The bug this file exists for was invisible to every existing test, and the
reason is worth stating because it is a pattern rather than an accident.

`AuthService.select_workspace` minted its access token by calling
`create_access_token` directly and omitting `token_version`, while every other
issuance path went through `_issue`, which passes it. A token with no `ver`
claim decodes to `None`; `get_current_user` compares that against
`users.token_version`, which defaults to 1. So the endpoint answered 200 with a
token that every subsequent request refused as revoked - multi-workspace
switching, a headline requirement of the product, did not work at all
(ADR-058).

`tests/integration/test_auth_endpoints.py` asserts the happy path against a
stubbed `AuthService` that returns the literal `"switched-value"`, and
`test_account_lifecycle.py` only exercises the 404 refusal. **No test ever used
the token that came back.** So every test here does: it switches, then spends
the token on `/auth/me` and on a workspace-scoped resource, and checks which
workspace the request was actually scoped to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import SESSION_STATE_ATTRIBUTE, get_session
from app.core.security import hash_password
from app.db.models import Membership, MembershipStatus, Tenant, TenantRole, User
from app.db.models.enums import TenantStatus
from app.main import create_app
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"
EMAIL = "member@example.com"
PASSWORD = "correct horse battery staple"


class _Redis:
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
    def __init__(self) -> None:
        self.commands = _Redis()

    @property
    def client(self) -> _Redis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def switch_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
    )


@pytest.fixture
def app(switch_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(switch_settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session(request: Request) -> AsyncIterator[AsyncSession]:
        setattr(request.state, SESSION_STATE_ATTRIBUTE, db_session)
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


async def _person(session: AsyncSession) -> User:
    user = User(email=EMAIL, hashed_password=hash_password(PASSWORD), is_active=True)
    session.add(user)
    await session.flush()
    return user


async def _workspace(
    session: AsyncSession,
    *,
    user: User,
    slug: str,
    role: TenantRole = TenantRole.TENANT_OWNER,
    status: MembershipStatus = MembershipStatus.ACTIVE,
) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=role, status=status))
    await session.flush()
    return tenant


async def _login(http: AsyncClient) -> dict:
    response = await http.post(
        f"{API}/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _switch(http: AsyncClient, access: str, slug: str):
    return await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": slug},
        headers=_bearer(access),
    )


# ------------------------------------------------------- the bug, closed


async def test_a_switched_token_is_accepted_by_the_next_request(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The whole defect in one assertion: the returned token has to work."""
    person = await _person(db_session)
    await _workspace(db_session, user=person, slug="alpha")
    await _workspace(db_session, user=person, slug="beta", role=TenantRole.TENANT_ADMIN)

    session = await _login(http)
    switched = await _switch(http, session["access_token"], "beta")
    assert switched.status_code == 200, switched.text
    token = switched.json()["access_token"]

    me = await http.get(f"{API}/auth/me", headers=_bearer(token))

    assert me.status_code == 200, me.text
    assert me.json()["email"] == EMAIL


async def test_a_switched_token_is_scoped_to_the_workspace_it_named(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Not merely accepted: accepted *as beta*.

    A workspace-scoped resource is read with the switched token, and the answer
    has to describe beta rather than alpha - which is the only way to tell a
    working switch from a token that happens to authenticate.
    """
    person = await _person(db_session)
    alpha = await _workspace(db_session, user=person, slug="alpha")
    beta = await _workspace(db_session, user=person, slug="beta", role=TenantRole.TENANT_ADMIN)

    session = await _login(http)
    assert session["active_workspace"]["slug"] == alpha.slug

    switched = await _switch(http, session["access_token"], "beta")
    body = switched.json()
    assert body["active_workspace"]["slug"] == beta.slug
    assert body["active_workspace"]["role"] == TenantRole.TENANT_ADMIN.value
    # No refresh token: switching workspace must not disturb the long-lived
    # credential.
    assert "refresh_token" not in body

    token = body["access_token"]
    members = await http.get(f"{API}/workspace/members", headers=_bearer(token))

    assert members.status_code == 200, members.text
    listed = members.json()["members"]
    assert [entry["role"] for entry in listed] == [TenantRole.TENANT_ADMIN.value]


async def test_switching_back_and_forth_keeps_working(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Each switch mints from the same policy, so none of them is a dead end."""
    person = await _person(db_session)
    await _workspace(db_session, user=person, slug="alpha")
    await _workspace(db_session, user=person, slug="beta")

    session = await _login(http)
    to_beta = await _switch(http, session["access_token"], "beta")
    back = await _switch(http, to_beta.json()["access_token"], "alpha")

    assert back.status_code == 200
    assert back.json()["active_workspace"]["slug"] == "alpha"
    me = await http.get(f"{API}/auth/me", headers=_bearer(back.json()["access_token"]))
    assert me.status_code == 200


# ------------------------------------------------- what must still be refused


async def test_a_workspace_without_a_membership_is_not_switchable(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """404 rather than 403: whether that workspace exists is not disclosed."""
    person = await _person(db_session)
    await _workspace(db_session, user=person, slug="alpha")
    stranger = Tenant(name="Theirs", slug="theirs", status=TenantStatus.ACTIVE)
    db_session.add(stranger)
    await db_session.flush()

    session = await _login(http)
    refused = await _switch(http, session["access_token"], "theirs")

    assert refused.status_code == 404


async def test_a_revoked_membership_is_not_switchable(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Revocation (ADR-038) is not weakened by the token fix."""
    person = await _person(db_session)
    await _workspace(db_session, user=person, slug="alpha")
    await _workspace(
        db_session,
        user=person,
        slug="gone",
        status=MembershipStatus.REVOKED,
    )

    session = await _login(http)
    refused = await _switch(http, session["access_token"], "gone")

    assert refused.status_code == 404


async def test_a_membership_revoked_after_switching_stops_the_token(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The switched token is subject to the same per-request membership re-read.

    Minting it correctly must not make it exempt from anything a login token is
    subject to.
    """
    person = await _person(db_session)
    await _workspace(db_session, user=person, slug="alpha")
    beta = await _workspace(db_session, user=person, slug="beta")

    session = await _login(http)
    token = (await _switch(http, session["access_token"], "beta")).json()["access_token"]
    assert (await http.get(f"{API}/workspace/members", headers=_bearer(token))).status_code == 200

    membership = (
        await db_session.execute(
            Membership.__table__.select().where(Membership.tenant_id == beta.id)
        )
    ).first()
    assert membership is not None
    await db_session.execute(
        Membership.__table__.update()
        .where(Membership.tenant_id == beta.id)
        .values(status=MembershipStatus.REVOKED)
    )
    await db_session.flush()

    after = await http.get(f"{API}/workspace/members", headers=_bearer(token))

    assert after.status_code == 404


async def test_global_revocation_invalidates_a_switched_token(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The point of carrying `ver` at all.

    A switched token that omitted the claim was rejected for the wrong reason -
    it never matched. Now it matches, so this test is what proves the claim is
    still doing its job rather than merely being present.
    """
    person = await _person(db_session)
    await _workspace(db_session, user=person, slug="alpha")
    await _workspace(db_session, user=person, slug="beta")

    session = await _login(http)
    token = (await _switch(http, session["access_token"], "beta")).json()["access_token"]
    assert (await http.get(f"{API}/auth/me", headers=_bearer(token))).status_code == 200

    revoked = await http.post(f"{API}/auth/logout-all", headers=_bearer(token))
    assert revoked.status_code == 200

    after = await http.get(f"{API}/auth/me", headers=_bearer(token))

    assert after.status_code == 401
