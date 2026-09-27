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
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

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


async def verification_problems(connection: AsyncConnection) -> list[Problem]:
    """Rules the schema declares and is not enforcing."""
    problems: list[Problem] = []
    for kind, query in (
        ("constraint not validated", UNVALIDATED_CONSTRAINTS),
        ("index invalid", INVALID_INDEXES),
        ("trigger disabled", DISABLED_TRIGGERS),
    ):
        names = (await connection.execute(text(query))).scalars()
        problems += [Problem(kind, name) for name in names]
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
