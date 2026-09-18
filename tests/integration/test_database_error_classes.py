"""Which database errors are permanent refusals of content (MEDIA-05).

Against the real PostgreSQL and the real asyncpg driver, because the defect was
exactly a mismatch between what a test assumed the driver raises and what it
does raise: `DataError` never appears under asyncpg, so a net built on it never
fired.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DataError, DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.errors import is_data_exception, sqlstate

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def engine(prepared_database: str) -> AsyncIterator[AsyncEngine]:
    built = create_async_engine(prepared_database, poolclass=NullPool)
    try:
        yield built
    finally:
        await built.dispose()


async def _refusal(engine: AsyncEngine, statement: str, **params: object) -> DBAPIError:
    async with engine.connect() as connection:
        await connection.execute(
            text("CREATE TEMP TABLE probe (label varchar(300), amount integer UNIQUE)")
        )
        await connection.execute(text("INSERT INTO probe (label, amount) VALUES ('x', 1)"))
        with pytest.raises(DBAPIError) as raised:
            await connection.execute(text(statement), params)
        return raised.value


@pytest.mark.parametrize(
    ("value", "state"),
    [
        ("a" * 301, "22001"),
        ("عقد" * 101, "22001"),
        ("con" + chr(0) + "tract", "22021"),
    ],
)
async def test_a_refused_value_is_a_data_exception_though_never_a_data_error(
    engine: AsyncEngine, value: str, state: str
) -> None:
    error = await _refusal(engine, "INSERT INTO probe (label) VALUES (:value)", value=value)

    assert not isinstance(error, DataError)
    assert sqlstate(error) == state
    assert is_data_exception(error)


async def test_an_out_of_range_integer_is_a_data_exception(engine: AsyncEngine) -> None:
    error = await _refusal(engine, "INSERT INTO probe (amount) VALUES (:value)", value=2**31)
    assert is_data_exception(error)


async def test_a_duplicate_is_not_content_the_database_refused(engine: AsyncEngine) -> None:
    error = await _refusal(engine, "INSERT INTO probe (label, amount) VALUES ('y', 1)")
    assert sqlstate(error) == "23505"
    assert not is_data_exception(error)


async def test_a_programming_error_is_not_content_the_database_refused(
    engine: AsyncEngine,
) -> None:
    error = await _refusal(engine, "SELECT * FROM nothing_by_this_name")
    assert not is_data_exception(error)


def test_errors_that_are_not_database_errors_are_never_data_exceptions() -> None:
    assert not is_data_exception(ValueError("22001"))
    assert not is_data_exception(ConnectionRefusedError())
