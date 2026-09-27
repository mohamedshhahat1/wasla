"""The checks around a migration or a restore refuse what they should (DB-010, DB-027).

`scripts/db_preflight.py` is what the `migrate` command runs before and after
`alembic upgrade head`. Both halves are exercised against scratch databases of
their own - one without pgvector, reached as a role that is not a superuser,
which is exactly the restore the audit saw fail - so nothing here disturbs the
schema the rest of the suite is using.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from scripts.db_preflight import Problem, prerequisite_problems, verification_problems

pytestmark = pytest.mark.integration

PASSWORD = "preflight-synthetic"


async def _admin(url: str, *statements: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def scratch(prepared_database: str) -> AsyncIterator[tuple[str, str]]:
    """A new, empty database owned by a new role that is not a superuser.

    Yields (the owner's URL, the administrator's URL for that database).
    """
    suffix = uuid.uuid4().hex[:10]
    role, name = f"preflight_{suffix}", f"preflight_{suffix}"
    admin = make_url(prepared_database)
    await _admin(
        prepared_database,
        f"CREATE ROLE {role} LOGIN NOSUPERUSER CREATEDB PASSWORD '{PASSWORD}'",
        f"CREATE DATABASE {name} OWNER {role} TEMPLATE template0",
    )
    owner_url = admin.set(username=role, password=PASSWORD, database=name)
    admin_url = admin.set(database=name)
    try:
        yield (
            owner_url.render_as_string(hide_password=False),
            admin_url.render_as_string(hide_password=False),
        )
    finally:
        await _admin(
            prepared_database,
            f"DROP DATABASE IF EXISTS {name} WITH (FORCE)",
            f"DROP ROLE IF EXISTS {role}",
        )


async def _problems(url: str, check: str) -> list[Problem]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            if check == "prerequisites":
                return await prerequisite_problems(connection)
            return await verification_problems(connection)
    finally:
        await engine.dispose()


async def test_a_non_superuser_without_pgvector_is_refused_before_migrating(
    scratch: tuple[str, str],
) -> None:
    owner, _ = scratch
    problems = await _problems(owner, "prerequisites")
    assert Problem("extension not installed and not creatable", "vector") in problems


async def test_the_refusal_changes_nothing(scratch: tuple[str, str]) -> None:
    owner, _ = scratch
    await _problems(owner, "prerequisites")
    engine = create_async_engine(owner, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            installed = await connection.execute(text("SELECT extname FROM pg_extension"))
            assert "vector" not in set(installed.scalars())
    finally:
        await engine.dispose()


async def test_pgvector_installed_by_a_superuser_satisfies_the_owner(
    scratch: tuple[str, str],
) -> None:
    owner, admin = scratch
    await _admin(admin, "CREATE EXTENSION vector", "CREATE EXTENSION pgcrypto")
    assert await _problems(owner, "prerequisites") == []
    # And the statement migration 0001 runs is then a no-op for the owner.
    await _admin(owner, "CREATE EXTENSION IF NOT EXISTS vector")


async def test_a_migrated_schema_passes_verification(db_connection: AsyncConnection) -> None:
    assert await verification_problems(db_connection) == []


async def test_an_unvalidated_constraint_an_invalid_index_and_a_disabled_trigger_fail(
    scratch: tuple[str, str],
) -> None:
    _, admin = scratch
    await _admin(
        admin,
        "CREATE TABLE ledger (id int PRIMARY KEY, amount int)",
        "INSERT INTO ledger VALUES (1, -5)",
        "ALTER TABLE ledger ADD CONSTRAINT ck_ledger_amount CHECK (amount >= 0) NOT VALID",
        "CREATE INDEX ix_ledger_amount ON ledger (amount)",
        "UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'ix_ledger_amount'::regclass",
        "CREATE FUNCTION ledger_guard() RETURNS trigger LANGUAGE plpgsql"
        " AS $$BEGIN RETURN NEW; END$$",
        "CREATE TRIGGER ledger_guard BEFORE UPDATE ON ledger"
        " FOR EACH ROW EXECUTE FUNCTION ledger_guard()",
        "ALTER TABLE ledger DISABLE TRIGGER ledger_guard",
    )
    assert await _problems(admin, "verify") == [
        Problem("constraint not validated", "ledger.ck_ledger_amount"),
        Problem("index invalid", "ledger.ix_ledger_amount"),
        Problem("trigger disabled", "ledger.ledger_guard"),
    ]
