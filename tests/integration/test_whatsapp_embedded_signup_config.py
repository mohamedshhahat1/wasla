"""GET /whatsapp/embedded-signup/config and the enabled rule (ADR-204, CF-03 .. CF-05).

What the browser needs to open Meta's popup - and never the app secret, the
verify token, a token or the credential key; `enabled` only when every piece an
onboarding needs is set (FEB-48: the route had no direct test).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_whatsapp_onboarding_service,
)
from app.core.config import Settings
from app.core.crypto import generate_key
from app.core.dependencies import get_settings_from_state
from app.db.models import (
    Membership,
    MembershipStatus,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
)
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

URL = "/api/v1/whatsapp/embedded-signup/config"
SECRET = "es-app-secret-NEVER-RETURNED"
VERIFY = "es-verify-token-NEVER-RETURNED"
KEY = generate_key()


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "meta_app_id": "1234567890",
        "meta_app_secret": SECRET,
        "meta_verify_token": VERIFY,
        "meta_embedded_signup_config_id": "es-config-1",
        "credential_encryption_keys": [KEY],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@dataclass
class StubOnboarding:
    enabled: bool


def _workspace(role: TenantRole) -> ActiveWorkspace:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    return ActiveWorkspace(
        user=User(id=user_id, email="owner@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=user_id,
            tenant_id=tenant_id,
            role=role,
            status=MembershipStatus.ACTIVE,
        ),
        tenant=Tenant(
            timezone=TEST_TIMEZONE,
            id=tenant_id,
            name="Acme",
            slug="acme",
            status=TenantStatus.ACTIVE,
        ),
    )


def _use(app: FastAPI, settings: Settings, role: TenantRole = TenantRole.TENANT_OWNER) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(role)
    app.dependency_overrides[get_settings_from_state] = lambda: settings
    app.dependency_overrides[get_whatsapp_onboarding_service] = lambda: StubOnboarding(
        enabled=settings.embedded_signup_enabled
    )


@pytest.mark.parametrize(
    "missing",
    [
        "meta_app_id",
        "meta_app_secret",
        "meta_embedded_signup_config_id",
        "credential_encryption_keys",
        "meta_verify_token",
    ],
)
def test_cf04_enabled_needs_every_piece_an_onboarding_needs(missing: str) -> None:
    empty: object = [] if missing == "credential_encryption_keys" else None
    assert _settings().embedded_signup_enabled is True
    assert _settings(**{missing: empty}).embedded_signup_enabled is False


async def test_cf03_an_owner_reads_what_the_popup_needs_and_nothing_secret(
    app: FastAPI, client: AsyncClient
) -> None:
    _use(app, _settings())
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "app_id": "1234567890",
        "config_id": "es-config-1",
        "es_version": "v5",
        "graph_version": "v27.0",
        "coexistence": True,
    }
    for secret in (SECRET, VERIFY, KEY):
        assert secret not in response.text
    assert response.headers["cache-control"] == "private"


async def test_cf04_a_deployment_that_cannot_finish_says_only_disabled(
    app: FastAPI, client: AsyncClient
) -> None:
    _use(app, _settings(meta_embedded_signup_config_id=None))
    response = await client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert {key for key, value in body.items() if value is not None} == {"enabled"}


async def test_cf03_coexistence_switched_off_is_said(app: FastAPI, client: AsyncClient) -> None:
    _use(app, _settings(meta_embedded_signup_coexistence=False))
    assert (await client.get(URL)).json()["coexistence"] is False


async def test_cf03_a_member_cannot_read_it(app: FastAPI, client: AsyncClient) -> None:
    _use(app, _settings(), TenantRole.MEMBER)
    response = await client.get(URL)
    assert response.status_code == 403
