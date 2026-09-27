"""The member roster and the workspace switcher cost a fixed number of queries.

DB-014: `MembershipService.list_members` issued one query per member - 201
statements for 200 members - and `AuthService.list_workspaces` one per
membership. Both now read the memberships, then their people or workspaces in
one `IN` query. Counted at the driver, so any statement an ORM layer adds is
counted too.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.token_store import RefreshTokenStore
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.auth_service import AuthService
from app.services.membership_service import MembershipService
from tests.fakes import as_redis_client

pytestmark = pytest.mark.integration

MEMBERS = 200


@contextmanager
def counted(session: AsyncSession) -> Iterator[list[str]]:
    """Every statement the session's connection executes while inside."""
    statements: list[str] = []
    engine = session.get_bind()
    sync_engine: Any = getattr(engine, "sync_engine", engine)
    target: Any = getattr(sync_engine, "engine", sync_engine)

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    event.listen(target, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(target, "before_cursor_execute", record)


async def _users(session: AsyncSession, count: int) -> list[User]:
    users = [
        User(
            email=f"roster-{uuid.uuid4().hex[:10]}@example.com",
            hashed_password="x",
            is_active=True,
        )
        for _ in range(count)
    ]
    session.add_all(users)
    await session.flush()
    return users


async def test_the_roster_is_two_statements_for_two_hundred_members(
    db_session: AsyncSession,
) -> None:
    tenant = Tenant(name="Roster", slug=f"roster-{uuid.uuid4().hex[:10]}")
    db_session.add(tenant)
    await db_session.flush()
    for index, user in enumerate(await _users(db_session, MEMBERS)):
        db_session.add(
            Membership(
                tenant_id=tenant.id,
                user_id=user.id,
                role=TenantRole.TENANT_OWNER if index == 0 else TenantRole.MEMBER,
                status=MembershipStatus.ACTIVE,
            )
        )
    await db_session.flush()

    with counted(db_session) as statements:
        members = await MembershipService(session=db_session, tenant_id=tenant.id).list_members()
    assert len(members) == MEMBERS
    assert len(statements) <= 2, statements


async def test_the_workspace_switcher_is_two_statements_for_many_workspaces(
    db_session: AsyncSession, settings: Settings
) -> None:
    (person,) = await _users(db_session, 1)
    for index in range(40):
        tenant = Tenant(name=f"W{index}", slug=f"switch-{uuid.uuid4().hex[:10]}")
        db_session.add(tenant)
        await db_session.flush()
        db_session.add(
            Membership(
                tenant_id=tenant.id,
                user_id=person.id,
                role=TenantRole.MEMBER,
                status=MembershipStatus.ACTIVE,
            )
        )
    await db_session.flush()

    # Listing workspaces never touches refresh tokens, so the store is inert.
    service = AuthService(
        session=db_session,
        settings=settings,
        token_store=RefreshTokenStore(as_redis_client(object())),
    )
    with counted(db_session) as statements:
        workspaces = await service.list_workspaces(user=person)
    assert len(workspaces) == 40
    assert len(statements) <= 2, statements
