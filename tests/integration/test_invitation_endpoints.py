"""Invitation endpoint tests.

The workspace resolver is overridden rather than the role guard, so the role
check itself runs: that is the part worth testing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI

from app.api.dependencies import ActiveWorkspace, get_active_workspace, get_invitation_service
from app.core.exceptions import AuthenticationError, ConflictError
from app.db.models import (
    InvitationStatus,
    Membership,
    Tenant,
    TenantInvitation,
    TenantRole,
    TenantStatus,
    User,
)
from app.services.invitation_service import AcceptedInvitation

pytestmark = pytest.mark.integration


def _workspace(role: TenantRole) -> ActiveWorkspace:
    user = User(id=uuid.uuid4(), email="admin@example.com", is_active=True)
    tenant = Tenant(id=uuid.uuid4(), name="Acme", slug="acme", status=TenantStatus.ACTIVE)
    membership = Membership(tenant_id=tenant.id, user_id=user.id, role=role)
    return ActiveWorkspace(user=user, membership=membership, tenant=tenant)


def _invitation(tenant_id: uuid.UUID) -> TenantInvitation:
    return TenantInvitation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        email="invited@example.com",
        role=TenantRole.MEMBER,
        status=InvitationStatus.PENDING,
        token_hash="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )


class StubInvitationService:
    # Named so a test can assert it is absent from a response body rather than
    # matching a literal in two places.
    token = "raw-invitation-token"

    def __init__(self, *, tenant_id):
        self.invitation = _invitation(tenant_id)
        self.calls = []

    async def issue(self, **kwargs):
        self.calls.append(("issue", kwargs))
        return self.invitation, self.token

    async def list_pending(self, **kwargs):
        self.calls.append(("list_pending", kwargs))
        return [self.invitation]

    async def revoke(self, **kwargs):
        self.calls.append(("revoke", kwargs))
        self.invitation.status = InvitationStatus.REVOKED
        return self.invitation

    async def accept(self, **kwargs):
        self.calls.append(("accept", kwargs))
        tenant = Tenant(
            id=self.invitation.tenant_id,
            name="Acme",
            slug="acme",
            status=TenantStatus.ACTIVE,
        )
        user = User(id=uuid.uuid4(), email="invited@example.com", is_active=True)
        membership = Membership(
            tenant_id=tenant.id,
            user_id=user.id,
            role=TenantRole.MEMBER,
        )
        return AcceptedInvitation(user=user, membership=membership, tenant=tenant)


@pytest.fixture
def owner(app: FastAPI) -> ActiveWorkspace:
    workspace = _workspace(TenantRole.TENANT_OWNER)
    app.dependency_overrides[get_active_workspace] = lambda: workspace
    return workspace


@pytest.fixture
def service(app: FastAPI, owner: ActiveWorkspace) -> StubInvitationService:
    stub = StubInvitationService(tenant_id=owner.tenant.id)
    app.dependency_overrides[get_invitation_service] = lambda: stub
    return stub


async def test_issuing_never_returns_the_token(client, service, owner):
    """The raw invitation token does not cross the API boundary (ADR-057).

    It used to, because there was no way to deliver it. There is now: `issue`
    queues it to the invited address through the outbox. A 201 body reaches
    proxy logs and browser captures, so a credential returned here is a
    credential published - and this one both joins a workspace and, until the
    accompanying fix, could claim a Google-only account.
    """
    response = await client.post(
        "/api/v1/invitations",
        json={"email": "invited@example.com", "role": "member"},
    )

    assert response.status_code == 201
    body = response.json()
    assert "token" not in body
    assert service.token not in response.text
    assert body["email"] == "invited@example.com"
    assert body["status"] == "pending"
    # The tenant comes from the resolved workspace, not from the request.
    assert service.calls[0][1]["tenant_id"] == owner.tenant.id


async def test_listing_omits_the_token(client, service, owner):
    response = await client.get("/api/v1/invitations")

    assert response.status_code == 200
    entries = response.json()
    assert len(entries) == 1
    assert "token" not in entries[0]


async def test_revoking_reports_the_new_status(client, service, owner):
    response = await client.delete(f"/api/v1/invitations/{service.invitation.id}")

    assert response.status_code == 200
    assert response.json()["status"] == "revoked"


async def test_a_member_cannot_invite_anybody(client, app: FastAPI, service):
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(TenantRole.MEMBER)

    response = await client.post(
        "/api/v1/invitations",
        json={"email": "invited@example.com"},
    )

    assert response.status_code == 403
    assert service.calls == []


async def test_inviting_requires_authentication(client, app: FastAPI):
    # No workspace override here: the real dependency chain runs and finds no
    # credentials.
    response = await client.post(
        "/api/v1/invitations",
        json={"email": "invited@example.com"},
    )

    assert response.status_code == 401


async def test_a_duplicate_invitation_conflicts(client, app: FastAPI, service, owner):
    class Conflicting(StubInvitationService):
        async def issue(self, **kwargs):
            raise ConflictError("That address already has a pending invitation.")

    app.dependency_overrides[get_invitation_service] = lambda: Conflicting(
        tenant_id=owner.tenant.id
    )

    response = await client.post(
        "/api/v1/invitations",
        json={"email": "invited@example.com"},
    )

    assert response.status_code == 409


async def test_accepting_needs_no_credentials(client, service):
    response = await client.post(
        "/api/v1/invitations/accept",
        json={"token": "raw-invitation-token", "password": "correct horse battery staple"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "invited@example.com"
    assert body["workspace"]["slug"] == "acme"
    # Acceptance prepares the membership; it does not hand out a session.
    assert "access_token" not in body


async def test_an_unusable_invitation_is_not_described(client, app: FastAPI, owner):
    class Rejecting(StubInvitationService):
        async def accept(self, **kwargs):
            raise AuthenticationError("That invitation is not valid.")

    app.dependency_overrides[get_invitation_service] = lambda: Rejecting(tenant_id=owner.tenant.id)

    response = await client.post(
        "/api/v1/invitations/accept",
        json={"token": "whatever"},
    )

    # Unknown, spent, revoked and expired all answer the same way.
    assert response.status_code == 401
