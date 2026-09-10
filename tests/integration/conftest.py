"""Fixtures for tests that need a real PostgreSQL database.

Schema strategy
---------------

``WASLA_TEST_SCHEMA`` chooses how the schema under test is built:

``models`` (the default)
    ``Base.metadata.create_all``. Fast, and what the bulk of the suite runs
    against.

``migrations``
    ``alembic upgrade head`` against an empty ``public`` schema — the same path
    a deployment takes.

The default is not the safe one, and saying so is the point. A schema built
from the models is *by construction* in agreement with the models: a native
PostgreSQL enum created by ``create_all`` carries every Python member, so a
value the migrations never added is present anyway and every test that writes
one passes. That is exactly how AUTH-01 shipped — four ``AuditAction`` labels
lived in Python and in no migration, six account-security endpoints raised
``InvalidTextRepresentation`` and rolled back in production, and the suite was
green.

Two things stop that recurring, and both are run in CI (``migration-parity``):

* ``test_schema_parity.py`` compares every model enum against ``pg_enum`` on a
  migration-built database and fails on any difference.
* The account and workspace lifecycle suites are re-run with
  ``WASLA_TEST_SCHEMA=migrations``, so the guarantees those endpoints make are
  proven against the schema production actually has.

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
import pathlib
from collections.abc import AsyncIterator, Iterator

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

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]

# TEST_DATABASE_URL wins so a developer can point these at a scratch database
# without touching the one their application uses.
URL_VARIABLES = ("TEST_DATABASE_URL", "DATABASE_URL")
REQUIRED_EXTENSIONS = ("pgcrypto", "vector")

# How the schema under test is built. See the module docstring.
SCHEMA_VARIABLE = "WASLA_TEST_SCHEMA"
MODEL_SCHEMA = "models"
MIGRATION_SCHEMA = "migrations"


def schema_strategy() -> str:
    """Which of the two schema builds this run uses.

    Read through a function rather than captured at import so a test can report
    it, and so the value is validated once: an unrecognised setting is a typo in
    a CI workflow, and defaulting quietly would run the fast path under a name
    somebody chose to get the slow one.
    """
    value = os.environ.get(SCHEMA_VARIABLE, MODEL_SCHEMA).strip().lower()
    if value not in (MODEL_SCHEMA, MIGRATION_SCHEMA):
        raise RuntimeError(
            f"{SCHEMA_VARIABLE} must be {MODEL_SCHEMA!r} or {MIGRATION_SCHEMA!r}, not {value!r}"
        )
    return value


def built_from_migrations() -> bool:
    """Whether this run's schema came from ``alembic upgrade head``."""
    return schema_strategy() == MIGRATION_SCHEMA


@pytest.fixture(scope="session")
def database_url() -> str:
    security_run = os.environ.get("WASLA_SECURITY_TESTS") == "1"
    if security_run and not os.environ.get("TEST_DATABASE_URL"):
        pytest.fail(
            "WASLA_SECURITY_TESTS=1 requires an explicit TEST_DATABASE_URL; "
            "critical database security coverage may not be skipped."
        )
    for variable in URL_VARIABLES:
        value = os.environ.get(variable)
        if value:
            return value
    pytest.skip("No PostgreSQL URL configured; set TEST_DATABASE_URL to run these tests.")


async def _reset_public_schema(url: str) -> None:
    """Empty the database completely, extensions included.

    ``DROP SCHEMA public CASCADE`` rather than ``Base.metadata.drop_all``,
    because the things a migration leaves behind are precisely the things the
    metadata does not know about: ``alembic_version``, and any enum type whose
    labels have drifted from the models. Dropping only what the models describe
    would leave a stale ``audit_action`` standing for the next run to reuse -
    which is the failure this whole strategy exists to expose.

    The extensions are recreated immediately afterwards because they live in
    ``public`` and go down with it.
    """
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
            for extension in REQUIRED_EXTENSIONS:
                await connection.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
    finally:
        await engine.dispose()


def _run_migrations(url: str) -> None:
    """``alembic upgrade head``, in this process, against ``url``.

    Deliberately the library entry point rather than a subprocess. A subprocess
    would take the developer's ``DATABASE_URL`` from the environment and quietly
    migrate whichever database that names, which is somebody's working copy; the
    config override below cannot address the wrong database.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    # `env.py` reads settings for the URL, so it is passed as an attribute the
    # environment cannot override, and escaped for ConfigParser exactly as
    # `env.py` escapes its own.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["wasla_database_url"] = url
    command.upgrade(config, "head")


async def _create_from_models(url: str) -> None:
    """The fast build: every table straight off the mapped metadata."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


def _build_schema(url: str) -> None:
    """Build the schema this run tests against, once.

    Synchronous, and each async step gets its own ``asyncio.run``. That is not
    style: ``alembic`` drives its own event loop from ``env.py``, so calling
    ``command.upgrade`` from inside a running loop raises "asyncio.run() cannot
    be called from a running event loop" - which is what a single enclosing
    coroutine here produced.

    The reset before the build still matters: a crashed run must not poison the
    next one. It happens once per session now rather than once per test.
    """
    asyncio.run(_reset_public_schema(url))
    if built_from_migrations():
        _run_migrations(url)
        return
    asyncio.run(_create_from_models(url))


def _drop_schema(url: str) -> None:
    """Remove everything the session created."""
    asyncio.run(_reset_public_schema(url))


@pytest.fixture(scope="session")
def prepared_database(database_url: str) -> Iterator[str]:
    """The schema, built once for the whole session and removed afterwards.

    Deliberately synchronous. ``asyncio.run`` gives this its own event loop and
    closes it before returning, so nothing it opened can be reached from a
    test's loop later - which is the failure mode a session-scoped *async*
    fixture would introduce here. The teardown gets its own loop for the same
    reason.

    **The teardown is not tidiness.** CI points pytest and Alembic at the *same*
    database: it runs the tests, then ``alembic upgrade head`` on what is left
    behind. Tables left standing make that upgrade fail on ``CREATE TABLE ...
    already exists``, which reads as a broken migration when it is nothing of
    the sort.

    That is not hypothetical. An earlier version of this file dropped the schema
    per test, so the last teardown happened to leave the database clean; moving
    the build to session scope removed the drop along with it and turned CI red
    for two commits. CI is the regression test - it runs the two steps in that
    order - so if this teardown disappears again, it will fail there rather than
    here.

    Under ``WASLA_TEST_SCHEMA=migrations`` the teardown carries a second job:
    it drops ``alembic_version`` and every enum type along with the tables, so
    a subsequent run genuinely starts from nothing rather than reusing an enum
    whose labels have since drifted.
    """
    _build_schema(database_url)
    try:
        yield database_url
    finally:
        _drop_schema(database_url)


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
