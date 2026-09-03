"""Repository tests, with tenant isolation as the main subject.

The repositories are driven through a recording session stand-in and the
assertions are made on the compiled statements. That verifies the tenant id is
actually bound into every scoped read, which is the property that matters, and
it needs no database.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from sqlalchemy.sql import ClauseElement

from app.core.exceptions import ConflictError, TenantIsolationError
from app.db.models import Membership, TenantRole, User
from app.repositories import (
    MembershipRepository,
    TenantRepository,
    UserMembershipRepository,
    UserRepository,
)
from tests.fakes import as_session

TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class _FakeScalars:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> Sequence[Any]:
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class RecordingSession:
    """Minimal session stand-in: records statements instead of running them."""

    def __init__(self, rows: Sequence[Any] = ()) -> None:
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.commits = 0
        self._rows = list(rows)

    async def execute(self, statement: ClauseElement) -> _FakeResult:
        self.statements.append(statement)
        return _FakeResult(self._rows)

    def add(self, entity: object) -> None:
        self.added.append(entity)

    async def commit(self) -> None:
        self.commits += 1


def bound_values(statement: ClauseElement) -> set[Any]:
    """Values SQLAlchemy would send as bind parameters for this statement."""
    return set(statement.compile().params.values())


SCOPED_READS = (
    pytest.param(lambda repository: repository.get_for_user(USER_ID), id="get_for_user"),
    pytest.param(lambda repository: repository.require_for_user(USER_ID), id="require_for_user"),
    pytest.param(lambda repository: repository.list_members(), id="list_members"),
)


@pytest.mark.parametrize("read", SCOPED_READS)
async def test_every_scoped_read_binds_the_tenant(read: Callable[..., Any]) -> None:
    session = RecordingSession(rows=[Membership(role=TenantRole.MEMBER)])
    repository = MembershipRepository(as_session(session), tenant_id=TENANT_A)

    await read(repository)

    assert TENANT_A in bound_values(session.statements[0])


async def test_two_tenants_never_share_a_query() -> None:
    session_a = RecordingSession()
    session_b = RecordingSession()

    await MembershipRepository(as_session(session_a), tenant_id=TENANT_A).get_for_user(USER_ID)
    await MembershipRepository(as_session(session_b), tenant_id=TENANT_B).get_for_user(USER_ID)

    bound_a = bound_values(session_a.statements[0])
    bound_b = bound_values(session_b.statements[0])

    assert TENANT_A in bound_a
    assert TENANT_B not in bound_a
    assert TENANT_B in bound_b
    assert TENANT_A not in bound_b


async def test_a_row_outside_the_tenant_is_indistinguishable_from_a_missing_one() -> None:
    repository = MembershipRepository(as_session(RecordingSession()), tenant_id=TENANT_A)

    with pytest.raises(TenantIsolationError) as raised:
        await repository.require_for_user(USER_ID)

    # A 404 with the generic message: error codes must not become a probe for
    # what exists in another tenant.
    assert raised.value.status_code == 404
    assert "not found" in raised.value.message.lower()


async def test_new_membership_belongs_to_the_repository_tenant() -> None:
    session = RecordingSession()
    repository = MembershipRepository(as_session(session), tenant_id=TENANT_A)

    membership = await repository.add_member(user_id=USER_ID, role=TenantRole.MEMBER)

    assert membership.tenant_id == TENANT_A
    assert session.added == [membership]


async def test_writes_leave_the_transaction_to_the_caller() -> None:
    session = RecordingSession()
    repository = MembershipRepository(as_session(session), tenant_id=TENANT_A)

    await repository.add_member(user_id=USER_ID, role=TenantRole.MEMBER)

    assert session.commits == 0


async def test_cross_tenant_membership_listing_is_keyed_by_user() -> None:
    session = RecordingSession()

    await UserMembershipRepository(as_session(session)).list_for_user(USER_ID)

    statement = session.statements[0]
    assert USER_ID in bound_values(statement)
    # Deliberately unscoped, so this must be keyed by the user and nothing else.
    assert "tenant_id" not in str(statement.whereclause)


async def test_email_lookup_is_case_insensitive() -> None:
    session = RecordingSession()

    await UserRepository(as_session(session)).get_by_email("  Owner@Example.COM ")

    assert "owner@example.com" in bound_values(session.statements[0])


async def test_duplicate_email_is_rejected_as_a_conflict() -> None:
    session = RecordingSession(rows=[User(email="owner@example.com")])

    with pytest.raises(ConflictError):
        await UserRepository(as_session(session)).create(email="Owner@Example.com")


async def test_new_user_is_normalised_and_active() -> None:
    session = RecordingSession()

    user = await UserRepository(as_session(session)).create(
        email=" Owner@Example.COM ", full_name="  Sara  "
    )

    assert user.email == "owner@example.com"
    assert user.full_name == "Sara"
    assert user.is_active is True
    assert user.hashed_password is None


async def test_active_tenant_listing_excludes_suspended_and_deleted() -> None:
    session = RecordingSession()

    await TenantRepository(as_session(session)).list_active()

    where_clause = str(session.statements[0].whereclause)
    assert "tenants.status" in where_clause
    assert "tenants.deleted_at IS NULL" in where_clause


async def test_new_tenant_slug_is_normalised() -> None:
    session = RecordingSession()

    tenant = await TenantRepository(as_session(session)).create(name="  Acme Inc  ", slug=" Acme ")

    assert tenant.slug == "acme"
    assert tenant.name == "Acme Inc"


async def test_duplicate_slug_is_rejected_as_a_conflict() -> None:
    session = RecordingSession(rows=[object()])

    with pytest.raises(ConflictError):
        await TenantRepository(as_session(session)).create(name="Acme", slug="acme")
