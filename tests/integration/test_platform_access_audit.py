"""Platform staff reading across workspaces, and the trail it leaves.

Every platform *write* was audited — a payment recorded, an invoice voided, an
account disabled — and no platform *read* was. That asymmetry was defensible
while the reads were aggregates and stops being defensible the moment a customer
asks who looked at their workspace (ADR-095).

Two things are proved here and they pull against each other. The reads must be
recorded, and the records must be worth having: an entry naming the search term
an operator typed would be a trail that leaks the thing it exists to police,
since somebody looking for one business types an address as readily as a name.

The routes run against a real database with the real guard, so what is asserted
is the request as it ships rather than a service called directly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import TokenClaims, TokenType
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.enums import PlatformRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.main import create_app

pytestmark = pytest.mark.integration

PATH = "/api/v1/platform"

# Values that must never reach an audit entry. Each is a thing a real operator
# would type or a real workspace would be called, and each is checked against
# every column and the whole of `meta`.
CANARY_SEARCH = "customer-canary@example.com"
CANARY_WORKSPACE = "Canary Holdings"


class _Infra:
    """Enough of the infrastructure handles for the app to build."""

    async def close(self) -> None:  # pragma: no cover - never reached here
        return None


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        jwt_secret="platform-access-audit-secret-not-for-deployment",
    )


@pytest.fixture
def app(db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(_settings())
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as client:
        yield client


async def _account(session: AsyncSession, *, role: PlatformRole | None) -> User:
    user = User(
        email=f"staff-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="argon2-placeholder-never-verified-here",
        is_active=True,
        platform_role=role,
    )
    session.add(user)
    await session.flush()
    return user


def _act_as(app: FastAPI, user: User) -> None:
    claims = TokenClaims(
        subject=user.id,
        token_type=TokenType.ACCESS,
        token_id=uuid.uuid4(),
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
        tenant_id=None,
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user=user, claims=claims)


async def _workspace(session: AsyncSession, *, name: str = CANARY_WORKSPACE) -> Tenant:
    tenant = Tenant(name=name, slug=f"canary-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _access_entries(session: AsyncSession) -> list[AuditLog]:
    """Every platform read recorded so far, oldest first."""
    rows = await session.execute(
        select(AuditLog)
        .where(
            AuditLog.action.in_(
                [
                    AuditAction.PLATFORM_OVERVIEW_READ,
                    AuditAction.PLATFORM_WORKSPACES_READ,
                    AuditAction.PLATFORM_AUDIT_LOG_READ,
                ]
            )
        )
        .order_by(AuditLog.occurred_at)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------- the trail


async def test_platform_cross_workspace_read_writes_audit_event(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """The finding itself: listing other people's workspaces is now recorded."""
    staff = await _account(db_session, role=PlatformRole.PLATFORM_OWNER)
    await _workspace(db_session)
    _act_as(app, staff)

    response = await http.get(f"{PATH}/tenants")
    assert response.status_code == 200

    (entry,) = await _access_entries(db_session)
    assert entry.action is AuditAction.PLATFORM_WORKSPACES_READ
    assert entry.actor_kind is AuditActorKind.PLATFORM_STAFF
    assert entry.actor_id == staff.id
    assert entry.actor_label == staff.email
    assert entry.meta is not None
    assert entry.meta["resource"] == "workspaces"
    assert entry.meta["returned"] >= 1


async def test_reading_one_workspaces_trail_names_that_workspace(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """The deepest read on this surface, and the one a customer would ask about.

    A workspace id is the *subject* of the entry rather than its payload, so it
    is recorded — that is precisely the access somebody is entitled to be told
    about.
    """
    staff = await _account(db_session, role=PlatformRole.PLATFORM_ADMIN)
    tenant = await _workspace(db_session)
    _act_as(app, staff)

    response = await http.get(f"{PATH}/audit-logs", params={"tenant_id": str(tenant.id)})
    assert response.status_code == 200

    (entry,) = await _access_entries(db_session)
    assert entry.action is AuditAction.PLATFORM_AUDIT_LOG_READ
    assert entry.target_type == "tenant"
    assert entry.target_id == tenant.id
    assert entry.meta is not None
    assert entry.meta["resource"] == "audit_log"


async def test_platform_aggregate_route_uses_expected_audit_policy(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """The overview names no workspace, and is recorded anyway.

    A privacy trail answers "who was looking at us at all", and an aggregate
    read is part of that answer. What is *not* recorded is any window boundary:
    a date range is a filter, and filters are how an operator narrows to one
    customer.
    """
    staff = await _account(db_session, role=PlatformRole.PLATFORM_OWNER)
    _act_as(app, staff)

    response = await http.get(f"{PATH}/overview", params={"since": "2026-08-01T00:00:00Z"})
    assert response.status_code == 200

    (entry,) = await _access_entries(db_session)
    assert entry.action is AuditAction.PLATFORM_OVERVIEW_READ
    assert entry.target_type == "platform"
    assert entry.target_id is None, "no workspace is identifiable in an aggregate"
    assert entry.meta == {"resource": "platform_usage", "windowed": True}


async def test_platform_read_audit_contains_no_customer_payload(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """A canary suite for the trail itself.

    An operator searching a workspace list types an address as readily as a
    company name, so the term is never recorded - only that a search happened.
    Nothing here may carry what was typed or what was found.
    """
    staff = await _account(db_session, role=PlatformRole.PLATFORM_OWNER)
    await _workspace(db_session)
    _act_as(app, staff)

    response = await http.get(f"{PATH}/tenants", params={"search": CANARY_SEARCH})
    assert response.status_code == 200

    (entry,) = await _access_entries(db_session)
    written = " ".join(
        str(value)
        for value in (
            entry.action,
            entry.actor_label,
            entry.target_type,
            entry.target_label,
            entry.meta,
        )
    )
    assert CANARY_SEARCH not in written
    assert CANARY_WORKSPACE not in written
    assert entry.meta is not None
    assert entry.meta["searched"] is True, "that a search happened is recorded"


async def test_normal_workspace_member_cannot_create_platform_read_events(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Owning a workspace grants nothing across the platform, trail included."""
    member = await _account(db_session, role=None)
    _act_as(app, member)

    for path in ("/overview", "/tenants", "/audit-logs"):
        response = await http.get(f"{PATH}{path}")
        assert response.status_code == 403

    assert await _access_entries(db_session) == []


async def test_failed_authorization_does_not_create_successful_access_audit(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """A refusal is not an access, and must not read as one.

    The entry is written *after* the read rather than in a dependency, so a
    request the guard turned away leaves nothing - which is what keeps
    "somebody read this" from meaning "somebody tried".
    """
    await _account(db_session, role=None)
    response = await http.get(f"{PATH}/tenants")

    assert response.status_code == 401
    assert await _access_entries(db_session) == []


async def test_every_platform_read_route_records_one_entry(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """Coverage stated as an assertion rather than left to review.

    A read route added later without a matching entry fails here, which is the
    property that keeps this finding closed rather than fixed once.
    """
    staff = await _account(db_session, role=PlatformRole.PLATFORM_OWNER)
    _act_as(app, staff)

    # From the OpenAPI document rather than `app.routes`, which on this
    # application yields router wrappers with no path of their own - so a set
    # comprehension over it is empty and every assertion built on one is
    # vacuous.
    read_routes = sorted(
        path
        for path, operations in app.openapi()["paths"].items()
        if path.startswith(f"{PATH}/") and "get" in operations
    )
    assert read_routes == [
        f"{PATH}/audit-logs",
        f"{PATH}/overview",
        f"{PATH}/tenants",
    ]

    for path in read_routes:
        response = await http.get(path)
        assert response.status_code == 200, path

    assert len(await _access_entries(db_session)) == len(read_routes)
