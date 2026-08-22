"""The template registry's HTTP contract.

Two things are worth asserting at this level. The role boundary — reading is
open to any member, syncing is not — and the absence of any route that writes a
template. Approval belongs to Meta, and an endpoint that created one locally
would be inventing permission the platform does not have.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_template_service,
)
from app.db.models import (
    Membership,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
)
from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)
from app.services.template_service import SyncOutcome

pytestmark = pytest.mark.integration

PATH = "/api/v1/templates"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
ACCOUNT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
TEMPLATE_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")
MOMENT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _template() -> WhatsAppTemplate:
    return WhatsAppTemplate(
        id=TEMPLATE_ID,
        tenant_id=TENANT_ID,
        account_id=ACCOUNT_ID,
        meta_template_id="meta-1",
        name="order_update",
        language="ar_EG",
        category=TemplateCategory.MARKETING,
        status=TemplateStatus.APPROVED,
        body_text="Hello {{1}}",
        components=[{"type": "BODY", "text": "Hello {{1}}"}],
        variable_count=1,
        quality_rating="GREEN",
        rejection_reason=None,
        synced_at=MOMENT,
        created_at=MOMENT,
        updated_at=MOMENT,
    )


class StubTemplates:
    """Records what the routes asked for."""

    def __init__(self) -> None:
        self.list_calls: list[dict] = []
        self.sync_calls: list[uuid.UUID] = []

    async def list_templates(self, *, account_id=None, status=None, category=None, limit=100):
        self.list_calls.append({"account_id": account_id, "status": status, "category": category})
        return [_template()]

    async def get(self, template_id: uuid.UUID):
        return _template()

    async def sync(self, account_id: uuid.UUID) -> SyncOutcome:
        self.sync_calls.append(account_id)
        return SyncOutcome(account_id=account_id, created=2, updated=1, withdrawn=0)


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="member@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            role=role,
        ),
        tenant=Tenant(
            id=TENANT_ID,
            name="Acme",
            slug="acme",
            status=TenantStatus.ACTIVE,
        ),
    )


@pytest.fixture
def templates(app) -> StubTemplates:
    stub = StubTemplates()
    app.dependency_overrides[get_template_service] = lambda: stub
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.TENANT_ADMIN)
    return stub


@pytest.fixture
def as_member(app) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.MEMBER)


async def test_a_member_can_read_the_registry(client, templates, as_member):
    response = await client.get(PATH)

    assert response.status_code == 200
    template = response.json()["templates"][0]
    assert template["name"] == "order_update"
    assert template["status"] == "approved"
    assert template["variable_count"] == 1
    assert template["body_text"] == "Hello {{1}}"


async def test_the_filters_reach_the_service(client, templates):
    await client.get(PATH, params={"status": "approved", "category": "marketing"})

    call = templates.list_calls[0]
    assert call["status"] is TemplateStatus.APPROVED
    assert call["category"] is TemplateCategory.MARKETING


async def test_a_status_that_is_not_ours_is_refused(client, templates):
    response = await client.get(PATH, params={"status": "almost_approved"})

    assert response.status_code == 422
    assert templates.list_calls == []


async def test_one_template_can_be_read_on_its_own(client, templates):
    response = await client.get(f"{PATH}/{TEMPLATE_ID}")

    assert response.status_code == 200
    assert response.json()["id"] == str(TEMPLATE_ID)


async def test_an_admin_can_sync(client, templates):
    response = await client.post(PATH + "/sync", params={"account_id": str(ACCOUNT_ID)})

    assert response.status_code == 200
    assert response.json() == {
        "account_id": str(ACCOUNT_ID),
        "created": 2,
        "updated": 1,
        "withdrawn": 0,
    }
    assert templates.sync_calls == [ACCOUNT_ID]


async def test_a_member_cannot_sync(client, templates, as_member):
    """Syncing calls Meta and rewrites the registry, so it takes an admin."""
    response = await client.post(PATH + "/sync", params={"account_id": str(ACCOUNT_ID)})

    assert response.status_code == 403
    assert templates.sync_calls == []


async def test_there_is_no_way_to_create_a_template(client, templates):
    """Approval belongs to Meta. A local one would be a fiction."""
    response = await client.post(PATH, json={"name": "invented", "language": "ar_EG"})

    assert response.status_code == 405
