"""The signed client-IP forward through the real auth routes and the real buckets (FEB-01).

Every request in this file arrives from one peer - the test transport's
address - which is exactly the frontend tier's position under the session
design of ADR-189: the browser talks to the tier, the tier talks to us. The
questions are the two FEB-01 exists to answer:

- does a person signing in through the tier get their own bucket, and
- can anybody who is not the tier choose the bucket a request is counted in?

The first must be yes and the second must be no, and the second is asserted in
more shapes than the first: unsigned, signed for another route, stale, signed
with the wrong secret, the feature off. Each of those must behave exactly as
the code did before the forward existed - counted under the tier's own address,
answered with the route's own status, and never refused for the forward itself.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_auth_service, get_entitlement_service
from app.core import client_ip_forward
from app.core.client_ip_forward import (
    FORWARD_IP_HEADER,
    FORWARD_SIG_HEADER,
    FORWARD_TS_HEADER,
    sign_forward,
)
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.exceptions import AuthenticationError
from app.core.telemetry import CLIENT_IP_FORWARD
from app.main import create_app
from tests.conftest import AllowingEntitlements, FakeDependency

pytestmark = pytest.mark.integration

API = "/api/v1"
LOGIN = f"{API}/auth/login"
REFRESH = f"{API}/auth/refresh"
SECRET = "integration-forward-secret-0123456789abcdef"
NOW = 1_794_000_000
LIMIT = 10


class RefusingAuth:
    """Every credential is wrong, so only the limiter decides between 401 and 429."""

    async def login(self, **kwargs: object) -> None:
        raise AuthenticationError("The credentials are not valid.")

    async def refresh(self, **kwargs: object) -> None:
        raise AuthenticationError("The credentials are not valid.")


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verifier's clock seam, fixed: no test here depends on the wall clock."""
    monkeypatch.setattr(client_ip_forward, "forward_clock", lambda: float(NOW))


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "rate_limit_enabled": True,
        "rate_limit_auth_per_minute": LIMIT,
        "session_forward_secret": SECRET,
    }
    values.update(overrides)
    return Settings(**values)


def _app(settings: Settings) -> FastAPI:
    application = create_app(settings)
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = FakeDependency(name="redis")
    application.dependency_overrides[get_auth_service] = RefusingAuth
    return application


@pytest_asyncio.fixture
async def tier() -> AsyncIterator[AsyncClient]:
    """The frontend tier: one peer address, the secret configured."""
    async with AsyncClient(
        transport=ASGITransport(app=_app(_settings())), base_url="http://wasla.test"
    ) as client:
        yield client


def _signed(
    client_ip: str,
    *,
    path: str = LOGIN,
    method: str = "POST",
    ts: int = NOW,
    secret: str = SECRET,
) -> dict[str, str]:
    return {
        FORWARD_IP_HEADER: client_ip,
        FORWARD_TS_HEADER: str(ts),
        FORWARD_SIG_HEADER: sign_forward(
            secret, ts=str(ts), client_ip=client_ip, method=method, path=path
        ),
    }


async def _logins(client: AsyncClient, headers: list[dict[str, str]]) -> list[int]:
    statuses = []
    for n, extra in enumerate(headers):
        response = await client.post(
            LOGIN,
            json={"email": f"person-{n}@example.com", "password": "a-wrong-password-123"},
            headers=extra,
        )
        statuses.append(response.status_code)
    return statuses


def _refused_at_the_bucket(statuses: list[int]) -> bool:
    """The shape the tier's own bucket produces: ten answers, then refusals."""
    return statuses == [401] * LIMIT + [429] * (len(statuses) - LIMIT)


# ------------------------------------------------------------------------- F-6


async def test_people_signing_in_through_the_tier_are_each_counted_as_themselves(
    tier: AsyncClient,
) -> None:
    """P-F01's shape: twelve accounts behind one peer, each with its own signed address."""
    statuses = await _logins(tier, [_signed(f"203.0.113.{n + 1}") for n in range(12)])

    assert 429 not in statuses, statuses
    assert statuses == [401] * 12


async def test_one_browser_through_the_tier_still_meets_its_own_bucket(
    tier: AsyncClient,
) -> None:
    statuses = await _logins(tier, [_signed("198.51.100.9") for _ in range(LIMIT + 1)])

    assert _refused_at_the_bucket(statuses), statuses


async def test_the_forward_does_not_borrow_the_tiers_own_budget(tier: AsyncClient) -> None:
    """The tier's own address spent its budget; a signed browser is unaffected."""
    await _logins(tier, [{} for _ in range(LIMIT + 1)])

    statuses = await _logins(tier, [_signed("198.51.100.10")])

    assert statuses == [401]


# ------------------------------------------------------------------------- F-7


async def test_an_unsigned_claim_changes_nothing(tier: AsyncClient) -> None:
    """P-F02's shape: the same peer, twelve different UNSIGNED claims."""
    statuses = await _logins(tier, [{FORWARD_IP_HEADER: f"192.0.2.{n + 1}"} for n in range(12)])

    assert _refused_at_the_bucket(statuses), statuses


async def test_a_claim_signed_with_another_secret_changes_nothing(tier: AsyncClient) -> None:
    forged = [
        _signed(f"192.0.2.{n + 1}", secret="an-attackers-guess-0123456789abcdef") for n in range(12)
    ]

    assert _refused_at_the_bucket(await _logins(tier, forged))


async def test_a_stale_claim_changes_nothing(tier: AsyncClient) -> None:
    stale = [_signed(f"192.0.2.{n + 1}", ts=NOW - 61) for n in range(12)]

    assert _refused_at_the_bucket(await _logins(tier, stale))


async def test_a_malformed_claim_changes_nothing(tier: AsyncClient) -> None:
    malformed = [_signed(f"192.0.2.{n + 1}:443") for n in range(12)]

    assert _refused_at_the_bucket(await _logins(tier, malformed))


async def test_a_bad_forward_is_never_refused_for_itself(tier: AsyncClient) -> None:
    """A misconfigured tier degrades to the old bucket, not to an error."""
    response = await tier.post(
        LOGIN,
        json={"email": "a@example.com", "password": "a-wrong-password-123"},
        headers={FORWARD_IP_HEADER: "x", FORWARD_TS_HEADER: "y", FORWARD_SIG_HEADER: "z"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_with_the_feature_off_a_perfect_signature_changes_nothing() -> None:
    application = _app(_settings(session_forward_secret=None))
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://wasla.test"
    ) as client:
        statuses = await _logins(client, [_signed(f"203.0.113.{n + 1}") for n in range(12)])

    assert _refused_at_the_bucket(statuses), statuses


# ------------------------------------------------------------------------- F-8


async def test_a_signature_for_login_is_not_an_address_on_refresh(tier: AsyncClient) -> None:
    statuses = []
    for n in range(12):
        response = await tier.post(
            REFRESH,
            json={"refresh_token": "not-a-real-refresh-token"},
            headers=_signed(f"203.0.113.{n + 1}", path=LOGIN),
        )
        statuses.append(response.status_code)

    assert _refused_at_the_bucket(statuses), statuses


async def test_a_signature_for_post_is_not_an_address_for_another_method(
    tier: AsyncClient,
) -> None:
    statuses = await _logins(tier, [_signed(f"203.0.113.{n + 1}", method="GET") for n in range(12)])

    assert _refused_at_the_bucket(statuses), statuses


# ------------------------------------------------------------------------- F-9


@pytest.mark.parametrize("secret", [None, SECRET])
async def test_a_trusted_proxys_header_is_honoured_as_before(secret: str | None) -> None:
    """No forward present: TRUSTED_PROXY_IPS behaves exactly as it did, feature on or off."""
    application = _app(_settings(session_forward_secret=secret, trusted_proxy_ips=["127.0.0.1"]))
    async with AsyncClient(
        transport=ASGITransport(app=application, client=("127.0.0.1", 50000)),
        base_url="http://wasla.test",
    ) as client:
        spread = await _logins(client, [{"X-Real-IP": f"203.0.113.{n + 1}"} for n in range(12)])
        same = await _logins(client, [{"X-Real-IP": "198.51.100.1"} for _ in range(LIMIT + 1)])

    assert spread == [401] * 12
    assert _refused_at_the_bucket(same), same


async def test_a_valid_forward_wins_over_a_trusted_proxys_header() -> None:
    application = _app(_settings(trusted_proxy_ips=["127.0.0.1"]))
    async with AsyncClient(
        transport=ASGITransport(app=application, client=("127.0.0.1", 50000)),
        base_url="http://wasla.test",
    ) as client:
        # Every request names the same nginx-reported address, but the tier
        # signed a different browser each time: the signed one is the identity.
        statuses = await _logins(
            client,
            [{"X-Real-IP": "198.51.100.1", **_signed(f"203.0.113.{n + 1}")} for n in range(12)],
        )

    assert statuses == [401] * 12


# ------------------------------------------------------------------------ F-10


@pytest.fixture
def account_limited_app(db_session: AsyncSession) -> Iterator[FastAPI]:
    """The real AuthService, so its per-account bucket is the one being counted."""
    application = create_app(
        _settings(rate_limit_auth_per_minute=50, rate_limit_login_per_account_per_minute=3)
    )
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = FakeDependency(name="redis")

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


async def test_the_per_account_bucket_still_trips_across_many_signed_addresses(
    account_limited_app: FastAPI,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=account_limited_app), base_url="http://wasla.test"
    ) as client:
        statuses = []
        for n in range(6):
            response = await client.post(
                LOGIN,
                json={"email": "victim@example.com", "password": f"guess-number-{n}"},
                headers=_signed(f"203.0.113.{n + 1}"),
            )
            statuses.append(response.status_code)

    assert statuses[:3] == [401, 401, 401], statuses
    assert set(statuses[3:]) == {429}, statuses


# ------------------------------------------------------------------------ F-12


async def test_nothing_of_the_forward_reaches_a_log_a_metric_or_a_response(
    tier: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    # A fresh minute for the throttled warning, so the refusals below do log.
    client_ip_forward._last_warned.clear()
    sent: list[str] = [SECRET]
    bodies: list[str] = []
    cases = [
        _signed("203.0.113.201"),
        _signed("203.0.113.202", path=LOGIN, ts=NOW - 600),
        _signed("203.0.113.203", secret="another-secret-0123456789abcdef0123"),
        {FORWARD_IP_HEADER: "203.0.113.204"},
    ]
    for headers in cases:
        sent.extend(value for value in headers.values() if value != str(NOW))
        response = await tier.post(
            LOGIN,
            json={"email": "someone@example.com", "password": "a-wrong-password-123"},
            headers=headers,
        )
        bodies.append(response.text + str(dict(response.headers)))
    refresh = await tier.post(
        REFRESH, json={"refresh_token": "nope"}, headers=_signed("203.0.113.205", path=LOGIN)
    )
    bodies.append(refresh.text + str(dict(refresh.headers)))
    sent.extend(_signed("203.0.113.205", path=LOGIN).values())

    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    rendered = "\n".join(CLIENT_IP_FORWARD.render())
    for value in sent:
        if value == str(NOW - 600):
            continue
        assert value not in logged, value
        assert value not in rendered, value
        for body in bodies:
            assert value not in body, value
    # The refusals were seen - by kind, and only by kind.
    assert "client_ip_forward.ignored" in logged
    assert 'result="valid"' in rendered
    assert 'result="bad_signature"' in rendered
