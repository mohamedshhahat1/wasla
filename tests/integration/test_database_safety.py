"""What the destructive fixtures will and will not point themselves at.

`prepared_database` begins with `DROP SCHEMA public CASCADE`. That is correct
and cannot change: it is the only thing that clears an `alembic_version` and an
enum whose labels have drifted, which is the whole basis of the migration-built
schema strategy.

What could change, and has, is *which* database receives it. The fixture used to
fall back from `TEST_DATABASE_URL` to `DATABASE_URL`, so a developer who
exported the application's database for a script and then ran `pytest` in the
same shell lost that database's schema. Neither half was wrong on its own; the
combination was (WQ-12).

These run in a subprocess rather than by calling the fixture, because what has
to be proved is the behaviour of a real pytest run with a real environment -
and, in the first case, that it refuses **before it opens a connection**. A test
that called `database_url()` in-process could show the refusal but not that
nothing had connected by the time it fired.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _table_exists(url: str, table: str) -> bool:
    async def ask() -> bool:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                found = await connection.execute(
                    text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"}
                )
                return bool(found.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(ask())


@pytest.fixture
def scratch(database_url: str) -> Iterator[tuple[str, str]]:
    """A disposable database of this test's own, with one table in it.

    Built beside the run's own test database on the same server and dropped
    afterwards however the test ends. Two tests use it for opposite reasons: one
    names it in `DATABASE_URL` and asserts the table *survives*, and the other
    names it in `TEST_DATABASE_URL` and lets the child run destroy it.

    Never the database this run is itself using, and that is not fastidiousness
    - the child process builds a schema and drops it at teardown, so pointing it
    at the parent's database would delete the schema out from under the suite
    that spawned it. (It did, once, which is how this fixture came to exist.)
    """
    name = f"wasla_wq_scratch_{uuid.uuid4().hex[:8]}"
    administrative = database_url.rsplit("/", 1)[0] + "/postgres"
    target = database_url.rsplit("/", 1)[0] + "/" + name
    marker = "developers_working_copy"

    async def create() -> None:
        engine = create_async_engine(
            administrative, poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'CREATE DATABASE "{name}"'))
        finally:
            await engine.dispose()

        seeded = create_async_engine(target, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with seeded.connect() as connection:
                await connection.execute(text(f"CREATE TABLE {marker} (id integer primary key)"))
        finally:
            await seeded.dispose()

    async def drop() -> None:
        engine = create_async_engine(
            administrative, poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            await engine.dispose()

    asyncio.run(create())
    try:
        yield target, marker
    finally:
        asyncio.run(drop())


def _run(env_overrides: dict[str, str | None], *, test_body: str) -> subprocess.CompletedProcess:
    """Run one generated test file in a clean subprocess."""
    environment = {
        key: value
        for key, value in os.environ.items()
        # Start from a copy without either database variable, so the parent
        # run's configuration cannot decide the child's outcome.
        if key not in {"TEST_DATABASE_URL", "DATABASE_URL"}
    }
    environment["ENVIRONMENT"] = "test"
    environment.setdefault("JWT_SECRET", "database-safety-probe-secret-value-not-deployed")
    for key, value in env_overrides.items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value

    target = REPOSITORY_ROOT / "tests" / "integration" / "_database_safety_probe.py"
    target.write_text(test_body, encoding="utf-8")
    try:
        return subprocess.run(  # noqa: S603 - a literal command and this interpreter
            [sys.executable, "-m", "pytest", str(target), "-p", "no:cacheprovider", "-rs"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        target.unlink(missing_ok=True)


ASKS_FOR_THE_DESTRUCTIVE_FIXTURE = textwrap.dedent('''
    """Generated by test_database_safety.py; removed immediately afterwards."""

    import pytest

    pytestmark = pytest.mark.integration


    def test_it_reached_the_database(prepared_database: str) -> None:
        assert prepared_database
    ''')


def test_the_application_database_alone_is_refused_before_anything_connects(
    scratch: tuple[str, str],
) -> None:
    """The finding, stated as the behaviour that replaced it.

    `DATABASE_URL` here names a **real, reachable** database standing in for a
    developer's working copy, with a table in it. That is deliberately stronger
    than pointing at a closed port: an unreachable URL could only ever prove
    that the run failed, not that it failed for the right reason, and a
    connection error and a refusal look similar in a transcript.

    Two things are asserted, and the second is the one that matters. The run
    fails, naming both variables - and the canary table is still there
    afterwards, which is only possible if nothing ever reached
    `DROP SCHEMA public CASCADE`.
    """
    url, marker = scratch

    result = _run({"DATABASE_URL": url}, test_body=ASKS_FOR_THE_DESTRUCTIVE_FIXTURE)
    output = result.stdout + result.stderr

    assert result.returncode != 0, "a lone DATABASE_URL must not be accepted"
    assert "DATABASE_URL is set and TEST_DATABASE_URL is not" in output
    assert "wasla_test" in output, "the refusal should show what to set instead"

    # The whole finding, inverted: the database `DATABASE_URL` named still has
    # everything it had. Under the old fallback this table was gone.
    assert _table_exists(
        url, marker
    ), "the run destroyed the schema of the database DATABASE_URL named"


def test_a_dedicated_test_database_is_accepted(scratch: tuple[str, str]) -> None:
    """The other side of it: the supported configuration still works.

    Deliberately *not* the database this run is using. The child builds a schema
    and drops it at teardown, so naming the parent's database here would delete
    the schema out from under the suite that spawned it - which is exactly the
    class of accident this whole file is about.
    """
    url, _ = scratch

    result = _run({"TEST_DATABASE_URL": url}, test_body=ASKS_FOR_THE_DESTRUCTIVE_FIXTURE)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 passed" in output


def test_neither_variable_skips_rather_than_failing() -> None:
    """A machine without PostgreSQL must still be able to run the unit suite.

    This is the behaviour the refusal above must not have broken: no database
    configured at all is an ordinary developer laptop, not a mistake, and
    turning it into a failure would make the whole suite unusable there. CI's
    skip policy is what stops this reason hiding anything, and it allow-lists
    this message by name.
    """
    result = _run(
        {"TEST_DATABASE_URL": None, "DATABASE_URL": None},
        test_body=ASKS_FOR_THE_DESTRUCTIVE_FIXTURE,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "No PostgreSQL URL configured" in output
    assert "1 skipped" in output
