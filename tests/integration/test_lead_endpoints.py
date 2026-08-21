"""The lead endpoints, and the role boundary drawn across them.

The workspace dependency is overridden, but the real role guard runs: a member
being refused an assignment is asserted against the actual wiring rather than a
mock of it.

Most of the CRM is open to any member, because the people working a pipeline
have to be able to work it. Assignment and the workspace-wide statistics view
are management actions and require an administrator.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_lead_service,
)
from app.core.exceptions import ConflictError, TenantIsolationError, ValidationError
from app.core.pagination import Page
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.lead import (
    ActorKind,
    Lead,
    LeadActivity,
    LeadActivityKind,
    LeadNote,
    LeadSource,
    LeadStatus,
)
from app.repositories.lead_repository import LeadStatistics

pytestmark = pytest.mark.integration

PATH = "/api/v1/leads"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
LEAD_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
MOMENT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _lead(**overrides) -> Lead:
    values = {
        "id": LEAD_ID,
        "tenant_id": TENANT_ID,
        "contact_id": None,
        "conversation_id": None,
        "name": "Ahmed Hassan",
        "phone": None,
        "email": None,
        "interest": "Apartment finishing",
        "budget_amount": None,
        "budget_currency": None,
        "status": LeadStatus.NEW,
        "source": LeadSource.MANUAL,
        "score": 0,
        "assigned_to_id": None,
        "tags": [],
        "custom_fields": {},
        "human_verified_fields": ["name"],
        "qualified_at": None,
        "closed_at": None,
        "last_activity_at": MOMENT,
        "created_at": MOMENT,
        "updated_at": MOMENT,
    }
    values.update(overrides)
    return Lead(**values)


class StubLeads:
    """Records what it was asked to do and returns canned rows."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.assigned: list[dict] = []
        self.statuses: list[dict] = []
        self.scores: list[dict] = []
        self.notes: list[dict] = []
        self.missing = False
        self.conflict = False
        self.invalid = False
        self.lead = _lead()

    def _guard(self) -> None:
        if self.missing:
            raise TenantIsolationError()

    async def list_leads(self, *, filters=None, limit=50, cursor=None):
        self.filters = filters
        return Page(items=[self.lead], next_cursor=None)

    async def get_lead(self, lead_id):
        self._guard()
        return self.lead

    async def statistics(self, *, filters=None):
        return LeadStatistics(
            total=3,
            open_leads=2,
            unassigned=1,
            by_status={LeadStatus.NEW: 2, LeadStatus.WON: 1},
        )

    async def create_lead(self, **kwargs):
        if self.conflict:
            raise ConflictError("That customer already has an open lead.")
        self.created.append(kwargs)
        return self.lead

    async def update_lead(self, *, lead_id, actor_id, update):
        self._guard()
        self.updated.append({"lead_id": lead_id, "update": update})
        return self.lead

    async def change_status(self, *, lead_id, status, actor_id, reason=None):
        self._guard()
        if self.invalid:
            raise ValidationError("A lead cannot move from won to new.")
        self.statuses.append({"lead_id": lead_id, "status": status, "reason": reason})
        return _lead(status=status)

    async def assign(self, *, lead_id, assigned_to_id, actor_id):
        self._guard()
        self.assigned.append({"lead_id": lead_id, "assigned_to_id": assigned_to_id})
        return _lead(assigned_to_id=assigned_to_id)

    async def set_score(self, *, lead_id, score, actor_id):
        self._guard()
        self.scores.append({"lead_id": lead_id, "score": score})
        return _lead(score=score)

    async def add_note(self, *, lead_id, body, author_id, author_kind=ActorKind.USER):
        self._guard()
        self.notes.append({"lead_id": lead_id, "body": body})
        return LeadNote(
            id=uuid.uuid4(),
            tenant_id=TENANT_ID,
            lead_id=lead_id,
            author_id=author_id,
            author_kind=author_kind,
            body=body,
            created_at=MOMENT,
        )

    async def list_notes(self, *, lead_id, limit=50, cursor=None):
        self._guard()
        return Page(
            items=[
                LeadNote(
                    id=uuid.uuid4(),
                    tenant_id=TENANT_ID,
                    lead_id=lead_id,
                    author_id=USER_ID,
                    author_kind=ActorKind.USER,
                    body="Called, will call back.",
                    created_at=MOMENT,
                )
            ],
            next_cursor=None,
        )

    async def list_activity(self, *, lead_id, limit=50, cursor=None):
        self._guard()
        return Page(
            items=[
                LeadActivity(
                    id=uuid.uuid4(),
                    tenant_id=TENANT_ID,
                    lead_id=lead_id,
                    kind=LeadActivityKind.CREATED,
                    actor_id=USER_ID,
                    actor_kind=ActorKind.USER,
                    summary="Lead created.",
                    data={"source": "manual"},
                    created_at=MOMENT,
                )
            ],
            next_cursor=None,
        )


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
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
def leads(app) -> StubLeads:
    stub = StubLeads()
    app.dependency_overrides[get_lead_service] = lambda: stub
    return stub


def _as(app, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(role)


# ------------------------------------------------------------ open to any member


async def test_a_member_can_list_leads(client, app, leads):
    _as(app, TenantRole.MEMBER)

    response = await client.get(PATH)

    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "Ahmed Hassan"


async def test_a_member_can_create_a_lead(client, app, leads):
    _as(app, TenantRole.MEMBER)

    response = await client.post(PATH, json={"name": "Ahmed", "interest": "Finishing"})

    assert response.status_code == 201
    assert leads.created[0]["name"] == "Ahmed"


async def test_a_member_can_edit_a_lead(client, app, leads):
    _as(app, TenantRole.MEMBER)

    response = await client.patch(f"{PATH}/{LEAD_ID}", json={"interest": "Villa finishing"})

    assert response.status_code == 200
    assert leads.updated[0]["update"].interest == "Villa finishing"


async def test_a_member_can_move_a_lead_through_the_pipeline(client, app, leads):
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/{LEAD_ID}/status", json={"status": "contacted"})

    assert response.status_code == 200
    assert response.json()["status"] == "contacted"


async def test_a_member_can_write_a_note(client, app, leads):
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/{LEAD_ID}/notes", json={"body": "Wants a call."})

    assert response.status_code == 201
    assert leads.notes[0]["body"] == "Wants a call."


async def test_a_member_can_read_the_activity_trail(client, app, leads):
    _as(app, TenantRole.MEMBER)

    response = await client.get(f"{PATH}/{LEAD_ID}/activity")

    assert response.status_code == 200
    assert response.json()["items"][0]["kind"] == "created"


# ----------------------------------------------------------- administrators only


async def test_an_admin_can_assign_a_lead(client, app, leads):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.post(
        f"{PATH}/{LEAD_ID}/assignment",
        json={"assigned_to_id": str(USER_ID)},
    )

    assert response.status_code == 200
    assert leads.assigned[0]["assigned_to_id"] == USER_ID


async def test_a_member_cannot_assign_a_lead(client, app, leads):
    """Handing someone a deal is a management decision, unlike inbox triage."""
    _as(app, TenantRole.MEMBER)

    response = await client.post(
        f"{PATH}/{LEAD_ID}/assignment",
        json={"assigned_to_id": str(USER_ID)},
    )

    assert response.status_code == 403
    assert leads.assigned == []


async def test_an_admin_can_read_pipeline_statistics(client, app, leads):
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.get(f"{PATH}/statistics")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    # Every status is named, including the ones at zero, so a dashboard need not
    # know the vocabulary to render an empty column.
    assert body["by_status"]["lost"] == 0


async def test_a_member_cannot_read_pipeline_statistics(client, app, leads):
    _as(app, TenantRole.MEMBER)

    response = await client.get(f"{PATH}/statistics")

    assert response.status_code == 403


async def test_an_owner_has_administrator_rights(client, app, leads):
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.get(f"{PATH}/statistics")

    assert response.status_code == 200


# --------------------------------------------------------------- error mapping


async def test_another_workspaces_lead_answers_not_found(client, app, leads):
    """Never 403: that would confirm the lead exists somewhere."""
    _as(app, TenantRole.MEMBER)
    leads.missing = True

    response = await client.get(f"{PATH}/{LEAD_ID}")

    assert response.status_code == 404


async def test_a_duplicate_open_lead_answers_conflict(client, app, leads):
    _as(app, TenantRole.MEMBER)
    leads.conflict = True

    response = await client.post(PATH, json={"contact_id": str(uuid.uuid4())})

    assert response.status_code == 409


async def test_an_illegal_transition_answers_unprocessable(client, app, leads):
    _as(app, TenantRole.MEMBER)
    leads.invalid = True

    response = await client.post(f"{PATH}/{LEAD_ID}/status", json={"status": "new"})

    assert response.status_code == 422


async def test_an_unknown_field_is_refused(client, app, leads):
    """`extra="forbid"`: a misspelled field must not be silently dropped."""
    _as(app, TenantRole.MEMBER)

    response = await client.post(PATH, json={"name": "Ahmed", "budgt": 500000})

    assert response.status_code == 422
    assert leads.created == []


async def test_a_score_outside_its_bounds_is_refused_at_the_edge(client, app, leads):
    """Clamping is for model output; a person's typo deserves an error."""
    _as(app, TenantRole.MEMBER)

    response = await client.post(f"{PATH}/{LEAD_ID}/score", json={"score": 900})

    assert response.status_code == 422
    assert leads.scores == []


async def test_authentication_is_required(client):
    """Deliberately takes neither fixture.

    Overriding the service dependency would satisfy the route without ever
    resolving the workspace, so the token check would never run and this would
    pass against nothing. With the real providers in place the request has to
    get past `get_current_user` first, and with no bearer header it does not.
    """
    response = await client.get(PATH)

    assert response.status_code == 401


# -------------------------------------------------------------------- filtering


async def test_filters_reach_the_service(client, app, leads):
    _as(app, TenantRole.MEMBER)

    response = await client.get(
        PATH,
        params={"status": ["new", "contacted"], "unassigned": "true", "tag": ["Hot"]},
    )

    assert response.status_code == 200
    assert leads.filters.statuses == (LeadStatus.NEW, LeadStatus.CONTACTED)
    assert leads.filters.unassigned_only is True
    # Normalised on the way in, so a filter matches what the service stored.
    assert leads.filters.tags == ("hot",)


async def test_an_unknown_status_filter_is_refused(client, app, leads):
    _as(app, TenantRole.MEMBER)

    response = await client.get(PATH, params={"status": "prospecting"})

    assert response.status_code == 422
