"""Real error bodies, held to the exported document's envelope (FEB-02, OA-05).

`tests/unit/test_openapi_export.py` proves the document *says* every non-2xx
answer is the envelope. This file proves it is *true*: it drives the real
application - real routes, real handlers, real middleware, a model-built
database - into each error status a browser can meet, and validates the body it
actually returned against the schema the committed
`frontend/packages/api-client/openapi.json` declares for that operation and
status (or its `default`). The schemas come from the exported file, not from
the Pydantic models, because the file is what the frontend's client is
generated from.

Validation is strict: an object schema that lists properties is closed, so a
body carrying a field the document does not describe fails here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_auth_service, get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.exceptions import AuthenticationError
from app.core.security import create_access_token, hash_password
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.main import create_app
from tests.conftest import AllowingEntitlements, FakeDependency
from tests.json_schema_subset import schema_errors
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = json.loads(
    (ROOT / "frontend" / "packages" / "api-client" / "openapi.json").read_text("utf-8")
)
COMPONENTS: dict[str, Any] = DOCUMENT["components"]["schemas"]
API = "/api/v1"
PASSWORD = "correct horse battery staple"


def _declared(path: str | None, method: str, status: int) -> dict[str, Any]:
    """The schema the document declares for this answer: its status, else `default`."""
    if path is None:
        # Not an operation at all (an unknown path): the envelope is the contract.
        return {"$ref": "#/components/schemas/ErrorEnvelope"}
    responses = DOCUMENT["paths"][path][method.lower()]["responses"]
    response = responses.get(str(status), responses["default"])
    schema: dict[str, Any] = response["content"]["application/json"]["schema"]
    return schema


def _assert_contract(response: Response, path: str | None, method: str, code: str) -> None:
    body = response.json()
    problems = schema_errors(body, _declared(path, method, response.status_code), COMPONENTS)
    problems += schema_errors(
        body, _declared(path, method, response.status_code), COMPONENTS, strict=True
    )
    assert not problems, (response.status_code, path, body, problems)
    assert body["error"]["code"] == code, body


class _Redis:
    """Enough of Redis for login, refresh rotation and the limiter."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.counters: dict[str, int] = {}

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self.values else 0

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return 60

    async def rpush(self, key: str, value: str) -> int:
        return 1


class _Infra:
    def __init__(self) -> None:
        self.commands = _Redis()

    @property
    def client(self) -> _Redis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "rate_limit_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings_used() -> Settings:
    return _settings()


@pytest.fixture
def app(settings_used: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(settings_used)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wasla.test") as c:
        yield c


async def _person(
    session: AsyncSession,
    *,
    verified: bool = True,
    role: TenantRole = TenantRole.TENANT_OWNER,
    slug: str | None = None,
) -> tuple[User, Tenant]:
    user = User(
        email=f"envelope-{uuid.uuid4().hex[:10]}@example.com",
        full_name="Envelope",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC) if verified else None,
    )
    tenant = Tenant(
        timezone=TEST_TIMEZONE,
        name="Envelope",
        slug=slug or f"envelope-{uuid.uuid4().hex[:8]}",
        status=TenantStatus.ACTIVE,
    )
    session.add_all([user, tenant])
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=role))
    await session.flush()
    return user, tenant


def _bearer(settings: Settings, user: User, tenant: Tenant | None) -> dict[str, str]:
    token, _ = create_access_token(
        settings=settings,
        subject=user.id,
        tenant_id=tenant.id if tenant else None,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------------- 401


async def test_401_without_a_bearer(http: AsyncClient) -> None:
    response = await http.get(f"{API}/auth/me")

    assert response.status_code == 401
    _assert_contract(response, f"{API}/auth/me", "GET", "unauthenticated")


async def test_401_for_a_spent_refresh_token(http: AsyncClient, db_session: AsyncSession) -> None:
    user, _ = await _person(db_session)
    login = await http.post(f"{API}/auth/login", json={"email": user.email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    first = login.json()["refresh_token"]
    rotated = await http.post(f"{API}/auth/refresh", json={"refresh_token": first})
    assert rotated.status_code == 200, rotated.text

    response = await http.post(f"{API}/auth/refresh", json={"refresh_token": first})

    assert response.status_code == 401
    _assert_contract(response, f"{API}/auth/refresh", "POST", "unauthenticated")


# ------------------------------------------------------------------------- 403


async def test_403_until_the_email_is_verified(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user, tenant = await _person(db_session, verified=False)

    response = await http.get(f"{API}/conversations", headers=_bearer(settings_used, user, tenant))

    assert response.status_code == 403
    _assert_contract(response, f"{API}/conversations", "GET", "email_verification_required")


async def test_403_for_a_role_that_may_not(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user, tenant = await _person(db_session, role=TenantRole.MEMBER)

    response = await http.get(f"{API}/audit-logs", headers=_bearer(settings_used, user, tenant))

    assert response.status_code == 403
    _assert_contract(response, f"{API}/audit-logs", "GET", "permission_denied")


# ------------------------------------------------------------------------- 404


async def test_404_for_another_workspaces_record(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user, tenant = await _person(db_session)

    response = await http.get(
        f"{API}/conversations/{uuid.uuid4()}", headers=_bearer(settings_used, user, tenant)
    )

    assert response.status_code == 404
    _assert_contract(response, f"{API}/conversations/{{conversation_id}}", "GET", "not_found")


async def test_404_for_a_path_that_is_not_an_operation(http: AsyncClient) -> None:
    response = await http.get(f"{API}/no-such-thing")

    assert response.status_code == 404
    _assert_contract(response, None, "GET", "not_found")


# ------------------------------------------------------------------------- 405


async def test_405_for_a_method_the_route_does_not_take(http: AsyncClient) -> None:
    response = await http.delete(f"{API}/auth/login")

    assert response.status_code == 405
    # The document has no DELETE on this path; the envelope is the contract.
    _assert_contract(response, None, "DELETE", "method_not_allowed")


# ------------------------------------------------------------------------- 409


async def test_409_for_a_workspace_address_in_use(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user, tenant = await _person(db_session)

    response = await http.post(
        f"{API}/workspaces",
        json={"name": "Taken", "slug": tenant.slug, "timezone": TEST_TIMEZONE},
        headers=_bearer(settings_used, user, tenant),
    )

    assert response.status_code == 409, response.text
    _assert_contract(response, f"{API}/workspaces", "POST", "conflict")


# ------------------------------------------------------------------------- 413


async def test_413_from_the_body_limit_middleware(http: AsyncClient) -> None:
    response = await http.post(
        f"{API}/auth/login",
        content=b'{"email": "a@example.com", "password": "' + b"x" * 200_000 + b'"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    _assert_contract(response, f"{API}/auth/login", "POST", "payload_too_large")


# ------------------------------------------------------------------------- 422


async def test_422_for_a_missing_body_field(http: AsyncClient) -> None:
    response = await http.post(f"{API}/auth/login", json={"email": "a@example.com"})

    assert response.status_code == 422
    _assert_contract(response, f"{API}/auth/login", "POST", "validation_error")
    assert response.json()["error"]["details"]["errors"][0]["loc"] == ["body", "password"]


async def test_422_for_an_unknown_field(http: AsyncClient) -> None:
    response = await http.post(
        f"{API}/auth/login",
        json={"email": "a@example.com", "password": "x", "role": "PLATFORM_OWNER"},
    )

    assert response.status_code == 422
    _assert_contract(response, f"{API}/auth/login", "POST", "validation_error")
    assert response.json()["error"]["details"]["errors"][0]["type"] == "extra_forbidden"


async def test_422_for_a_query_parameter(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    user, tenant = await _person(db_session)

    response = await http.get(
        f"{API}/conversations?limit=many", headers=_bearer(settings_used, user, tenant)
    )

    assert response.status_code == 422
    _assert_contract(response, f"{API}/conversations", "GET", "validation_error")
    assert response.json()["error"]["details"]["errors"][0]["loc"] == ["query", "limit"]


# ------------------------------------------------------------------------- 429


class RefusingAuth:
    async def login(self, **kwargs: object) -> None:
        raise AuthenticationError("The credentials are not valid.")


async def test_429_when_the_bucket_is_spent() -> None:
    application = create_app(_settings(rate_limit_enabled=True, rate_limit_auth_per_minute=1))
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = FakeDependency(name="redis")
    application.dependency_overrides[get_auth_service] = RefusingAuth
    body = {"email": "a@example.com", "password": "a-wrong-password-123"}
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://wasla.test"
    ) as client:
        await client.post(f"{API}/auth/login", json=body)
        response = await client.post(f"{API}/auth/login", json=body)

    assert response.status_code == 429
    _assert_contract(response, f"{API}/auth/login", "POST", "rate_limited")


# ------------------------------------------------------------------------- 400


async def test_400_for_a_body_the_server_cannot_parse(
    http: AsyncClient, db_session: AsyncSession, settings_used: Settings
) -> None:
    """No route raises a 400 itself; Starlette does, for a multipart body it cannot parse."""
    user, tenant = await _person(db_session)
    path = f"{API}/knowledge/bases/{{knowledge_base_id}}/documents/upload"

    response = await http.post(
        path.replace("{knowledge_base_id}", str(uuid.uuid4())),
        content=b"--wrong\r\nnot a part\r\n",
        headers={
            "Content-Type": "multipart/form-data; boundary=expected",
            **_bearer(settings_used, user, tenant),
        },
    )

    assert response.status_code == 400, response.text
    _assert_contract(response, path, "POST", "bad_request")
