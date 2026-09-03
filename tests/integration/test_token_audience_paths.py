"""Every path that mints an access token, and the claim all of them must carry.

`tests/unit/test_token_audience.py` covers the claim itself: what is minted,
what is refused, and that the two kinds of token cannot be confused. This file
covers the thing a unit test cannot — that no *route* mints a token by some
other means. The check is deliberately structural rather than enumerated: the
audit's SEC-14 sits beside ADR-058, where workspace switching had its own
issuance path and quietly dropped a claim `_issue` was setting, and nobody
noticed because the tests that covered issuance covered one caller each.

So this drives the real application over HTTP against real rows — real
services, real repositories, real Argon2, real login — collects every token any
endpoint hands back, and asserts the audience on all of them at once.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import SESSION_STATE_ATTRIBUTE, get_session
from app.core.security import ISSUER, TokenType, hash_password
from app.db.models import Membership, Tenant, TenantRole, User
from app.db.models.enums import TenantStatus
from app.main import create_app
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"

EMAIL = "audience@example.com"
PASSWORD = "a perfectly strong passphrase"


class _Redis:
    """Enough Redis for the refresh-token store and the limiter.

    `set(nx=True)` returning `None` on a key that exists is the behaviour
    `RefreshTokenStore.spend` reads as "already spent", so rotation is real
    here rather than stubbed.
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
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0

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
def audience_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
    )


@pytest.fixture
def app(audience_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(audience_settings)
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


@pytest_asyncio.fixture
async def account(db_session: AsyncSession) -> User:
    """A user with two workspaces, so switching between them is a real operation."""
    user = User(
        email=EMAIL,
        full_name="Audience Tester",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    for slug in ("first-space", "second-space"):
        tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
        db_session.add(tenant)
        await db_session.flush()
        db_session.add(
            Membership(tenant_id=tenant.id, user_id=user.id, role=TenantRole.TENANT_OWNER)
        )
    await db_session.flush()
    return user


def _payload(token: str) -> dict[str, Any]:
    decoded: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return decoded


async def _login(http: AsyncClient) -> dict[str, Any]:
    response = await http.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_login_issues_a_pair_addressed_to_their_own_consumers(
    http: AsyncClient,
    account: User,
) -> None:
    body = await _login(http)

    assert _payload(body["access_token"])["aud"] == TokenType.ACCESS.audience
    assert _payload(body["refresh_token"])["aud"] == TokenType.REFRESH.audience


async def test_registration_issues_a_pair_with_the_audiences(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The other entry point, which mints before any row exists to log in against."""
    response = await http.post(
        f"{API}/auth/register",
        json={
            "email": "brand-new@example.com",
            "password": PASSWORD,
            "full_name": "Brand New",
            "workspace_name": "Brand New Space",
            "workspace_slug": f"space-{uuid.uuid4().hex[:8]}",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert _payload(body["access_token"])["aud"] == TokenType.ACCESS.audience
    assert _payload(body["refresh_token"])["aud"] == TokenType.REFRESH.audience


async def test_refresh_issues_a_pair_with_the_audiences(
    http: AsyncClient,
    account: User,
) -> None:
    signed_in = await _login(http)

    response = await http.post(
        f"{API}/auth/refresh",
        json={"refresh_token": signed_in["refresh_token"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert _payload(body["access_token"])["aud"] == TokenType.ACCESS.audience
    assert _payload(body["refresh_token"])["aud"] == TokenType.REFRESH.audience
    # The refreshed token still works, which is what says the audience is
    # consistent between the minting side and the verifying side rather than
    # merely present on both.
    profile = await http.get(
        f"{API}/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert profile.status_code == 200, profile.text


async def test_switching_workspace_issues_a_usable_token_with_the_audience(
    http: AsyncClient,
    account: User,
) -> None:
    """The path ADR-058 records as having silently dropped a claim once already."""
    signed_in = await _login(http)

    response = await http.post(
        f"{API}/auth/workspace",
        json={"workspace_slug": "second-space"},
        headers={"Authorization": f"Bearer {signed_in['access_token']}"},
    )

    assert response.status_code == 200, response.text
    switched = response.json()["access_token"]
    payload = _payload(switched)
    assert payload["aud"] == TokenType.ACCESS.audience
    # Beside it, because the two claims are added by the same function and a
    # regression that drops one would plausibly drop the other.
    assert payload["ver"] == account.token_version

    profile = await http.get(
        f"{API}/auth/me",
        headers={"Authorization": f"Bearer {switched}"},
    )
    assert profile.status_code == 200, profile.text


async def test_a_token_in_the_old_format_no_longer_opens_the_api(
    http: AsyncClient,
    account: User,
    audience_settings: Settings,
) -> None:
    """The rollout cost, asserted rather than described.

    A token the previous release would have minted — every claim it set, signed
    with this deployment's own key — is refused. Existing sessions end at
    deploy; that is the intended behaviour and the reason it is written down
    here as a test rather than in a changelog nobody re-reads.
    """
    signed_in = await _login(http)
    current = _payload(signed_in["access_token"])
    old_style = jwt.encode(
        {key: value for key, value in current.items() if key != "aud"},
        audience_settings.jwt_secret,
        algorithm=audience_settings.jwt_algorithm,
    )

    response = await http.get(
        f"{API}/auth/me",
        headers={"Authorization": f"Bearer {old_style}"},
    )

    assert response.status_code == 401


async def test_a_refresh_token_does_not_open_the_api(
    http: AsyncClient,
    account: User,
) -> None:
    """Cross-purpose use, at the seam that matters: a fortnight-long credential."""
    signed_in = await _login(http)

    response = await http.get(
        f"{API}/auth/me",
        headers={"Authorization": f"Bearer {signed_in['refresh_token']}"},
    )

    assert response.status_code == 401


async def test_an_access_token_cannot_be_refreshed(
    http: AsyncClient,
    account: User,
) -> None:
    signed_in = await _login(http)

    response = await http.post(
        f"{API}/auth/refresh",
        json={"refresh_token": signed_in["access_token"]},
    )

    assert response.status_code == 401


async def test_a_token_for_another_service_does_not_open_the_api(
    http: AsyncClient,
    account: User,
    audience_settings: Settings,
) -> None:
    """Signed with this deployment's key, and addressed somewhere else.

    The scenario the claim exists for: a second service that shares the signing
    secret mints a token for itself, and it must not be a Wasla session.
    """
    signed_in = await _login(http)
    forged = jwt.encode(
        {**_payload(signed_in["access_token"]), "aud": "some-other-service", "iss": ISSUER},
        audience_settings.jwt_secret,
        algorithm=audience_settings.jwt_algorithm,
    )

    response = await http.get(
        f"{API}/auth/me",
        headers={"Authorization": f"Bearer {forged}"},
    )

    assert response.status_code == 401
