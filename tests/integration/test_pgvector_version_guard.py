"""The preflight and readiness refuse a pgvector older than 0.8 (RV-03, FX-1.3).

The supported database (the one every lane runs on) must pass. The refusal is
proved against a real older pgvector when `WASLA_TEST_OLD_PGVECTOR_URL` names
one (a `pgvector/pgvector:0.7.4-pg16` container); without it that half is an
opt-in lane, and says so in its skip reason, which CI's skip gate admits.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.pgvector_version import installed_version, pgvector_problem
from scripts.db_preflight import prerequisite_problems, verification_problems

pytestmark = pytest.mark.integration

OLD_URL = os.environ.get("WASLA_TEST_OLD_PGVECTOR_URL")
OPT_IN = "the old-pgvector lane is opt-in (WASLA_TEST_OLD_PGVECTOR_URL)"


async def test_the_supported_pgvector_passes_preflight_and_readiness(
    prepared_database: str,
) -> None:
    engine = create_async_engine(prepared_database)
    try:
        async with engine.connect() as connection:
            version = await installed_version(connection)
            assert version is not None
            assert await pgvector_problem(connection) is None
            assert not [
                p for p in await prerequisite_problems(connection) if p.kind == "extension too old"
            ]
    finally:
        await engine.dispose()


@pytest.mark.skipif(OLD_URL is None, reason=OPT_IN)
async def test_an_old_pgvector_fails_the_search_and_the_preflight_names_the_minimum() -> None:
    assert OLD_URL is not None
    engine = create_async_engine(OLD_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(text('CREATE EXTENSION IF NOT EXISTS "vector"'))
            await connection.commit()
            # The defect itself: once the library is loaded, the search's first
            # statement is refused.
            await connection.execute(text("SELECT '[1,2]'::vector <=> '[2,1]'::vector"))
            with pytest.raises(Exception, match="hnsw"):
                await connection.execute(
                    text("SELECT set_config('hnsw.iterative_scan', 'strict_order', true)")
                )
            await connection.rollback()

            problem = await pgvector_problem(connection)
            assert problem is not None and "0.8.0" in problem
            preflight = [str(p) for p in await prerequisite_problems(connection)]
            assert any("0.8.0" in line for line in preflight)
            verify = [str(p) for p in await verification_problems(connection, declared=None)]
            assert any("0.8.0" in line for line in verify)
    finally:
        await engine.dispose()


@pytest.mark.skipif(OLD_URL is None, reason=OPT_IN)
async def test_readiness_reports_postgresql_down_on_an_old_pgvector() -> None:
    from app.core.exceptions import DependencyUnavailableError
    from app.db.session import Database
    from tests.integration.ai_harness import settings_for

    assert OLD_URL is not None
    database = Database(settings_for(OLD_URL))
    try:
        with pytest.raises(DependencyUnavailableError, match="0.8.0"):
            await database.check(5)
    finally:
        await database.dispose()
