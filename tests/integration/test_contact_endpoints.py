"""The opt-out endpoints, and the asymmetry in who may use them.

Recording an opt-out is any member's to do: the person handling the conversation
is the one a customer says "stop sending me these" to, and sending them to find
an administrator first is how the request gets lost. Clearing one takes an
administrator, because undoing somebody's own refusal should be deliberate.

Both name a channel, and the read answers channel by channel (ENT-19): a STOP
on WhatsApp is not a STOP on Instagram.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_campaign_service,
)
from app.db.models import (
    Membership,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
)
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel import (
    Channel,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
    IdentitySource,
)
from app.db.models.consent import ContactChannelConsent
from app.db.models.conversation import Contact

pytestmark = pytest.mark.integration

PATH = "/api/v1/contacts"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CONTACT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
MOMENT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


# Synthetic identifiers, not anybody's: a reserved-looking phone and a
# business-scoped id in Meta's documented shape.
PHONE = "201000000001"
BSUID = "EG.1a2b3c4d5e6f"


def _identity(
    kind: IdentityKind, value: str, channel: Channel = Channel.WHATSAPP
) -> ContactIdentity:
    return ContactIdentity(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        contact_id=CONTACT_ID,
        channel=channel,
        kind=kind,
        scope=(
            IdentityScope.WORKSPACE
            if kind is IdentityKind.PHONE
            else IdentityScope.PROVIDER_ACCOUNT
        ),
        scope_ref="" if kind is IdentityKind.PHONE else "waba-synthetic",
        value=value,
        source=IdentitySource.PROVIDER,
    )


class StubCampaigns:
    """The service's opt-out surface, keeping one consent row per channel."""

    def __init__(self, *, wa_id: str | None = PHONE) -> None:
        self.set_calls: list[dict[str, Any]] = []
        self.cleared: list[tuple[uuid.UUID, Channel]] = []
        self.wa_id = wa_id
        self.identities = [_identity(IdentityKind.BSUID, BSUID)]
        if wa_id is not None:
            self.identities.insert(0, _identity(IdentityKind.PHONE, wa_id))
        self.consents: dict[Channel, ContactChannelConsent] = {}

    async def opt_out_view(
        self, contact_id: uuid.UUID
    ) -> tuple[list[ContactIdentity], list[ContactChannelConsent]]:
        return self.identities, list(self.consents.values())

    def _contact(self) -> Contact:
        return Contact(id=CONTACT_ID, tenant_id=TENANT_ID, wa_id=self.wa_id, display_name="Nour")

    async def set_opt_out(
        self,
        *,
        contact_id: uuid.UUID,
        channel: Channel,
        source: OptOutSource,
        at: datetime | None = None,
    ) -> Contact:
        self.set_calls.append({"contact_id": contact_id, "channel": channel, "source": source})
        self.consents[channel] = ContactChannelConsent(
            tenant_id=TENANT_ID,
            contact_id=contact_id,
            channel=channel,
            marketing_opt_out_at=MOMENT,
            opt_out_source=source,
            opt_out_via=OptOutVia.TEAM,
        )
        return self._contact()

    async def clear_opt_out(self, contact_id: uuid.UUID, *, channel: Channel) -> Contact:
        self.cleared.append((contact_id, channel))
        self.consents[channel] = ContactChannelConsent(
            tenant_id=TENANT_ID, contact_id=contact_id, channel=channel, resumed_at=MOMENT
        )
        return self._contact()


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="member@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            role=role,
        ),
        tenant=Tenant(id=TENANT_ID, name="Acme", slug="acme", status=TenantStatus.ACTIVE),
    )


@pytest.fixture
def campaigns(app: FastAPI) -> StubCampaigns:
    stub = StubCampaigns()
    app.dependency_overrides[get_campaign_service] = lambda: stub
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.MEMBER)
    return stub


@pytest.fixture
def as_admin(app: FastAPI) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.TENANT_ADMIN)


WHATSAPP = {"channel": "whatsapp"}


def _entry(body: dict[str, Any], channel: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = [
        item for item in body["channels"] if item["channel"] == channel
    ]
    [entry] = entries
    return entry


async def test_a_member_can_record_an_opt_out(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    response = await client.post(f"{PATH}/{CONTACT_ID}/opt-out", json=WHATSAPP)

    assert response.status_code == 200
    whatsapp = _entry(response.json(), "whatsapp")
    assert whatsapp["opted_out"] is True
    assert whatsapp["marketing_opt_out_at"] is not None
    assert whatsapp["opt_out_source"] == "team"
    assert campaigns.set_calls[0]["source"] is OptOutSource.TEAM
    assert campaigns.set_calls[0]["channel"] is Channel.WHATSAPP


async def test_an_opt_out_must_name_its_channel(
    client: AsyncClient, campaigns: StubCampaigns, as_admin: None
) -> None:
    """ENT-19: there is no person-wide opt-out to record or to clear."""
    recorded = await client.post(f"{PATH}/{CONTACT_ID}/opt-out", json={})
    cleared = await client.delete(f"{PATH}/{CONTACT_ID}/opt-out")
    unknown = await client.post(f"{PATH}/{CONTACT_ID}/opt-out", json={"channel": "fax"})

    assert (recorded.status_code, cleared.status_code, unknown.status_code) == (422, 422, 422)
    assert campaigns.set_calls == [] and campaigns.cleared == []


async def test_the_source_can_say_the_customer_asked(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    await client.post(f"{PATH}/{CONTACT_ID}/opt-out", json={**WHATSAPP, "source": "customer"})

    assert campaigns.set_calls[0]["source"] is OptOutSource.CUSTOMER


async def test_a_source_that_is_not_ours_is_refused(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    response = await client.post(
        f"{PATH}/{CONTACT_ID}/opt-out", json={**WHATSAPP, "source": "vibes"}
    )

    assert response.status_code == 422
    assert campaigns.set_calls == []


async def test_a_member_cannot_undo_a_customers_refusal(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    response = await client.delete(f"{PATH}/{CONTACT_ID}/opt-out", params=WHATSAPP)

    assert response.status_code == 403
    assert campaigns.cleared == []


async def test_an_admin_can_clear_one_recorded_in_error(
    client: AsyncClient, campaigns: StubCampaigns, as_admin: None
) -> None:
    response = await client.delete(f"{PATH}/{CONTACT_ID}/opt-out", params=WHATSAPP)

    assert response.status_code == 200
    whatsapp = _entry(response.json(), "whatsapp")
    assert (whatsapp["opted_out"], whatsapp["marketing_opt_out_at"]) == (False, None)
    assert whatsapp["resumed_at"] is not None
    assert campaigns.cleared == [(CONTACT_ID, Channel.WHATSAPP)]


async def test_an_opt_out_lists_the_identities_of_its_own_channel(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    """ENT-19: a WhatsApp opt-out covers each way WhatsApp addresses the person -
    phone and business-scoped id - and nothing on Instagram, which is listed
    under its own channel and stays reachable."""
    campaigns.identities.append(_identity(IdentityKind.IGSID, "igsid-1", Channel.INSTAGRAM))
    response = await client.post(f"{PATH}/{CONTACT_ID}/opt-out", json=WHATSAPP)

    body = response.json()
    assert body["wa_id"] == PHONE
    assert [entry["channel"] for entry in body["channels"]] == ["whatsapp", "instagram"]
    whatsapp, instagram = _entry(body, "whatsapp"), _entry(body, "instagram")
    assert [(entry["kind"], entry["value"]) for entry in whatsapp["identities"]] == [
        ("phone", PHONE),
        ("bsuid", BSUID),
    ]
    assert whatsapp["opted_out"] is True
    assert [(entry["kind"], entry["value"]) for entry in instagram["identities"]] == [
        ("igsid", "igsid-1")
    ]
    assert (instagram["opted_out"], instagram["marketing_opt_out_at"]) == (False, None)
    assert len(body["identities"]) == 3


async def test_a_customer_known_only_by_username_can_be_opted_out(
    app: FastAPI, client: AsyncClient
) -> None:
    """OMNI-002: a username sender has no phone. The read used to require
    `wa_id`, so opting such a customer out would have failed to serialise."""
    stub = StubCampaigns(wa_id=None)
    app.dependency_overrides[get_campaign_service] = lambda: stub
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.MEMBER)

    response = await client.post(f"{PATH}/{CONTACT_ID}/opt-out", json=WHATSAPP)

    assert response.status_code == 200
    body = response.json()
    assert body["wa_id"] is None
    assert [(entry["kind"], entry["value"]) for entry in body["identities"]] == [("bsuid", BSUID)]
