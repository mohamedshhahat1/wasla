"""Fixtures for tests that need a real PostgreSQL database.

The schema is built from the models rather than by running Alembic. CI runs
pytest before it applies migrations, and the migration path has its own gates
(upgrade/downgrade/upgrade, then ``alembic check``), so building from metadata
here keeps the two concerns separate.

When no database URL is configured these tests skip instead of failing, so the
unit suite stays usable without PostgreSQL running.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
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


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """A fresh schema per test.

    The drop before the create matters: a crashed run must not poison the next
    one. NullPool keeps connections from outliving the test that opened them.
    """
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        for extension in REQUIRED_EXTENSIONS:
            await connection.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
