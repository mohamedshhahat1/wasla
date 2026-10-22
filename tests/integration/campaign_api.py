"""The real API over the real database, as an admin or a member of a seeded workspace.

The campaigns page's routes are driven over HTTP here - request validation,
role guards, the committing route, the response models - against the same
PostgreSQL the workers use. Only authentication is shortcut: the active
workspace is resolved from a membership row this module writes, in the
request's own session, exactly as `get_active_workspace` would after a login.
The routes' own guards (`TenantAdminDep` and the rest) still run on it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.dependencies import ActiveWorkspace, get_active_workspace
from app.core.dependencies import SessionDep
from app.core.redis import RedisClient
from app.db.models import Membership, TenantRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.session import Database
from app.main import create_app
from tests.integration.campaign_harness import USER_DOMAIN, Workspace, harness_settings

API = "/api/v1"


async def member_of(
    database: Database, workspace: Workspace, *, role: TenantRole = TenantRole.TENANT_ADMIN
) -> uuid.UUID:
    """A person with `role` in the workspace; erased with the harness's people."""
    async with database.session_factory() as session:
        user = User(
            email=f"page-{uuid.uuid4().hex[:10]}{USER_DOMAIN}",
            full_name="Page Tester",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        session.add(Membership(tenant_id=workspace.tenant_id, user_id=user.id, role=role))
        await session.commit()
        return user.id


def page_app(database: Database, **settings: Any) -> FastAPI:
    resolved = harness_settings(
        database.engine.url.render_as_string(hide_password=False),
        rate_limit_enabled=False,
        **settings,
    )
    application = create_app(resolved)
    application.state.database = database
    application.state.redis = RedisClient(resolved)
    return application


@asynccontextmanager
async def client_for(
    database: Database,
    workspace: Workspace,
    *,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    user_id: uuid.UUID | None = None,
    overrides: dict[Any, Any] | None = None,
    **settings: Any,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client acting as one person in one workspace.

    `overrides` replaces further dependencies - the messaging service with one
    on the fake Meta, the media store with a test one - for the routes that send.
    """
    person = user_id or await member_of(database, workspace, role=role)
    application = page_app(database, **settings)

    async def active(session: SessionDep) -> ActiveWorkspace:
        user = await session.get(User, person)
        tenant = await session.get(Tenant, workspace.tenant_id)
        membership = await session.scalar(
            select(Membership).where(
                Membership.user_id == person, Membership.tenant_id == workspace.tenant_id
            )
        )
        assert user is not None and tenant is not None and membership is not None
        return ActiveWorkspace(user=user, membership=membership, tenant=tenant)

    application.dependency_overrides[get_active_workspace] = active
    application.dependency_overrides.update(overrides or {})
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://wasla.test"
        ) as client:
            yield client
    finally:
        await application.state.redis.close()


__all__ = ["API", "client_for", "member_of", "page_app"]
