"""The scrape carries PostgreSQL's own view of the database (DB-023).

Read through the application's pool and identity, so these run against the real
exposition: the series exist, they are typed correctly, their labels are
bounded, and a session held on a lock is actually counted.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.metrics import MetricsRegistry
from app.db.session import Database
from app.services.metrics_service import DEAD_TUPLE_TABLES, MetricsService

pytestmark = pytest.mark.integration

LOCK_KEY = 0x57_44_42_23


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    settings = Settings(_env_file=None, environment="test", database_url=prepared_database)
    handle = Database(settings)
    try:
        yield handle
    finally:
        await handle.dispose()


def _value(exposition: str, series: str) -> float:
    match = re.search(rf"^{re.escape(series)} (\S+)$", exposition, re.MULTILINE)
    assert match, f"{series} is not in the exposition"
    return float(match.group(1))


async def _render(database: Database) -> str:
    return await MetricsService(None, registry=MetricsRegistry(), database=database).render()


async def test_the_database_series_are_published(database: Database) -> None:
    exposition = await _render(database)
    assert _value(exposition, 'wasla_db_connections{state="all"}') >= 1
    assert _value(exposition, "wasla_db_max_connections") > 0
    assert _value(exposition, "wasla_db_size_bytes") > 0
    assert _value(exposition, "wasla_db_deadlocks_total") >= 0
    assert "# TYPE wasla_db_deadlocks_total counter" in exposition
    assert "# TYPE wasla_db_lock_waiting_sessions gauge" in exposition


async def test_dead_tuples_are_labelled_only_by_the_fixed_tables(database: Database) -> None:
    exposition = await _render(database)
    labelled = set(
        re.findall(r'^wasla_db_dead_tuples\{table="([^"]+)"\}', exposition, re.MULTILINE)
    )
    assert labelled == set(DEAD_TUPLE_TABLES)


async def test_a_session_waiting_on_a_lock_is_counted(
    database: Database, prepared_database: str
) -> None:
    engine = create_async_engine(prepared_database, poolclass=NullPool)
    try:
        async with engine.connect() as holder, engine.connect() as waiter:
            await holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
            blocked = asyncio.create_task(
                waiter.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
            )
            try:
                waiting = 0.0
                for _ in range(100):
                    waiting = _value(await _render(database), "wasla_db_lock_waiting_sessions")
                    if waiting >= 1:
                        break
                    await asyncio.sleep(0.05)
                assert waiting >= 1
                oldest = _value(await _render(database), "wasla_db_oldest_transaction_age_seconds")
                assert oldest > 0
            finally:
                await holder.rollback()
                await asyncio.wait_for(blocked, timeout=10)
                await waiter.rollback()
    finally:
        await engine.dispose()
