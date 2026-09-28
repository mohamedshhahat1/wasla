"""Every application session is bounded; migrations are not (DB-007).

The database audit found `statement_timeout`, `lock_timeout` and
`idle_in_transaction_session_timeout` all 0 for the runtime role: a lock wait
behind a long purge, or a transaction left open across a slow provider call,
held a pooled connection for as long as it lasted - and a replica's pool is
fifteen connections.

Thresholds here are generous and one-sided: a bound firing is asserted to
happen *before* a ceiling many times the configured value, never at a
particular moment, so a slow machine cannot turn these red.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db.errors import sqlstate
from app.db.session import Database
from app.services.workspace_purge_service import PURGE_SESSION_BOUNDS

pytestmark = pytest.mark.integration


def _settings(url: str, **bounds: int) -> Settings:
    return Settings(_env_file=None, environment="test", database_url=url).model_copy(update=bounds)


@pytest_asyncio.fixture
async def bounded(prepared_database: str) -> AsyncIterator[Database]:
    """The application's own engine, with short bounds so they fire quickly."""
    database = Database(
        _settings(
            prepared_database,
            database_statement_timeout_ms=1_000,
            database_lock_timeout_ms=300,
            database_idle_in_transaction_timeout_ms=500,
        )
    )
    try:
        yield database
    finally:
        await database.dispose()


async def _shown(session: object, setting: str) -> str:
    return str(await session.scalar(text(f"SHOW {setting}")))  # type: ignore[attr-defined]


async def test_the_default_bounds_reach_every_application_session(
    prepared_database: str,
) -> None:
    database = Database(_settings(prepared_database))
    try:
        async with database.session() as session:
            assert await _shown(session, "statement_timeout") == "30s"
            assert await _shown(session, "lock_timeout") == "5s"
            assert await _shown(session, "idle_in_transaction_session_timeout") == "1min"
    finally:
        await database.dispose()


async def test_a_blocked_lock_wait_ends_within_its_bound(
    bounded: Database, prepared_database: str
) -> None:
    """Another transaction holds the row; ours gives up instead of waiting for ever."""
    holder = create_async_engine(prepared_database, poolclass=NullPool)
    tenant_id = uuid.uuid4()
    try:
        async with holder.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, status)"
                    " VALUES (:id, 'Lock', :s, 'active')"
                ),
                {"id": tenant_id, "s": f"lock-{tenant_id.hex[:10]}"},
            )
        async with holder.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(
                text("SELECT 1 FROM tenants WHERE id = :id FOR UPDATE"), {"id": tenant_id}
            )
            started = time.monotonic()
            with pytest.raises(DBAPIError) as refused:
                async with bounded.session() as session:
                    await session.execute(
                        text("SELECT 1 FROM tenants WHERE id = :id FOR UPDATE"),
                        {"id": tenant_id},
                    )
            waited = time.monotonic() - started
            await transaction.rollback()
        assert sqlstate(refused.value) == "55P03"
        assert waited < 10
    finally:
        async with holder.begin() as connection:
            await connection.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
        await holder.dispose()


async def test_an_idle_open_transaction_cannot_live_for_ever(bounded: Database) -> None:
    """The server closes a session that sits in a transaction doing nothing."""
    with pytest.raises(DBAPIError):
        async with bounded.session() as session:
            await session.execute(text("SELECT 1"))
            # The thing under test is idleness itself, so the test idles:
            # three times the bound, and the server's own clock decides.
            await asyncio.sleep(1.5)
            await session.execute(text("SELECT 1"))


async def test_a_runaway_statement_is_cancelled(bounded: Database) -> None:
    started = time.monotonic()
    with pytest.raises(DBAPIError) as refused:
        async with bounded.session() as session:
            await session.execute(text("SELECT pg_sleep(30)"))
    assert sqlstate(refused.value) == "57014"
    assert time.monotonic() - started < 15


async def test_the_purge_raises_its_own_bounds_for_its_transaction_only(
    bounded: Database,
) -> None:
    async with bounded.session() as session:
        for setting, value in PURGE_SESSION_BOUNDS.items():
            await session.execute(text(f"SET LOCAL {setting} = '{value}'"))
        assert await _shown(session, "statement_timeout") == "15min"
        assert await _shown(session, "lock_timeout") == "30s"
    async with bounded.session() as session:
        assert await _shown(session, "statement_timeout") == "1s"


async def test_migrations_are_not_bounded_by_the_applications_settings(
    prepared_database: str,
) -> None:
    """Alembic builds its own engine from the URL; none of these reach it."""
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import async_engine_from_config

    config = Config()
    config.set_main_option("sqlalchemy.url", prepared_database)
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            for setting in ("statement_timeout", "lock_timeout"):
                assert await connection.scalar(text(f"SHOW {setting}")) == "0"
    finally:
        await engine.dispose()
