"""Fixtures for tests that need a real PostgreSQL database.

The schema is built from the models rather than by running Alembic. CI runs
pytest before it applies migrations, and the migration path has its own gates
(upgrade/downgrade/upgrade, then ``alembic check``), so building from metadata
here keeps the two concerns separate.

When no database URL is configured these tests skip instead of failing, so the
unit suite stays usable without PostgreSQL running.

Isolation strategy
------------------

The schema is built **once per session**; each test runs inside a transaction
that is **rolled back** afterwards. Isolation is therefore as complete as
dropping and recreating the schema per test, and roughly two orders of
magnitude cheaper: the drop/create cycle across every table, index and enum
type dominated the runtime of the whole suite.

Two details make this safe rather than merely fast, and neither is optional.

**No async fixture is session-scoped.** pytest-asyncio gives session-scoped
async fixtures a different event loop from function-scoped tests, and an
asyncpg connection cannot cross loops - it fails at runtime with an attached-to-
a-different-loop error, usually somewhere unrelated. The one-time schema build
therefore runs in a *synchronous* session fixture that opens its own loop with
``asyncio.run`` and closes it before returning, so no connection outlives it.
The engine and the session stay function-scoped, where the test's own loop owns
them.

**The session joins the outer transaction as a savepoint.**
``join_transaction_mode="create_savepoint"`` means a ``commit()`` inside a test
releases a savepoint rather than committing to the database, so the outer
rollback still undoes it. No test commits today - they all use ``flush()`` -
but one written later would otherwise leak its rows into every test that
follows, and that failure is miserable to diagnose because it depends on
ordering.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.db.models import Base

# TEST_DATABASE_URL wins so a developer can point these at a scratch database
# without touching the one their application uses.
URL_VARIABLES = ("TEST_DATABASE_URL", "DATABASE_URL")
REQUIRED_EXTENSIONS = ("pgcrypto", "vector")


@pytest.fixture(scope="session")
def database_url() -> str:
    for variable in URL_VARIABLES:
        value = os.environ.get(variable)
        if value:
            return value
    pytest.skip("No PostgreSQL URL configured; set TEST_DATABASE_URL to run these tests.")


async def _build_schema(url: str) -> None:
    """Drop and recreate every table, once.

    The drop before the create still matters: a crashed run must not poison the
    next one. It happens once per session now rather than once per test.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            for extension in REQUIRED_EXTENSIONS:
                await connection.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def prepared_database(database_url: str) -> str:
    """The schema, built once for the whole session.

    Deliberately synchronous. ``asyncio.run`` gives this its own event loop and
    closes it before returning, so nothing it opened can be reached from a
    test's loop later - which is the failure mode a session-scoped *async*
    fixture would introduce here.
    """
    asyncio.run(_build_schema(database_url))
    return database_url


@pytest_asyncio.fixture
async def engine(prepared_database: str) -> AsyncIterator[AsyncEngine]:
    """A function-scoped engine over the already-built schema.

    Function-scoped because the test's event loop owns any connection it opens.
    Constructing an engine is negligible; what used to cost was the schema, and
    that has moved to the session fixture. NullPool keeps connections from
    outliving the test that opened them.
    """
    engine = create_async_engine(prepared_database, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """One connection with an open transaction, rolled back at teardown.

    The rollback is what isolates the test. It is in a ``finally`` so that a
    failing test still leaves the database clean for the next one.
    """
    connection = await engine.connect()
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()


@pytest_asyncio.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """A session bound to the test's transaction.

    ``join_transaction_mode="create_savepoint"`` turns any ``commit()`` inside
    the test into a savepoint release, so the outer rollback still undoes it.
    ``expire_on_commit=False`` matches the application's own sessionmaker, so a
    test reads attributes off a flushed object the same way a service does.
    """
    session = AsyncSession(
        bind=db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
