"""What the authentication endpoints do when Redis is not there.

Refresh and logout are the two routes that write to a Redis-backed replay
control, and the store used to let `RedisError` escape. FastAPI has no handler
for it, so it reached `handle_unexpected_error` and came back as a 500: the
wrong status for a dependency outage, an alarm pointed at the application rather
than at the infrastructure, and a response a client cannot act on - a 500 is not
something to retry, and this is.

The direction was never in doubt. It failed *closed*, which is right, and this
file is careful to keep it that way: the assertions are not only "answers 503"
but "answers 503 **and issues nothing**". A fix that made the outage legible by
letting the refresh through would be a much worse bug than the one it replaced.

Three things are proved here that the unit tests around `RefreshTokenStore`
cannot reach, because they are properties of the whole request:

- the status and body a caller actually receives, through the real exception
  handler, including that no Redis host, URL or exception text is in it;
- that nothing is issued or committed on the way past - no access token, no
  refresh token, and no bump of `users.token_version`;
- that the paths which do **not** depend on Redis keep working during the
  outage, so "fail closed" means this control and not the product. Signing in
  still works, and so does signing out everywhere, because bulk revocation
  lives in PostgreSQL (ADR-036).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import TokenType, decode_token, hash_password
from app.db.models import Membership, Tenant, TenantRole, User
from app.db.models.enums import TenantStatus
from app.main import create_app
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"
EMAIL = "outage@example.com"

# What redis-py raises when the server is gone, message and all. The message is
# the point of the hygiene assertion below: it carries the address it could not
# reach, and a configured URL may carry a password.
OUTAGE_MESSAGE = "Error connecting to redis://:hunter2@cache.internal:6379. Connection refused."


class _Redis:
    """An in-memory Redis with a switch.

    `down` is flipped by the tests rather than fixed at construction, so one
    fixture covers the outage, the recovery and the control - and so a
    "recovered" assertion is genuinely the same object that just failed, not a
    fresh one that never did.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.down = False

    def _guard(self) -> None:
        if self.down:
            raise RedisConnectionError(OUTAGE_MESSAGE)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        self._guard()
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expiries[key] = ex if ex is not None else -1
        return True

    async def exists(self, key: str) -> int:
        self._guard()
        return 1 if key in self.values else 0

    async def incr(self, key: str) -> int:
        self._guard()
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self._guard()
        self.expiries[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        self._guard()
        return self.expiries.get(key, -1)

    async def rpush(self, key: str, value: str) -> int:
        self._guard()
        return 1


class _Infra:
    def __init__(self, commands: _Redis | None = None) -> None:
        self.commands = commands or _Redis()

    @property
    def client(self) -> _Redis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def redis() -> _Redis:
    return _Redis()


@pytest.fixture
def outage_settings() -> Settings:
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
    outage_settings: Settings,
    db_session: AsyncSession,
    redis: _Redis,
) -> Iterator[FastAPI]:
    application = create_app(outage_settings)
    application.state.database = _Infra()
    application.state.redis = _Infra(redis)

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


@pytest_asyncio.fixture
async def account(db_session: AsyncSession) -> User:
    user = User(
        email=EMAIL,
        full_name="Outage Person",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    tenant = Tenant(name="Outage", slug="outage", status=TenantStatus.ACTIVE)
    db_session.add_all([user, tenant])
    await db_session.flush()
    db_session.add(
        Membership(tenant_id=tenant.id, user_id=user.id, role=TenantRole.TENANT_OWNER),
    )
    await db_session.flush()
    return user


async def _login(http: AsyncClient) -> dict:
    response = await http.post(
        f"{API}/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------- refresh


async def test_refreshing_during_a_redis_outage_answers_503_and_issues_nothing(
    http: AsyncClient,
    account: User,
    redis: _Redis,
    outage_settings: Settings,
) -> None:
    """The finding, closed: 503 rather than 500, and no new credentials.

    The second assertion is the one that matters. Spending the token is what
    proves it has not been replayed, so a refresh that cannot spend must not
    mint anything - and the body is checked field by field rather than by
    status alone, because a handler that answered 503 while still returning a
    pair would satisfy a status-only test.
    """
    session = await _login(http)
    redis.down = True

    response = await http.post(
        f"{API}/auth/refresh",
        json={"refresh_token": session["refresh_token"]},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "dependency_unavailable"
    assert body["error"]["details"] == {"dependency": "redis"}
    assert "access_token" not in body
    assert "refresh_token" not in body

    # And nothing was written down on the way past: the original token is
    # neither spent nor recorded, so its holder is not locked out by the outage.
    claims = decode_token(
        session["refresh_token"],
        settings=outage_settings,
        expected_type=TokenType.REFRESH,
    )
    assert not any(str(claims.token_id) in key for key in redis.values)


async def test_the_outage_response_names_the_dependency_and_nothing_else(
    http: AsyncClient,
    account: User,
    redis: _Redis,
) -> None:
    """No connection string, no password, no exception text, no traceback.

    redis-py puts the address it failed to reach into the exception, and a URL
    configured with a password is one string away from that. This is also the
    response most likely to be pasted into a ticket.
    """
    session = await _login(http)
    redis.down = True

    response = await http.post(
        f"{API}/auth/refresh",
        json={"refresh_token": session["refresh_token"]},
    )

    rendered = response.text
    for leak in ("hunter2", "redis://", "cache.internal", "6379", "ConnectionError", "Traceback"):
        assert leak not in rendered, f"{leak!r} reached the client"


async def test_the_account_survives_the_outage_untouched(
    http: AsyncClient,
    account: User,
    redis: _Redis,
    db_session: AsyncSession,
) -> None:
    """A dependency failure is not a replay, and must not be answered like one.

    A refusal that bumped `token_version` would sign the whole account out of
    every device because a cache was briefly unreachable - the teardown is for
    a *detected* reuse, and an outage detects nothing.
    """
    before = account.token_version
    session = await _login(http)
    redis.down = True

    await http.post(f"{API}/auth/refresh", json={"refresh_token": session["refresh_token"]})

    await db_session.refresh(account)
    assert account.token_version == before


async def test_refreshing_works_again_the_moment_redis_returns(
    http: AsyncClient,
    account: User,
    redis: _Redis,
) -> None:
    """No sticky error state, and no credential burnt by the failed attempt."""
    session = await _login(http)

    redis.down = True
    refused = await http.post(
        f"{API}/auth/refresh",
        json={"refresh_token": session["refresh_token"]},
    )
    assert refused.status_code == 503

    redis.down = False
    recovered = await http.post(
        f"{API}/auth/refresh",
        json={"refresh_token": session["refresh_token"]},
    )

    assert recovered.status_code == 200
    assert recovered.json()["access_token"] != session["access_token"]


# ---------------------------------------------------------------------- logout


async def test_logout_refuses_rather_than_reporting_a_revocation_it_did_not_make(
    http: AsyncClient,
    account: User,
    redis: _Redis,
) -> None:
    """204 would be a lie, and a specifically dangerous one.

    Somebody signing out of a shared machine reads 204 as "that token is dead".
    If the write never happened it is live for another fortnight, and they have
    been told otherwise. There is no second authoritative record for an
    individual refresh token - `token_version` revokes the whole estate, which
    is `/auth/logout-all`, a different request with different consequences - so
    an honest 503 is the only available answer.
    """
    session = await _login(http)
    redis.down = True

    response = await http.post(
        f"{API}/auth/logout",
        json={"refresh_token": session["refresh_token"]},
    )

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"dependency": "redis"}

    # And the claim it refused to make is genuinely still untrue: once Redis is
    # back, the token has not been revoked behind the caller's back.
    redis.down = False
    still_live = await http.post(
        f"{API}/auth/refresh",
        json={"refresh_token": session["refresh_token"]},
    )
    assert still_live.status_code == 200


async def test_logout_still_ignores_a_token_it_cannot_read(
    http: AsyncClient,
    account: User,
    redis: _Redis,
) -> None:
    """An unreadable token is answered before Redis is consulted at all.

    Logging out with a garbage token stays 204 during an outage, because the
    route never reaches the store. Turning that into a 503 would have made the
    endpoint an oracle for whether Redis is up.
    """
    redis.down = True

    response = await http.post(f"{API}/auth/logout", json={"refresh_token": "not-a-jwt"})

    assert response.status_code == 204


# ------------------------------------------- what the outage must *not* break


async def test_signing_in_is_unaffected_by_the_outage(
    http: AsyncClient,
    account: User,
    redis: _Redis,
) -> None:
    """Fail closed on the replay control, not on the product.

    Nothing in `/auth/login` consults the denylist - a token being issued has
    no history to check - so an outage that broke sign-in would mean the
    coupling had been drawn wider than it needs to be.
    """
    redis.down = True

    response = await http.post(
        f"{API}/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
    )

    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_signing_out_everywhere_still_works_during_the_outage(
    http: AsyncClient,
    account: User,
    redis: _Redis,
) -> None:
    """Bulk revocation lives in PostgreSQL, and that is what makes 503 tolerable.

    Somebody who believes a token has leaked can still act during a Redis
    outage: `token_version` is a column, the access-token check reads it on
    every request, and `AuthService.refresh` compares against it too. So the
    lever that matters most is the one that does not depend on the cache.
    """
    session = await _login(http)
    before = account.token_version
    redis.down = True

    response = await http.post(
        f"{API}/auth/logout-all",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )

    assert response.status_code == 200
    assert response.json()["token_version"] > before

    # And the revocation is real rather than a number in a response body: the
    # access token stops at once, on the version check that rides the user row
    # `get_current_user` already loads - and that check runs during the outage,
    # because it reads PostgreSQL.
    denied = await http.get(
        f"{API}/auth/me",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    assert denied.status_code == 401

    # The refresh token is dead too, which is the half that outlives the
    # outage. Checked once Redis is back, because refusing it is a decision the
    # refresh path only reaches after spending the token.
    redis.down = False
    replayed = await http.post(
        f"{API}/auth/refresh",
        json={"refresh_token": session["refresh_token"]},
    )
    assert replayed.status_code == 401
