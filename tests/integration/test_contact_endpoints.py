"""The opt-out endpoints, and the asymmetry in who may use them.

Recording an opt-out is any member's to do: the person handling the conversation
is the one a customer says "stop sending me these" to, and sending them to find
an administrator first is how the request gets lost. Clearing one takes an
administrator, because undoing somebody's own refusal should be deliberate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

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
from app.db.models.campaign import OptOutSource
from app.db.models.conversation import Contact

pytestmark = pytest.mark.integration

PATH = "/api/v1/contacts"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CONTACT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
MOMENT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


class StubCampaigns:
    def __init__(self) -> None:
        self.set_calls: list[dict] = []
        self.cleared: list[uuid.UUID] = []

    def _contact(self, *, opted_out: bool, source: OptOutSource | None = None) -> Contact:
        return Contact(
            id=CONTACT_ID,
            tenant_id=TENANT_ID,
            wa_id="201234567890",
            display_name="Nour",
            marketing_opt_out_at=MOMENT if opted_out else None,
            opt_out_source=source,
        )

    async def set_opt_out(self, *, contact_id, source, at=None):
        self.set_calls.append({"contact_id": contact_id, "source": source})
        return self._contact(opted_out=True, source=source)

    async def clear_opt_out(self, contact_id):
        self.cleared.append(contact_id)
        return self._contact(opted_out=False)


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
def campaigns(app) -> StubCampaigns:
    stub = StubCampaigns()
    app.dependency_overrides[get_campaign_service] = lambda: stub
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.MEMBER)
    return stub


@pytest.fixture
def as_admin(app) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.TENANT_ADMIN)


async def test_a_member_can_record_an_opt_out(client, campaigns):
    response = await client.post(f"{PATH}/{CONTACT_ID}/opt-out", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["marketing_opt_out_at"] is not None
    assert body["opt_out_source"] == "team"
    assert campaigns.set_calls[0]["source"] is OptOutSource.TEAM


async def test_the_source_can_say_the_customer_asked(client, campaigns):
    await client.post(f"{PATH}/{CONTACT_ID}/opt-out", json={"source": "customer"})

    assert campaigns.set_calls[0]["source"] is OptOutSource.CUSTOMER


async def test_a_source_that_is_not_ours_is_refused(client, campaigns):
    response = await client.post(f"{PATH}/{CONTACT_ID}/opt-out", json={"source": "vibes"})

    assert response.status_code == 422
    assert campaigns.set_calls == []


async def test_a_member_cannot_undo_a_customers_refusal(client, campaigns):
    response = await client.delete(f"{PATH}/{CONTACT_ID}/opt-out")

    assert response.status_code == 403
    assert campaigns.cleared == []


async def test_an_admin_can_clear_one_recorded_in_error(client, campaigns, as_admin):
    response = await client.delete(f"{PATH}/{CONTACT_ID}/opt-out")

    assert response.status_code == 200
    assert response.json()["marketing_opt_out_at"] is None
    assert campaigns.cleared == [CONTACT_ID]
