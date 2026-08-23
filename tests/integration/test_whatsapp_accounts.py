"""The account connection endpoints.

The workspace dependency is overridden, but the real role guard runs: a member
being refused a connect is asserted against the actual wiring, not a mock of it.

Ownership proof is asserted here at the contract level - what the endpoint
accepts, what it refuses, and what it never says. The proof mechanism itself is
covered against a fake Graph API in `tests/unit/test_number_ownership.py`, and
end to end against a real database in `test_whatsapp_ownership.py`.
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
from app.core.exceptions import ConflictError, DependencyUnavailableError, TenantIsolationError
from app.db.models import (
    Membership,
    MembershipStatus,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
    WhatsAppAccount,
    WhatsAppAccountStatus,
)
from app.integrations.whatsapp.ownership import NumberOwnershipError

pytestmark = pytest.mark.integration

PATH = "/api/v1/whatsapp/accounts"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
ACCOUNT_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
PHONE_NUMBER_ID = "109876543210"
# The credential the claim is proven with. Not a real one, and the tests below
# assert it never comes back out.
TOKEN = "EAAG-a-token-that-never-appears-in-a-response"
CONNECT_BODY = {
    "phone_number_id": PHONE_NUMBER_ID,
    "access_token": TOKEN,
    "waba_id": "102030405060",
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
        "verified_name": "Acme Ltd",
        "status": WhatsAppAccountStatus.ACTIVE,
        "ownership_verified_at": datetime(2026, 8, 23, tzinfo=UTC),
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 21, tzinfo=UTC),
    }
    values.update(overrides)
    return WhatsAppAccount(**values)


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            role=role,
            status=MembershipStatus.ACTIVE,
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
        self.released = []
        self.verified = []
        self.listed = []
        self.conflict = False
        self.unverified = False
        self.no_verifier = False
        self.missing = False

    async def connect(self, **kwargs):
        if self.unverified:
            raise NumberOwnershipError()
        if self.no_verifier:
            raise DependencyUnavailableError(
                "WhatsApp number verification is not available in this deployment."
            )
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

    async def reverify(self, **kwargs):
        if self.missing:
            raise TenantIsolationError("That WhatsApp account could not be found.")
        if self.unverified:
            raise NumberOwnershipError()
        self.verified.append(kwargs)
        return _account()

    async def release(self, **kwargs):
        if self.missing:
            raise TenantIsolationError("That WhatsApp account could not be found.")
        self.released.append(kwargs)
        return _account(
            status=WhatsAppAccountStatus.RELEASED,
            released_at=datetime(2026, 8, 23, tzinfo=UTC),
        )


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


async def test_the_credential_reaches_the_service_and_comes_back_from_nowhere(
    app,
    client,
    accounts,
):
    """The whole point of the credential: it goes in, it proves the claim, and
    no response model anywhere can return it."""
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(PATH, json=CONNECT_BODY)

    assert accounts.connect_calls[0]["access_token"] == TOKEN
    assert TOKEN not in response.text
    assert "access_token" not in response.json()


async def test_a_connect_without_a_credential_is_rejected(app, client, accounts):
    """Fail closed. Without a credential there is no proof that this workspace
    controls this number, and the platform token deliberately does not count."""
    _as(app, TenantRole.TENANT_ADMIN)
    body = {key: value for key, value in CONNECT_BODY.items() if key != "access_token"}

    response = await client.post(PATH, json=body)

    assert response.status_code == 422
    assert accounts.connect_calls == []


async def test_an_empty_credential_is_rejected(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(PATH, json={**CONNECT_BODY, "access_token": ""})

    assert response.status_code == 422
    assert accounts.connect_calls == []


async def test_an_unprovable_claim_is_refused_without_saying_why(app, client, accounts):
    """One message for every cause. Distinguishing "no such number" from "your
    token cannot read it" would turn this into an oracle for mapping other
    businesses' numbers."""
    _as(app, TenantRole.TENANT_ADMIN)
    accounts.unverified = True

    response = await client.post(PATH, json=CONNECT_BODY)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "whatsapp_ownership_unverified"
    # The credential itself, and anything that would say *which* check failed.
    # The message may name the concept of an access token - it has to, to be
    # actionable - but never the value, and never Meta's own error text.
    text = response.text.lower()
    assert TOKEN.lower() not in text
    for leak in ("graph", "facebook", "oauth", "expired", "does not exist", "unsupported get"):
        assert leak not in text


async def test_a_deployment_without_verification_refuses_rather_than_trusting(
    app,
    client,
    accounts,
):
    """503, not 201. A deployment that cannot verify must not accept unproven
    claims, and this is our misconfiguration rather than the caller's mistake."""
    _as(app, TenantRole.TENANT_ADMIN)
    accounts.no_verifier = True

    response = await client.post(PATH, json=CONNECT_BODY)

    assert response.status_code == 503


async def test_the_waba_id_is_optional_because_meta_is_the_authority(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)
    body = {key: value for key, value in CONNECT_BODY.items() if key != "waba_id"}

    response = await client.post(PATH, json=body)

    assert response.status_code == 201
    assert accounts.connect_calls[0]["waba_id"] is None


async def test_the_display_number_cannot_be_supplied_by_the_caller(app, client, accounts):
    """It comes back from Meta during verification. Accepting one would let a
    workspace label somebody else's number however it liked."""
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(
        PATH,
        json={**CONNECT_BODY, "display_phone_number": "+20 999 999 9999"},
    )

    assert response.status_code == 422
    assert accounts.connect_calls == []


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
    account = response.json()["accounts"][0]
    assert account["phone_number_id"] == PHONE_NUMBER_ID
    assert account["verified_name"] == "Acme Ltd"
    assert account["ownership_verified_at"] is not None
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


async def test_releasing_an_account_gives_the_number_up(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/release")

    assert response.status_code == 200
    assert response.json()["status"] == "released"
    assert accounts.released[0]["tenant_id"] == TENANT_ID


async def test_a_member_cannot_release_a_number(app, client, accounts):
    """Handing a number back frees it for anybody on the platform to claim.
    That is administration, not day-to-day work."""
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/release")

    assert response.status_code == 403
    assert accounts.released == []


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


async def test_another_workspaces_account_cannot_be_released(app, client, accounts):
    """The dangerous half of the scoped lookup: releasing somebody else's
    number would free it for the caller to claim a moment later."""
    _as(app, TenantRole.TENANT_ADMIN)
    accounts.missing = True

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/release")

    assert response.status_code == 404
    assert accounts.released == []


async def test_a_malformed_account_id_is_rejected(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/not-a-uuid/disable")

    assert response.status_code == 422
    assert accounts.status_calls == []


async def test_an_admin_can_prove_a_number_they_already_hold(app, client, accounts):
    """The migration path for a number claimed before proof existed (ADR-041)."""
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(
        f"{PATH}/{ACCOUNT_ID}/verify",
        json={"access_token": TOKEN},
    )

    assert response.status_code == 200
    assert accounts.verified[0]["tenant_id"] == TENANT_ID
    assert accounts.verified[0]["access_token"] == TOKEN
    assert TOKEN not in response.text


async def test_the_number_cannot_be_named_when_verifying(app, client, accounts):
    """It comes from the row. Accepting one would make this a second way to
    claim a number rather than a way to prove one already held."""
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(
        f"{PATH}/{ACCOUNT_ID}/verify",
        json={"access_token": TOKEN, "phone_number_id": "somebody-elses-number"},
    )

    assert response.status_code == 422
    assert accounts.verified == []


async def test_a_member_cannot_verify_a_number(app, client, accounts):
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/verify", json={"access_token": TOKEN})

    assert response.status_code == 403
    assert accounts.verified == []


async def test_verifying_without_a_credential_is_rejected(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/verify", json={})

    assert response.status_code == 422
    assert accounts.verified == []


async def test_an_unprovable_verification_is_refused_without_saying_why(app, client, accounts):
    _as(app, TenantRole.TENANT_ADMIN)
    accounts.unverified = True

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/verify", json={"access_token": TOKEN})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "whatsapp_ownership_unverified"
    assert TOKEN.lower() not in response.text.lower()


async def test_another_workspaces_number_cannot_be_verified(app, client, accounts):
    """Stamping somebody else's row would be a claim about their traffic."""
    _as(app, TenantRole.TENANT_ADMIN)
    accounts.missing = True

    response = await client.post(f"{PATH}/{ACCOUNT_ID}/verify", json={"access_token": TOKEN})

    assert response.status_code == 404
    assert accounts.verified == []


async def test_the_listing_says_whether_a_number_is_proven(app, client, accounts):
    """The security state of a number is readable without reasoning about a
    null timestamp."""
    _as(app, TenantRole.MEMBER)

    response = await client.get(PATH)

    assert response.json()["accounts"][0]["ownership_verified"] is True
