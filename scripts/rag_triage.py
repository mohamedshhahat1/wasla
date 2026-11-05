"""Read-only knowledge triage for an operator (FX-0.2 .. FX-0.4, RAG_SYSTEM_REVIEW.md App. C).

    docker compose exec worker python -m scripts.rag_triage

Answers, per workspace, the questions the review's triage SQL asks - and only
with workspace ids and counts. A title, a filename or a passage is a
workspace's own words and never leaves this command; an operator who needs to
see one goes through the workspace's own API, as the workspace.

    pgvector                 the installed version, and whether search can run on it (RV-03)
    duplicate-titles         ready documents sharing a title in one workspace (RV-02)
    cross-kb-copies          identical text in more than one knowledge base (RV-07)
    presentation-forms       documents stored as Arabic presentation forms (RV-01)
    extraction-warnings      documents whose extraction flagged re-ordered lines (RV-04)
    unreachable-knowledge    ready documents and no answering agent able to search them (RC-7)

It writes nothing, takes no lock, and is safe to run against production.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.pgvector_version import installed_version, version_problem
from app.db.session import utc_connect_args

# Each answers (tenant_id, count). Titles are compared case-folded, which is
# how a person would call two titles "the same".
QUERIES: Final[dict[str, str]] = {
    "duplicate-titles": """
        SELECT tenant_id, count(*) FROM (
            SELECT tenant_id, lower(title) FROM documents
            WHERE status = 'ready'
            GROUP BY tenant_id, lower(title) HAVING count(*) > 1
        ) AS groups GROUP BY tenant_id ORDER BY tenant_id""",
    "cross-kb-copies": """
        SELECT tenant_id, count(*) FROM (
            SELECT tenant_id, content_hash FROM documents
            GROUP BY tenant_id, content_hash
            HAVING count(DISTINCT knowledge_base_id) > 1
        ) AS groups GROUP BY tenant_id ORDER BY tenant_id""",
    "presentation-forms": """
        SELECT tenant_id, count(*) FROM documents
        WHERE content ~ '[\\uFB50-\\uFDFF\\uFE70-\\uFEFF]'
        GROUP BY tenant_id ORDER BY tenant_id""",
    "extraction-warnings": """
        SELECT tenant_id, count(*) FROM documents
        WHERE extraction_warnings IS NOT NULL
        GROUP BY tenant_id ORDER BY tenant_id""",
    # Only the ACTIVE default agent answers a workspace (`plan_turn`), so that
    # is the agent whose grant decides whether knowledge is reachable.
    "unreachable-knowledge": """
        SELECT d.tenant_id, count(*) FROM documents d
        WHERE d.status = 'ready'
          AND NOT EXISTS (
                SELECT 1 FROM agents a JOIN agent_tools t ON t.agent_id = a.id
                WHERE a.tenant_id = d.tenant_id AND t.tenant_id = d.tenant_id
                  AND a.status = 'active' AND a.is_default
                  AND t.name = 'search_knowledge' AND t.enabled)
        GROUP BY d.tenant_id ORDER BY d.tenant_id""",
}


async def triage(connection: AsyncConnection) -> dict[str, list[tuple[str, int]]]:
    """Every check's rows, as (workspace id, count)."""
    report: dict[str, list[tuple[str, int]]] = {}
    for name, query in QUERIES.items():
        rows = (await connection.execute(text(query))).all()
        report[name] = [(str(tenant_id), int(count)) for tenant_id, count in rows]
    return report


async def _run(url: str, only: Sequence[str]) -> int:
    engine = create_async_engine(url, poolclass=NullPool, connect_args=utc_connect_args(url))
    try:
        async with engine.connect() as connection:
            version = await installed_version(connection)
            problem = version_problem(version)
            sys.stdout.write(f"pgvector: {version or 'not installed'}")
            sys.stdout.write(f" - {problem}\n" if problem else " - ok\n")
            report = await triage(connection)
    finally:
        await engine.dispose()
    findings = 1 if problem else 0
    for name, rows in report.items():
        if only and name not in only:
            continue
        sys.stdout.write(f"{name}: {len(rows)} workspace(s)\n")
        for tenant_id, count in rows:
            sys.stdout.write(f"  {tenant_id}  {count}\n")
        findings += len(rows)
    return 1 if findings else 0


def main(argv: list[str]) -> int:
    unknown = [name for name in argv if name not in QUERIES]
    if unknown:
        sys.stderr.write(f"usage: python -m scripts.rag_triage [{'|'.join(QUERIES)} ...]\n")
        return 64
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.stderr.write("rag_triage: DATABASE_URL is required\n")
        return 64
    return asyncio.run(_run(url, argv))


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main(sys.argv[1:]))
