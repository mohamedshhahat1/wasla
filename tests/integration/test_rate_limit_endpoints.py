"""Rate limits through the real routes and the real dependency wiring.

The refusals are worth checking once. What is worth checking carefully is the
*absence*: the WhatsApp webhook must never be refused, because Meta retries a
non-2xx and eventually disables the subscription — so a 429 there does not shed
load, it loses a customer's message and then the integration (ADR-032).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_agent_service,
    get_auth_service,
)
from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.main import create_app

pytestmark = pytest.mark.integration

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class RefusingAuth:
    """Every login fails, so nothing but the limiter decides the status."""

    async def login(self, **kwargs: object) -> None:
        raise AuthenticationError("The credentials are not valid.")


class StubAgents:
    async def list_agents(self, *, limit: int = 50) -> list[object]:
        return []


def _workspace(tenant_id: uuid.UUID) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=USER_ID,
            tenant_id=tenant_id,
            role=TenantRole.TENANT_OWNER,
        ),
        tenant=Tenant(id=tenant_id, name="Acme", slug="acme", status=TenantStatus.ACTIVE),
    )


@pytest.fixture
def limited_settings() -> Settings:
    """Limiting switched on, with small budgets so a test is not a loop."""
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=True,
        rate_limit_auth_per_minute=3,
        rate_limit_workspace_per_minute=4,
        rate_limit_campaign_per_minute=2,
    )


@pytest.fixture
def limited_app(limited_settings: Settings, fake_database, fake_redis) -> FastAPI:
    app = create_app(limited_settings)
    app.state.database = fake_database
    app.state.redis = fake_redis
    return app


@pytest.fixture
async def limited_client(limited_app: FastAPI):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=limited_app),
        base_url="http://wasla.test",
    ) as client:
        yield client


# ------------------------------------------------------------ authentication


async def test_repeated_logins_are_refused_after_the_limit(limited_app, limited_client):
    limited_app.dependency_overrides[get_auth_service] = RefusingAuth
    body = {"email": "someone@example.com", "password": "a-very-long-password-1"}

    statuses = [
        (await limited_client.post("/api/v1/auth/login", json=body)).status_code for _ in range(4)
    ]

    # Three attempts answer 401 - wrong credentials - and the fourth is refused
    # for being the fourth.
    assert statuses == [401, 401, 401, 429]


async def test_the_refusal_says_when_to_come_back(limited_app, limited_client):
    limited_app.dependency_overrides[get_auth_service] = RefusingAuth
    body = {"email": "someone@example.com", "password": "a-very-long-password-1"}
    for _ in range(3):
        await limited_client.post("/api/v1/auth/login", json=body)

    response = await limited_client.post("/api/v1/auth/login", json=body)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) > 0


async def test_registration_shares_the_authentication_budget(limited_app, limited_client):
    """Both are unauthenticated attempts from one address, and a script that
    cannot log in should not be able to make accounts instead."""
    limited_app.dependency_overrides[get_auth_service] = RefusingAuth
    body = {"email": "someone@example.com", "password": "a-very-long-password-1"}
    for _ in range(3):
        await limited_client.post("/api/v1/auth/login", json=body)

    response = await limited_client.post(
        "/api/v1/auth/register",
        json={
            "email": "new@example.com",
            "password": "a-very-long-password-1",
            "workspace_name": "New",
            "workspace_slug": "new",
        },
    )

    assert response.status_code == 429


# --------------------------------------------------------------- workspaces


async def test_a_workspace_is_limited_across_its_routes(limited_app, limited_client):
    limited_app.dependency_overrides[get_active_workspace] = lambda: _workspace(TENANT_ID)
    limited_app.dependency_overrides[get_agent_service] = StubAgents

    statuses = [(await limited_client.get("/api/v1/agents")).status_code for _ in range(5)]

    assert statuses == [200, 200, 200, 200, 429]


async def test_one_workspace_cannot_exhaust_anothers_budget(limited_app, limited_client):
    """Counted per workspace, so a busy customer cannot refuse a quiet one."""
    limited_app.dependency_overrides[get_agent_service] = StubAgents
    limited_app.dependency_overrides[get_active_workspace] = lambda: _workspace(TENANT_ID)
    for _ in range(5):
        await limited_client.get("/api/v1/agents")

    limited_app.dependency_overrides[get_active_workspace] = lambda: _workspace(OTHER_TENANT_ID)
    response = await limited_client.get("/api/v1/agents")

    assert response.status_code == 200


async def test_an_unauthenticated_request_is_refused_for_being_unauthenticated(
    limited_app,
    limited_client,
):
    """Not for being frequent: the workspace guard resolves the caller first,
    and 401 is the honest answer."""
    for _ in range(6):
        response = await limited_client.get("/api/v1/agents")

    assert response.status_code == 401


# ------------------------------------------------------------- the webhook


async def test_the_whatsapp_webhook_is_never_rate_limited(limited_app, limited_client):
    """The most important assertion in this file.

    Meta retries anything that is not a 2xx and eventually disables a
    subscription that keeps failing. A 429 here would lose a customer's message
    and then the integration.
    """
    statuses = []
    for _ in range(20):
        response = await limited_client.post(
            "/api/v1/webhooks/whatsapp",
            json={"entry": []},
            headers={"X-Hub-Signature-256": "sha256=unverified"},
        )
        statuses.append(response.status_code)

    assert 429 not in statuses
    # Every answer is the same one the first request got: nothing degrades with
    # repetition.
    assert len(set(statuses)) == 1


async def test_the_webhook_verification_handshake_is_never_limited(
    limited_app,
    limited_client,
):
    """Meta re-verifies a subscription, and a refusal there breaks the
    connection at exactly the moment somebody is setting it up."""
    statuses = []
    for _ in range(20):
        response = await limited_client.get(
            "/api/v1/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
        )
        statuses.append(response.status_code)

    assert 429 not in statuses


# ------------------------------------------------------------- switched off


async def test_limiting_can_be_switched_off(client, app):
    """The default in this suite: a limiter counting across a file makes every
    test in it order-dependent."""
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TENANT_ID)
    app.dependency_overrides[get_agent_service] = StubAgents

    statuses = [(await client.get("/api/v1/agents")).status_code for _ in range(30)]

    assert set(statuses) == {200}


async def test_logout_shares_the_authentication_budget(limited_app, limited_client):
    """Logout is limited rather than authenticated (ADR-040).

    Requiring an access token would break it exactly when people use it - the
    access token has expired, which is why they are signing out - and would add
    nothing against the adversary it looks like it guards: somebody holding a
    victim's refresh token can exchange it for a live session, which is strictly
    worse than revoking it.

    What was missing is a budget. The endpoint verifies a JWT signature for any
    caller, so unlimited it is free signature work for anybody who asks.
    """
    limited_app.dependency_overrides[get_auth_service] = RefusingAuth
    body = {"email": "someone@example.com", "password": "a-very-long-password-1"}
    for _ in range(3):
        await limited_client.post("/api/v1/auth/login", json=body)

    refused = await limited_client.post(
        "/api/v1/auth/logout", json={"refresh_token": "anything-at-all"}
    )

    assert refused.status_code == 429


async def test_logout_needs_no_credential_beyond_the_token_itself(client, app):
    """The property that must survive the limit: no authentication.

    A caller whose access token has already expired must still be able to revoke
    their refresh token, and a token that is already spent, revoked or invalid
    is not an error - answering differently would make this an oracle for
    whether a token is still live.
    """
    for token in ("not-a-real-token", "", "x" * 40):
        response = await client.post("/api/v1/auth/logout", json={"refresh_token": token})
        assert response.status_code in (204, 422), token
