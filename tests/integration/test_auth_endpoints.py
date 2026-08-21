"""Authentication endpoint tests.

The service is stubbed here on purpose. What is under test is the HTTP surface:
status codes, response shape, and the authorization dependencies. The service's
own behaviour is covered by its unit tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI

from app.api.dependencies import CurrentUser, get_auth_service, get_current_user
from app.core.exceptions import AuthenticationError, TenantIsolationError
from app.core.security import TokenClaims, TokenType
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.services.auth_service import (
    AuthenticatedSession,
    WorkspaceAccess,
    WorkspaceContext,
)

pytestmark = pytest.mark.integration

REGISTRATION = {
    "email": "owner@wasla.test",
    "password": "correct horse battery staple",
    "full_name": "Owner",
    "workspace_name": "Acme",
    "workspace_slug": "acme",
}
CREDENTIALS = {
    "email": "owner@wasla.test",
    "password": "correct horse battery staple",
}


@pytest.fixture
def user() -> User:
    return User(
        id=uuid.uuid4(),
        email="owner@wasla.test",
        full_name="Owner",
        is_active=True,
    )


@pytest.fixture
def workspace(user: User) -> WorkspaceContext:
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Acme",
        slug="acme",
        status=TenantStatus.ACTIVE,
    )
    membership = Membership(
        tenant_id=tenant.id,
        user_id=user.id,
        role=TenantRole.TENANT_OWNER,
    )
    return WorkspaceContext(membership=membership, tenant=tenant)


class StubAuthService:
    """Records what it was asked to do and returns a canned session."""

    def __init__(self, *, user, workspace):
        self.user = user
        self.workspace = workspace
        self.calls = []

    def _session(self):
        return AuthenticatedSession(
            user=self.user,
            access_token="access-value",
            refresh_token="refresh-value",
            expires_in=900,
            workspace=self.workspace,
        )

    async def register(self, **kwargs):
        self.calls.append(("register", kwargs))
        return self._session()

    async def login(self, **kwargs):
        self.calls.append(("login", kwargs))
        return self._session()

    async def refresh(self, **kwargs):
        self.calls.append(("refresh", kwargs))
        return self._session()

    async def logout(self, **kwargs):
        self.calls.append(("logout", kwargs))

    async def select_workspace(self, *, user, workspace_slug):
        self.calls.append(("select_workspace", {"workspace_slug": workspace_slug}))
        return WorkspaceAccess(
            access_token="switched-value",
            expires_in=900,
            workspace=self.workspace,
        )

    async def list_workspaces(self, *, user):
        self.calls.append(("list_workspaces", {}))
        return [self.workspace]


@pytest.fixture
def service(app: FastAPI, user: User, workspace: WorkspaceContext) -> StubAuthService:
    stub = StubAuthService(user=user, workspace=workspace)
    app.dependency_overrides[get_auth_service] = lambda: stub
    return stub


@pytest.fixture
def authenticated(app: FastAPI, user: User, workspace: WorkspaceContext) -> CurrentUser:
    now = datetime.now(UTC)
    claims = TokenClaims(
        subject=user.id,
        token_type=TokenType.ACCESS,
        token_id=uuid.uuid4(),
        issued_at=now,
        expires_at=now + timedelta(minutes=15),
        tenant_id=workspace.tenant.id,
    )
    current = CurrentUser(user=user, claims=claims)
    app.dependency_overrides[get_current_user] = lambda: current
    return current


async def test_registration_returns_a_session_and_the_new_workspace(client, service):
    response = await client.post("/api/v1/auth/register", json=REGISTRATION)

    assert response.status_code == 201
    body = response.json()
    assert body["access_token"] == "access-value"
    assert body["refresh_token"] == "refresh-value"
    assert body["token_type"] == "bearer"
    assert body["active_workspace"]["slug"] == "acme"
    assert body["active_workspace"]["role"] == "tenant_owner"


async def test_a_short_password_never_reaches_the_service(client, service):
    response = await client.post(
        "/api/v1/auth/register",
        json={**REGISTRATION, "password": "short"},
    )

    assert response.status_code == 422
    assert service.calls == []


async def test_unknown_fields_are_rejected(client, service):
    # extra="forbid": a misspelled field must fail loudly, and a caller must not
    # be able to smuggle in a tenant id.
    response = await client.post(
        "/api/v1/auth/register",
        json={**REGISTRATION, "tenant_id": str(uuid.uuid4())},
    )

    assert response.status_code == 422
    assert service.calls == []


async def test_login_passes_the_requested_workspace_through(client, service):
    response = await client.post(
        "/api/v1/auth/login",
        json={**CREDENTIALS, "workspace_slug": "acme"},
    )

    assert response.status_code == 200
    assert service.calls[0][1]["workspace_slug"] == "acme"


async def test_a_failed_login_answers_401_through_the_error_envelope(
    client,
    app: FastAPI,
    user: User,
    workspace: WorkspaceContext,
):
    class Failing(StubAuthService):
        async def login(self, **kwargs):
            raise AuthenticationError("The email address or password is incorrect.")

    app.dependency_overrides[get_auth_service] = lambda: Failing(
        user=user,
        workspace=workspace,
    )

    response = await client.post("/api/v1/auth/login", json=CREDENTIALS)

    assert response.status_code == 401
    assert "error" in response.json()
    assert "incorrect" in response.text


async def test_logout_answers_no_content(client, service):
    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": "refresh-value"},
    )

    assert response.status_code == 204
    assert service.calls[0][0] == "logout"


async def test_switching_workspace_requires_authentication(client, service):
    response = await client.post(
        "/api/v1/auth/workspace",
        json={"workspace_slug": "acme"},
    )

    assert response.status_code == 401
    assert service.calls == []


async def test_switching_workspace_leaves_the_refresh_token_alone(
    client,
    service,
    authenticated,
):
    response = await client.post(
        "/api/v1/auth/workspace",
        json={"workspace_slug": "acme"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "switched-value"
    assert "refresh_token" not in body
    assert body["active_workspace"]["slug"] == "acme"


async def test_a_workspace_the_caller_is_not_in_looks_missing(
    client,
    app: FastAPI,
    user: User,
    workspace: WorkspaceContext,
    authenticated,
):
    class Denying(StubAuthService):
        async def select_workspace(self, *, user, workspace_slug):
            raise TenantIsolationError()

    app.dependency_overrides[get_auth_service] = lambda: Denying(
        user=user,
        workspace=workspace,
    )

    response = await client.post(
        "/api/v1/auth/workspace",
        json={"workspace_slug": "someone-elses-workspace"},
    )

    # 404 rather than 403: whether that workspace exists is not disclosed.
    assert response.status_code == 404


async def test_the_profile_lists_the_callers_workspaces(client, service, authenticated):
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "owner@wasla.test"
    assert [entry["slug"] for entry in body["workspaces"]] == ["acme"]
    assert body["platform_role"] is None


async def test_the_profile_requires_authentication(client, service):
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
