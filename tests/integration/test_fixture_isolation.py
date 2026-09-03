"""The isolation the integration fixtures promise.

These test the test infrastructure, which is worth doing precisely because a
leak here does not fail loudly: it makes some later test pass or fail depending
on ordering, and the symptom appears nowhere near the cause.

The schema is now built once per session and each test is rolled back, rather
than the schema being dropped and recreated every time. That is a large change
in how isolation is achieved and no change at all in how much of it there is —
which is the claim this file exists to check.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.db.models.knowledge import KnowledgeBase
from app.db.models.tenant import Tenant

pytestmark = pytest.mark.integration

# Reused across tests on purpose. If anything leaked, the second test to run
# would collide on the unique slug or see the other's row.
SHARED_SLUG = "isolation-probe"


async def _count(session: AsyncSession, model: type[Any]) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def test_the_database_starts_empty(db_session: AsyncSession) -> None:
    """Whatever ran before this must not be visible."""
    assert await _count(db_session, Tenant) == 0


async def test_a_test_may_write_freely(db_session: AsyncSession) -> None:
    db_session.add(Tenant(name="Probe", slug=SHARED_SLUG))
    await db_session.flush()

    assert await _count(db_session, Tenant) == 1


async def test_the_previous_tests_writes_are_gone(db_session: AsyncSession) -> None:
    """Rollback, not deletion: the previous test never committed anything.

    Written to reuse the same unique slug, so a leak fails here on the
    constraint rather than passing quietly.
    """
    assert await _count(db_session, Tenant) == 0

    db_session.add(Tenant(name="Probe", slug=SHARED_SLUG))
    await db_session.flush()

    assert await _count(db_session, Tenant) == 1


async def test_a_commit_inside_a_test_does_not_escape_it(db_session: AsyncSession) -> None:
    """`join_transaction_mode="create_savepoint"` is what makes this true.

    No test commits today. One written later would otherwise leak its rows into
    every test that follows, and that failure depends on ordering, which makes
    it miserable to diagnose.
    """
    db_session.add(Tenant(name="Committed", slug="committed-probe"))
    await db_session.commit()

    assert await _count(db_session, Tenant) == 1


async def test_the_committed_row_is_gone_too(db_session: AsyncSession) -> None:
    """The outer rollback undoes the savepoint release from the test above."""
    assert await _count(db_session, Tenant) == 0


async def test_flush_makes_rows_visible_within_the_test(db_session: AsyncSession) -> None:
    """Every service in this project flushes and never commits.

    The fixture has to preserve that reading, or half the suite would be
    testing something the application never does.
    """
    tenant = Tenant(name="Flushed", slug="flushed-probe")
    db_session.add(tenant)
    await db_session.flush()

    found = await db_session.execute(select(Tenant).where(Tenant.slug == "flushed-probe"))
    assert found.scalar_one().id == tenant.id


async def test_a_rolled_back_savepoint_leaves_the_test_usable(db_session: AsyncSession) -> None:
    """A failed flush must not poison the rest of the test."""
    db_session.add(Tenant(name="First", slug="unique-probe"))
    await db_session.flush()

    db_session.add(Tenant(name="Duplicate", slug="unique-probe"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    # The connection is still usable and the transaction still isolates.
    assert await _count(db_session, Tenant) == 0


async def test_the_schema_carries_every_table_not_just_the_ones_touched(
    db_session: AsyncSession,
) -> None:
    """The session-scoped build must create everything, not a subset."""
    tenant = Tenant(name="Probe", slug="schema-probe")
    db_session.add(tenant)
    await db_session.flush()

    # A table from the newest migration, to prove the build is current.
    db_session.add(KnowledgeBase(tenant_id=tenant.id, name="General"))
    await db_session.flush()

    assert await _count(db_session, KnowledgeBase) == 1


async def test_each_test_gets_its_own_connection(
    db_session: AsyncSession, db_connection: AsyncConnection
) -> None:
    """The session must be bound to the transaction the fixture opened.

    If it opened its own connection instead, the rollback would isolate
    nothing.
    """
    assert db_session.bind is db_connection
    assert db_connection.in_transaction()


async def test_identifiers_are_generated_without_a_committed_sequence(
    db_session: AsyncSession,
) -> None:
    """UUID keys, so rollback reuse cannot collide the way a serial would."""
    first = Tenant(name="One", slug=f"probe-{uuid.uuid4().hex[:8]}")
    second = Tenant(name="Two", slug=f"probe-{uuid.uuid4().hex[:8]}")
    db_session.add_all([first, second])
    await db_session.flush()

    assert first.id != second.id
