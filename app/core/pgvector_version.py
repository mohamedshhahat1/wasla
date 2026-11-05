"""The pgvector version knowledge search needs, and how to tell what a database has.

Every knowledge search starts by setting `hnsw.iterative_scan`
(`DocumentChunkRepository._prepare_the_planner`), a setting pgvector introduced
in 0.8.0. On an older pgvector the statement is refused - "hnsw" is a reserved
prefix once the library is loaded - so every search fails and every agent tells
customers the knowledge base cannot be searched (RV-03). The database that
fails this way looks healthy to everything else, which is why the version is
checked where a deployment decides whether it is fit to serve: the preflight
before migrating, and readiness before taking traffic (FX-1.3).
"""

from __future__ import annotations

import re
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

MINIMUM_PGVECTOR: Final = (0, 8, 0)
MINIMUM_PGVECTOR_LABEL: Final = "0.8.0"

_INSTALLED: Final = text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
_AVAILABLE: Final = text(
    "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'"
)
_NUMBERS: Final = re.compile(r"\d+")


def parse_version(value: str) -> tuple[int, ...]:
    """`"0.10.1"` as `(0, 10, 1)`.

    Compared as numbers, never as strings: `"0.10.0" < "0.8.0"` as text, and a
    guard comparing text would refuse the newer extension and admit nothing.
    """
    return tuple(int(part) for part in _NUMBERS.findall(value))


def version_problem(version: str | None) -> str | None:
    """Why this pgvector cannot serve knowledge search, or None when it can.

    `None` - no pgvector at all - is not this check's concern: the migrations
    create the extension, and the preflight reports a missing one on its own.
    """
    if version is None:
        return None
    parsed = parse_version(version)
    if not parsed or parsed < MINIMUM_PGVECTOR:
        return (
            f"pgvector {version} is older than {MINIMUM_PGVECTOR_LABEL}, which knowledge "
            "search needs (hnsw.iterative_scan). Upgrade the extension."
        )
    return None


async def installed_version(connection: AsyncConnection) -> str | None:
    """The installed pgvector, or the version `CREATE EXTENSION` would install."""
    version = (await connection.execute(_INSTALLED)).scalar_one_or_none()
    if version is None:
        version = (await connection.execute(_AVAILABLE)).scalar_one_or_none()
    return None if version is None else str(version)


async def pgvector_problem(connection: AsyncConnection) -> str | None:
    """Why this database's pgvector cannot serve knowledge search, or None."""
    return version_problem(await installed_version(connection))


__all__ = [
    "MINIMUM_PGVECTOR",
    "MINIMUM_PGVECTOR_LABEL",
    "installed_version",
    "parse_version",
    "pgvector_problem",
    "version_problem",
]
