"""A comparable description of a database schema, read from `pg_catalog`.

Two uses. `test_schema_parity.py` builds one database with `create_all` and one
with `alembic upgrade head` and requires the two descriptions to be equal, which
is the independent catalog diff the database audit ran by hand: columns,
constraints, indexes, triggers, trigger-function bodies and settings, enum
labels *in order*, and table storage options. And an operator can compare two
live databases with it:

    python -m scripts.schema_catalog postgresql://.../a postgresql://.../b

which prints the differences and exits 1 if there are any.

Function bodies are compared with whitespace removed: the models and the
migrations indent the same SQL differently, and that is not a difference.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

# Alembic's own bookkeeping, which only one of the two builds has.
IGNORED_TABLES = frozenset({"alembic_version"})

_WHITESPACE = re.compile(r"\s+")

_QUERIES: dict[str, str] = {
    "columns": """
        SELECT c.relname, a.attname,
               format_type(a.atttypid, a.atttypmod),
               a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid),
               a.attidentity, a.attgenerated
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE n.nspname = current_schema() AND c.relkind = 'r'
           AND a.attnum > 0 AND NOT a.attisdropped
    """,
    "constraints": """
        SELECT c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid),
               k.convalidated, k.condeferrable, k.condeferred
          FROM pg_constraint k
          JOIN pg_class c ON c.oid = k.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = current_schema()
    """,
    "indexes": """
        SELECT t.relname, i.relname, pg_get_indexdef(x.indexrelid), x.indisvalid
          FROM pg_index x
          JOIN pg_class i ON i.oid = x.indexrelid
          JOIN pg_class t ON t.oid = x.indrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = current_schema()
    """,
    "triggers": """
        SELECT c.relname, g.tgname, pg_get_triggerdef(g.oid), g.tgenabled
          FROM pg_trigger g
          JOIN pg_class c ON c.oid = g.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = current_schema() AND NOT g.tgisinternal
    """,
    # Functions an extension installed (vector's, pgcrypto's) are the
    # extension's business, not the schema's.
    "functions": """
        SELECT p.proname, pg_get_function_identity_arguments(p.oid), p.prosrc,
               coalesce(array_to_string(p.proconfig, ','), ''), p.prosecdef, p.provolatile
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = current_schema()
           AND NOT EXISTS (
               SELECT 1 FROM pg_depend d
                WHERE d.objid = p.oid AND d.classid = 'pg_proc'::regclass AND d.deptype = 'e')
    """,
    "enums": """
        SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
          FROM pg_enum e
          JOIN pg_type t ON t.oid = e.enumtypid
          JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = current_schema()
         GROUP BY t.typname
    """,
    "storage": """
        SELECT c.relname, coalesce(array_to_string(c.reloptions, ','), '')
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = current_schema() AND c.relkind = 'r'
    """,
}


def _row(kind: str, row: tuple[Any, ...]) -> tuple[str, str] | None:
    """One catalog row as (key, comparable value), or None if it is ignored."""
    values = [str(value) if value is not None else "" for value in row]
    if kind != "functions" and kind != "enums" and values[0] in IGNORED_TABLES:
        return None
    if kind == "functions":
        name, arguments, body, *rest = values
        return f"{name}({arguments})", "|".join([_WHITESPACE.sub("", body), *rest])
    key_width = {"columns": 2, "constraints": 2, "indexes": 2, "triggers": 2}.get(kind, 1)
    return ".".join(values[:key_width]), "|".join(values[key_width:])


async def snapshot(connection: AsyncConnection) -> dict[str, dict[str, str]]:
    """The schema `connection` sees, as {object class: {object: definition}}."""
    found: dict[str, dict[str, str]] = {}
    for kind, query in _QUERIES.items():
        section: dict[str, str] = {}
        for row in await connection.execute(text(query)):
            entry = _row(kind, tuple(row))
            if entry is not None:
                section[entry[0]] = entry[1]
        found[kind] = section
    return found


def differences(
    left: dict[str, dict[str, str]], right: dict[str, dict[str, str]]
) -> dict[str, dict[str, dict[str, str | None]]]:
    """Every object present in only one snapshot or defined differently."""
    found: dict[str, dict[str, dict[str, str | None]]] = {}
    for kind in sorted(set(left) | set(right)):
        a, b = left.get(kind, {}), right.get(kind, {})
        section = {
            name: {"left": a.get(name), "right": b.get(name)}
            for name in sorted(set(a) | set(b))
            if a.get(name) != b.get(name)
        }
        if section:
            found[kind] = section
    return found


async def _snapshot_url(url: str) -> dict[str, dict[str, str]]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await snapshot(connection)
    finally:
        await engine.dispose()


async def _main(left_url: str, right_url: str) -> int:
    found = differences(await _snapshot_url(left_url), await _snapshot_url(right_url))
    if found:
        sys.stdout.write(json.dumps(found, indent=2, sort_keys=True) + "\n")
        return 1
    sys.stdout.write("schemas identical\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    if len(sys.argv) != 3:
        sys.exit("usage: python -m scripts.schema_catalog <url-a> <url-b>")
    sys.exit(asyncio.run(_main(sys.argv[1], sys.argv[2])))
