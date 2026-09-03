"""The campaign HTTP contract.

The role boundary is the point of this file. Reading a campaign is ordinary
inbox work; composing, targeting and starting one writes to thousands of
customers and is the least reversible thing the platform does, so all three take
an administrator.

The other thing asserted here is an absence: there is no field on any request
body that names a phone number, and no field that carries free text to send.
Both are what stop this API being a bulk-messaging tool.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
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
from app.core.pagination import Cursor, Page
from app.db.models import (
    Membership,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
)
from app.db.models.campaign import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    RecipientStatus,
)
from app.repositories.campaign_repository import AudienceFilter, CampaignStatistics

pytestmark = pytest.mark.integration

PATH = "/api/v1/campaigns"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
ACCOUNT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
TEMPLATE_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")
CAMPAIGN_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")
CONTACT_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
MOMENT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
NEXT_CURSOR = Cursor(sort_value=MOMENT, id=CAMPAIGN_ID).encode()


def _campaign(status: CampaignStatus = CampaignStatus.DRAFT) -> Campaign:
    return Campaign(
        id=CAMPAIGN_ID,
        tenant_id=TENANT_ID,
        account_id=ACCOUNT_ID,
        template_id=TEMPLATE_ID,
        name="Spring offer",
        description=None,
        # Set explicitly: a column default is applied at insert, and this row is
        # never inserted.
        status=status,
        variables=None,
        audience=None,
        audience_size=12,
        messages_per_minute=60,
        created_at=MOMENT,
        updated_at=MOMENT,
    )


def _recipient() -> CampaignRecipient:
    return CampaignRecipient(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        campaign_id=CAMPAIGN_ID,
        contact_id=CONTACT_ID,
        status=RecipientStatus.SENT,
        attempts=1,
        created_at=MOMENT,
        updated_at=MOMENT,
    )


class StubCampaigns:
    """Records what the routes asked for."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.audiences: list[dict[str, Any]] = []
        self.previews: list[dict[str, Any]] = []
        self.scheduled: list[dict[str, Any]] = []
        self.paused: list[uuid.UUID] = []
        self.cancelled: list[uuid.UUID] = []

    async def list_campaigns(
        self,
        *,
        statuses: Sequence[CampaignStatus] = (),
        account_id: uuid.UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Campaign]:
        return Page(items=[_campaign()], next_cursor=NEXT_CURSOR)

    async def get(self, campaign_id: uuid.UUID) -> Campaign:
        return _campaign()

    async def create(self, **kwargs: Any) -> Campaign:
        self.created.append(kwargs)
        return _campaign()

    async def preview_audience(self, *, account_id: uuid.UUID, filters: AudienceFilter) -> int:
        self.previews.append({"account_id": account_id, "filters": filters})
        return 7

    async def set_audience(
        self,
        *,
        campaign_id: uuid.UUID,
        filters: AudienceFilter,
    ) -> Campaign:
        self.audiences.append({"campaign_id": campaign_id, "filters": filters})
        return _campaign()

    async def schedule(
        self,
        *,
        campaign_id: uuid.UUID,
        scheduled_at: datetime | None = None,
        actor: User | None = None,
    ) -> Campaign:
        self.scheduled.append({"campaign_id": campaign_id, "scheduled_at": scheduled_at})
        return _campaign(CampaignStatus.SCHEDULED)

    async def pause(self, campaign_id: uuid.UUID) -> Campaign:
        self.paused.append(campaign_id)
        return _campaign(CampaignStatus.PAUSED)

    async def cancel(self, campaign_id: uuid.UUID, *, actor: User | None = None) -> Campaign:
        self.cancelled.append(campaign_id)
        return _campaign(CampaignStatus.CANCELLED)

    async def statistics(self, campaign_id: uuid.UUID) -> CampaignStatistics:
        return CampaignStatistics(pending=1, sent=2, failed=0, skipped=1, delivered=2, read=1)

    async def list_recipients(
        self,
        campaign_id: uuid.UUID,
        *,
        status: RecipientStatus | None = None,
        limit: int = 100,
    ) -> list[Any]:
        return [_recipient()]


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
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.TENANT_ADMIN)
    return stub


@pytest.fixture
def as_member(app: FastAPI) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.MEMBER)


# ---------------------------------------------------------------------- reads


async def test_the_campaign_list_answers_a_page(
    client: AsyncClient, campaigns: StubCampaigns, as_member: None
) -> None:
    response = await client.get(PATH)

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["items", "next_cursor"]
    assert body["items"][0]["name"] == "Spring offer"
    assert body["next_cursor"] == NEXT_CURSOR


async def test_a_member_can_read_one_campaign(
    client: AsyncClient, campaigns: StubCampaigns, as_member: None
) -> None:
    response = await client.get(f"{PATH}/{CAMPAIGN_ID}")

    assert response.status_code == 200
    assert response.json()["audience_size"] == 12


async def test_statistics_report_delivery_as_well_as_outcome(
    client: AsyncClient, campaigns: StubCampaigns, as_member: None
) -> None:
    body = (await client.get(f"{PATH}/{CAMPAIGN_ID}/statistics")).json()

    assert body == {
        "pending": 1,
        "sent": 2,
        "failed": 0,
        "skipped": 1,
        "delivered": 2,
        "read": 1,
        "total": 4,
    }


async def test_the_recipient_list_says_who_was_reached(
    client: AsyncClient, campaigns: StubCampaigns, as_member: None
) -> None:
    body = (await client.get(f"{PATH}/{CAMPAIGN_ID}/recipients")).json()

    assert body["recipients"][0]["status"] == "sent"
    assert body["recipients"][0]["contact_id"] == str(CONTACT_ID)


# -------------------------------------------------------------------- writes


async def test_an_admin_can_compose_a_campaign(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    response = await client.post(
        PATH,
        json={
            "account_id": str(ACCOUNT_ID),
            "template_id": str(TEMPLATE_ID),
            "name": "Spring offer",
            "variables": ["Ahmed"],
        },
    )

    assert response.status_code == 201
    assert campaigns.created[0]["name"] == "Spring offer"
    assert campaigns.created[0]["variables"] == ["Ahmed"]
    # Never from the request body.
    assert campaigns.created[0]["created_by_id"] == USER_ID


async def test_a_campaign_cannot_carry_free_text_to_send(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    """Meta accepts approved templates only outside the service window."""
    response = await client.post(
        PATH,
        json={
            "account_id": str(ACCOUNT_ID),
            "template_id": str(TEMPLATE_ID),
            "name": "Spring offer",
            "body": "Buy now!",
        },
    )

    assert response.status_code == 422
    assert campaigns.created == []


async def test_an_audience_cannot_name_a_phone_number(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    """The absence that stops this being a bulk-messaging tool."""
    response = await client.post(
        f"{PATH}/{CAMPAIGN_ID}/audience",
        json={"phone_numbers": ["201000000001"]},
    )

    assert response.status_code == 422
    assert campaigns.audiences == []


async def test_the_audience_filters_reach_the_service(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    await client.post(
        f"{PATH}/{CAMPAIGN_ID}/audience",
        json={"last_inbound_within_days": 30, "lead_statuses": ["qualified"]},
    )

    filters = campaigns.audiences[0]["filters"]
    assert filters.last_inbound_within_days == 30
    assert [status.value for status in filters.lead_statuses] == ["qualified"]


async def test_a_preview_counts_without_creating_anything(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    response = await client.post(
        f"{PATH}/audience/preview",
        json={"account_id": str(ACCOUNT_ID), "last_inbound_within_days": 7},
    )

    assert response.status_code == 200
    assert response.json()["size"] == 7
    assert campaigns.previews[0]["account_id"] == ACCOUNT_ID


async def test_scheduling_without_a_time_means_now(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    response = await client.post(f"{PATH}/{CAMPAIGN_ID}/schedule", json={})

    assert response.status_code == 200
    assert response.json()["status"] == "scheduled"
    assert campaigns.scheduled[0]["scheduled_at"] is None


async def test_pausing_and_cancelling_are_named_transitions(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    assert (await client.post(f"{PATH}/{CAMPAIGN_ID}/pause")).json()["status"] == "paused"
    assert (await client.post(f"{PATH}/{CAMPAIGN_ID}/cancel")).json()["status"] == "cancelled"
    assert campaigns.paused == [CAMPAIGN_ID]
    assert campaigns.cancelled == [CAMPAIGN_ID]


async def test_there_is_no_way_to_set_a_status_directly(
    client: AsyncClient, campaigns: StubCampaigns
) -> None:
    response = await client.patch(f"{PATH}/{CAMPAIGN_ID}", json={"status": "running"})

    assert response.status_code == 405


# ------------------------------------------------------------------ the roles


async def test_a_member_cannot_compose_a_campaign(
    client: AsyncClient, campaigns: StubCampaigns, as_member: None
) -> None:
    response = await client.post(
        PATH,
        json={
            "account_id": str(ACCOUNT_ID),
            "template_id": str(TEMPLATE_ID),
            "name": "Spring offer",
        },
    )

    assert response.status_code == 403
    assert campaigns.created == []


async def test_a_member_cannot_start_a_campaign(
    client: AsyncClient, campaigns: StubCampaigns, as_member: None
) -> None:
    response = await client.post(f"{PATH}/{CAMPAIGN_ID}/schedule", json={})

    assert response.status_code == 403
    assert campaigns.scheduled == []


async def test_a_member_cannot_change_the_audience(
    client: AsyncClient, campaigns: StubCampaigns, as_member: None
) -> None:
    response = await client.post(f"{PATH}/{CAMPAIGN_ID}/audience", json={})

    assert response.status_code == 403
    assert campaigns.audiences == []
