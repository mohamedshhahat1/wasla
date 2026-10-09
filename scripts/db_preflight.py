"""Checks run around a migration or a restore, so a bad state fails loudly.

    python -m scripts.db_preflight prerequisites   # before `alembic upgrade head`
    python -m scripts.db_preflight verify          # after it

The URL is `MIGRATION_DATABASE_URL`, else `DATABASE_URL`; the same identity
the migration itself uses.

**prerequisites (DB-010).** Migration 0001 runs `CREATE EXTENSION vector`, and
pgvector is not a trusted extension: only a superuser can create it. A migration
identity that is the database owner but not a superuser - which is what a
managed provider hands out, and what docs/DEPLOYMENT.md recommends - fails there
with "permission denied to create extension", half-way into a deploy or a
disaster recovery. This asks first. An extension already installed (by a
superuser, or by the provider's allow-list) passes; `CREATE EXTENSION IF NOT
EXISTS` is then a no-op for any role. One not installed is tried inside a
transaction that is rolled back, so the check changes nothing either way.

**verify (DB-027).** `alembic upgrade` exiting 0 does not mean the schema is
whole. A constraint added `NOT VALID` whose `VALIDATE` was skipped - 0071 turns
violations into a WARNING - and an index a failed `CREATE INDEX CONCURRENTLY`
left `INVALID` are both silent, and both mean a rule the code relies on is not
being enforced. Neither is expected after a successful migration of this
repository, so any is a failure. A disabled trigger is the third: every trigger
here guards an invariant.

**verify: what the code declares and the database lacks (PO-02, MIG-0091).**
The checks above only see objects that exist. An index that is simply *gone*
was invisible: a downgrade crossing 0091 that a lower migration refused left
the database stamped 0091 without 0091's index, `alembic upgrade head` never
rebuilt it, and verify said ok. So verify also reads every table, index,
unique constraint, primary key, check constraint and foreign key the model
metadata (`Base.metadata`) declares, under the name the naming convention
gives it, and reports each one the database does not have. The comparison is
by name and exact - the schema-parity suite proves the models and the
migrations build the same catalogue, name for name - so nothing needs to be
excused: `MIGRATION_ONLY_INDEXES` is empty, and an entry there must say why.
Objects in the database that the models do not declare are not reported here;
the parity suite owns that direction.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from sqlalchemy import (
    CheckConstraint,
    Constraint,
    ForeignKeyConstraint,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.compiler import IdentifierPreparer

REQUIRED_EXTENSIONS = ("vector", "pgcrypto")

UNVALIDATED_CONSTRAINTS = """
    SELECT format('%s.%s', c.relname, k.conname)
      FROM pg_constraint k
      JOIN pg_class c ON c.oid = k.conrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND NOT k.convalidated
     ORDER BY 1
"""
INVALID_INDEXES = """
    SELECT format('%s.%s', t.relname, i.relname)
      FROM pg_index x
      JOIN pg_class i ON i.oid = x.indexrelid
      JOIN pg_class t ON t.oid = x.indrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = current_schema() AND NOT (x.indisvalid AND x.indisready)
     ORDER BY 1
"""
DISABLED_TRIGGERS = """
    SELECT format('%s.%s', c.relname, g.tgname)
      FROM pg_trigger g
      JOIN pg_class c ON c.oid = g.tgrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND NOT g.tgisinternal AND g.tgenabled = 'D'
     ORDER BY 1
"""

EXISTING_TABLES = """
    SELECT c.relname
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')
"""
EXISTING_INDEXES = """
    SELECT t.relname, i.relname
      FROM pg_index x
      JOIN pg_class i ON i.oid = x.indexrelid
      JOIN pg_class t ON t.oid = x.indrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = current_schema()
"""
EXISTING_CONSTRAINTS = """
    SELECT c.relname, k.conname, k.contype::text
      FROM pg_constraint k
      JOIN pg_class c ON c.oid = k.conrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema()
"""

# Indexes a migration creates on purpose and the models do not declare, each
# with its reason. Empty: the parity suite keeps the two catalogues identical.
# This only matters if the models ever declare an index the migrations leave
# out on purpose - list it here by name, never by pattern.
MIGRATION_ONLY_INDEXES: dict[str, str] = {}

_CONSTRAINT_KINDS: dict[type[Constraint], tuple[str, str]] = {
    PrimaryKeyConstraint: ("p", "primary key"),
    UniqueConstraint: ("u", "unique constraint"),
    CheckConstraint: ("c", "check constraint"),
    ForeignKeyConstraint: ("f", "foreign key"),
}


@dataclass(frozen=True)
class Declared:
    """What the models declare, by the names the database is expected to use."""

    tables: frozenset[str]
    indexes: frozenset[tuple[str, str]]
    constraints: frozenset[tuple[str, str, str]]


def declared_schema(metadata: MetaData | None = None) -> Declared:
    """Every table, index and named constraint `metadata` declares (default: the app's)."""
    if metadata is None:
        from app.db.models import Base

        metadata = Base.metadata
    preparer = PGDialect().identifier_preparer  # type: ignore[no-untyped-call]
    tables: set[str] = set()
    indexes: set[tuple[str, str]] = set()
    constraints: set[tuple[str, str, str]] = set()
    for table in metadata.sorted_tables:
        tables.add(table.name)
        for index in table.indexes:
            indexes.add((table.name, _name(preparer, index)))
        for constraint in table.constraints:
            kind = _CONSTRAINT_KINDS.get(type(constraint))
            if kind is not None:
                constraints.add((table.name, _name(preparer, constraint), kind[0]))
    return Declared(frozenset(tables), frozenset(indexes), frozenset(constraints))


def _name(preparer: IdentifierPreparer, item: Index | Constraint) -> str:
    # The naming convention and PostgreSQL's 63-byte truncation, applied as DDL would.
    name = preparer.format_constraint(item)
    if name is None:  # pragma: no cover - every model object is named by the convention
        raise RuntimeError(f"an unnamed schema object on {item!r}")
    return name.strip('"')


_KIND_LABEL = dict(_CONSTRAINT_KINDS.values())


@dataclass(frozen=True)
class Problem:
    """One thing that makes the database unfit to migrate or to serve."""

    kind: str
    name: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.name}"


async def _can_create(connection: AsyncConnection, extension: str) -> bool:
    transaction = await connection.begin()
    try:
        await connection.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
        return True
    except Exception:
        return False
    finally:
        await transaction.rollback()


async def prerequisite_problems(connection: AsyncConnection) -> list[Problem]:
    """Extensions the migrations need and this identity cannot provide."""
    problems: list[Problem] = []
    for extension in REQUIRED_EXTENSIONS:
        installed = (
            await connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = :name"), {"name": extension}
            )
        ).first()
        await connection.commit()
        if installed is None and not await _can_create(connection, extension):
            problems.append(Problem("extension not installed and not creatable", extension))
    return problems


async def verification_problems(
    connection: AsyncConnection, declared: Declared | None = None
) -> list[Problem]:
    """Rules the schema declares and is not enforcing, and objects it lacks.

    `declared` defaults to the application's models; a test checking a
    scratch database of its own passes what that database is meant to hold.
    """
    problems: list[Problem] = []
    for kind, query in (
        ("constraint not validated", UNVALIDATED_CONSTRAINTS),
        ("index invalid", INVALID_INDEXES),
        ("trigger disabled", DISABLED_TRIGGERS),
    ):
        names = (await connection.execute(text(query))).scalars()
        problems += [Problem(kind, name) for name in names]
    return problems + await missing_problems(connection, declared)


async def missing_problems(
    connection: AsyncConnection, declared: Declared | None = None
) -> list[Problem]:
    """What the models declare and this database does not have (PO-02).

    Names only - the table and the object - never a row.
    """
    expected = declared if declared is not None else declared_schema()
    tables = set((await connection.execute(text(EXISTING_TABLES))).scalars())
    indexes = {(row[0], row[1]) for row in await connection.execute(text(EXISTING_INDEXES))}
    constraints = {
        (row[0], row[1], row[2]) for row in await connection.execute(text(EXISTING_CONSTRAINTS))
    }
    problems = [Problem("table missing", name) for name in sorted(expected.tables - tables)]
    # Constraints PostgreSQL backs with an index (primary keys, uniques) are
    # reported once, as the constraint.
    backed = {(table, name) for table, name, kind in expected.constraints if kind in ("p", "u")}
    for table, name in sorted(expected.indexes - indexes - backed):
        if table in tables and name not in MIGRATION_ONLY_INDEXES:
            problems.append(Problem("index missing", f"{table}.{name}"))
    for table, name, kind in sorted(expected.constraints - constraints):
        if table in tables:
            problems.append(Problem(f"{_KIND_LABEL[kind]} missing", f"{table}.{name}"))
    return problems


PREREQUISITE_ADVICE = """\
The migration identity cannot install a required extension. pgvector is not a
trusted extension, so one of these must happen before migrating or restoring:
  - self-hosted PostgreSQL: a superuser runs, in this database,
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE EXTENSION IF NOT EXISTS pgcrypto;
  - a managed provider: enable `vector` (and `pgcrypto`) through its extension
    allow-list, then create them as the provider documents.
See docs/BACKUP.md, "Restore prerequisites"."""


async def _run(check: str, url: str) -> int:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            if check == "prerequisites":
                problems = await prerequisite_problems(connection)
            else:
                problems = await verification_problems(connection)
    finally:
        await engine.dispose()
    for problem in problems:
        sys.stderr.write(f"db_preflight {check}: {problem}\n")
    if problems and check == "prerequisites":
        sys.stderr.write(PREREQUISITE_ADVICE + "\n")
    if not problems:
        sys.stdout.write(f"db_preflight {check}: ok\n")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("prerequisites", "verify"):
        sys.stderr.write("usage: python -m scripts.db_preflight prerequisites|verify\n")
        return 64
    url = os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.stderr.write("db_preflight: MIGRATION_DATABASE_URL or DATABASE_URL is required\n")
        return 64
    return asyncio.run(_run(argv[0], url))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main(sys.argv[1:]))
