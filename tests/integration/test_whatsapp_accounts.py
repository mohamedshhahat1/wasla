"""The account connection endpoints.

The workspace dependency is overridden, but the real role guard runs: a member
being refused a connect is asserted against the actual wiring, not a mock of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_whatsapp_account_service,
)
from app.core.exceptions import ConflictError, TenantIsolationError
from app.db.models import (
    Membership,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
    WhatsAppAccount,
    WhatsAppAccountStatus,
)

pytestmark = pytest.mark.integration

PATH = "/api/v1/whatsapp/accounts"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
ACCOUNT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
PHONE_NUMBER_ID = "109876543210"
CONNECT_BODY = {
    "phone_number_id": PHONE_NUMBER_ID,
    "waba_id": "102030405060",
    "display_phone_number": "+20 100 000 0000",
    "display_name": "Acme Support",
}


def _account(**overrides) -> WhatsAppAccount:
    values = {
        "id": ACCOUNT_ID,
        "tenant_id": TENANT_ID,
        "phone_number_id": PHONE_NUMBER_ID,
        "waba_id": "102030405060",
        "display_phone_number": "+20 100 000 0000",
        "display_name": "Acme Support",
        "status": WhatsAppAccountStatus.ACTIVE,
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 21, tzinfo=UTC),
    }
    values.update(overrides)
    return WhatsAppAccount(**values)


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@wasla.test", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            role=role,
            can_administer_tenant=role is not TenantRole.MEMBER,
        ),
        tenant=Tenant(
            id=TENANT_ID,
            name="Acme",
            slug="acme",
            status=TenantStatus.ACTIVE,
        ),
    )


class StubAccounts:
    def __init__(self):
        self.connect_calls = []
        self.status_calls = []
        self.listed = []
        self.conflict = False
        self.missing = False

    async def connect(self, **kwargs):
        if self.conflict:
            raise ConflictError("That WhatsApp number is already connected.")
        self.connect_calls.append(kwargs)
        return _account(
            tenant_id=kwargs["tenant_id"],
            phone_number_id=kwargs["phone_number_id"],
        )

    async def list_accounts(self, **kwargs):
        self.listed.append(kwargs)
        return [_account()]

    async def set_status(self, **kwargs):
        if self.missing:
            raise TenantIsolationError("That WhatsApp account could not be found.")
        self.status_calls.append(kwargs)
        return _account(status=kwargs["status"])


@pytest.fixture
def accounts(app) -> StubAccounts:
    stub = StubAccounts()
    app.dependency_overrides[get_whatsapp_account_service] = lambda: stub
    return stub


def _as(app, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(role)


async def test_an_admin_can_connect_a_number(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(PATH, json=CONNECT_BODY)

    assert response.status_code == 201
    body = response.json()
    assert body["phone_number_id"] == PHONE_NUMBER_ID
    assert body["status"] == "active"
    # The workspace came from the session, not from the request body.
    assert accounts.connect_calls[0]["tenant_id"] == TENANT_ID


async def test_a_member_cannot_connect_a_number(app, client, accounts):
    _as(app, TenantRole.MEMBER)

    response = await client.post(PATH, json=CONNECT_BODY)

    assert response.status_code == 403
    assert accounts.connect_calls == []


async def test_an_owner_can_connect_a_number(app, client, accounts):
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.post(PATH, json=CONNECT_BODY)

    assert response.status_code == 201


async def test_a_duplicate_number_is_a_conflict_without_naming_the_holder(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)
    accounts.conflict = True

    response = await client.post(PATH, json=CONNECT_BODY)

    assert response.status_code == 409
    # The number may be held by a workspace the caller cannot see.
    assert "workspace" not in response.text.lower()


async def test_an_unknown_field_is_rejected_rather_than_ignored(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(PATH, json={**CONNECT_BODY, "tenant_id": str(OTHER_TENANT_ID)})

    assert response.status_code == 422
    assert accounts.connect_calls == []


async def test_a_member_can_list_connected_numbers(app, client, accounts):
    _as(app, TenantRole.MEMBER)

    response = await client.get(PATH)

    assert response.status_code == 200
    assert response.json()["accounts"][0]["phone_number_id"] == PHONE_NUMBER_ID
    assert accounts.listed[0]["tenant_id"] == TENANT_ID


async def test_disabling_an_account_sets_it_disabled(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/disable")

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert accounts.status_calls[0]["status"] is WhatsAppAccountStatus.DISABLED


async def test_enabling_an_account_sets_it_active(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/enable")

    assert response.status_code == 200
    assert accounts.status_calls[0]["status"] is WhatsAppAccountStatus.ACTIVE


async def test_a_member_cannot_disable_an_account(app, client, accounts):
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/disable")

    assert response.status_code == 403
    assert accounts.status_calls == []


async def test_another_workspaces_account_is_not_found(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)
    accounts.missing = True

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/disable")

    # Scoped lookup: it looks missing rather than forbidden.
    assert response.status_code == 404


async def test_a_malformed_account_id_is_rejected(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/not-a-uuid/disable")

    assert response.status_code == 422
    assert accounts.status_calls == []
