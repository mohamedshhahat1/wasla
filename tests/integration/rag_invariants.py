"""The knowledge tables' invariants, as counts of rows that break them.

One query per invariant, each returning how many rows violate it - so a result
of all zeros is a statement about the data, and `presence` says how much data
there was to make it about. A sweep over an empty database is not evidence of
anything, which is why every caller asserts on `presence` first.

Scoped to a set of workspaces when given one (a test sweeping what it created,
beside other tests' rows), or to the whole database (the remediation report's
sweep of everything the runtime probes left).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace
from app.services.document_indexing import CLAIM_LEASE, MAX_INDEXING_ATTEMPTS

# `{scope}` is replaced by a tenant predicate on the named alias, or `TRUE`.
VIOLATIONS: Final[dict[str, str]] = {
    "chunks_without_document": """
        SELECT count(*) FROM document_chunks c
        LEFT JOIN documents d ON d.id = c.document_id
        WHERE d.id IS NULL AND {scope:c}""",
    "chunks_crossing_tenants": """
        SELECT count(*) FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        JOIN document_index_generations g ON g.id = c.generation_id
        WHERE (c.tenant_id <> d.tenant_id OR c.tenant_id <> g.tenant_id) AND {scope:c}""",
    "chunk_knowledge_base_differs_from_document": """
        SELECT count(*) FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.knowledge_base_id <> d.knowledge_base_id AND {scope:c}""",
    "chunk_generation_of_another_document": """
        SELECT count(*) FROM document_chunks c
        JOIN document_index_generations g ON g.id = c.generation_id
        WHERE g.document_id <> c.document_id AND {scope:c}""",
    "duplicate_generation_ordinal": """
        SELECT count(*) FROM (
          SELECT generation_id, ordinal FROM document_chunks c WHERE {scope:c}
          GROUP BY generation_id, ordinal HAVING count(*) > 1) x""",
    "duplicate_generation_number": """
        SELECT count(*) FROM (
          SELECT document_id, number FROM document_index_generations g WHERE {scope:g}
          GROUP BY document_id, number HAVING count(*) > 1) x""",
    "multiple_active_generations": """
        SELECT count(*) FROM (
          SELECT document_id FROM document_index_generations g
          WHERE state = 'active' AND {scope:g} GROUP BY document_id HAVING count(*) > 1) x""",
    "multiple_in_flight_generations": """
        SELECT count(*) FROM (
          SELECT document_id FROM document_index_generations g
          WHERE state IN ('pending', 'processing') AND {scope:g}
          GROUP BY document_id HAVING count(*) > 1) x""",
    "active_generation_without_chunks": """
        SELECT count(*) FROM document_index_generations g
        WHERE state = 'active' AND {scope:g}
          AND NOT EXISTS (SELECT 1 FROM document_chunks c WHERE c.generation_id = g.id)""",
    "active_generation_chunk_count_mismatch": """
        SELECT count(*) FROM document_index_generations g
        WHERE state = 'active' AND {scope:g}
          AND chunk_count <> (SELECT count(*) FROM document_chunks c
                              WHERE c.generation_id = g.id)""",
    "active_chunk_missing_embedding": """
        SELECT count(*) FROM document_chunks c
        JOIN document_index_generations g ON g.id = c.generation_id
        WHERE g.state = 'active' AND c.embedding IS NULL AND {scope:c}""",
    "active_chunk_zero_or_non_finite_embedding": """
        SELECT count(*) FROM document_chunks c
        JOIN document_index_generations g ON g.id = c.generation_id
        WHERE g.state = 'active' AND c.embedding IS NOT NULL AND {scope:c}
          AND NOT (vector_norm(c.embedding) > 1e-6
                   AND vector_norm(c.embedding) < 'Infinity'::float8)""",
    "chunks_of_a_generation_not_active": """
        SELECT count(*) FROM document_chunks c
        JOIN document_index_generations g ON g.id = c.generation_id
        WHERE g.state <> 'active' AND {scope:c}""",
    "published_generation_without_embedding_identity": """
        SELECT count(*) FROM document_index_generations g
        WHERE state IN ('active', 'superseded') AND {scope:g}
          AND (embedding_model IS NULL OR embedding_dimensions IS NULL
               OR embedding_provider IS NULL OR published_at IS NULL)""",
    "processing_past_its_lease": """
        SELECT count(*) FROM document_index_generations g
        WHERE state = 'processing' AND claimed_at < :lease_cutoff AND {scope:g}""",
    "pending_past_its_attempt_budget": """
        SELECT count(*) FROM document_index_generations g
        WHERE state = 'pending' AND attempts >= :max_attempts AND {scope:g}""",
    "ready_document_without_active_generation": """
        SELECT count(*) FROM documents d
        WHERE status = 'ready' AND {scope:d}
          AND NOT EXISTS (SELECT 1 FROM document_index_generations g
                          WHERE g.document_id = d.id AND g.state = 'active')""",
    "unready_document_with_active_generation": """
        SELECT count(*) FROM documents d
        WHERE status <> 'ready' AND {scope:d}
          AND EXISTS (SELECT 1 FROM document_index_generations g
                      WHERE g.document_id = d.id AND g.state = 'active')""",
    "failed_document_with_chunks": """
        SELECT count(*) FROM documents d
        WHERE status = 'failed' AND {scope:d}
          AND EXISTS (SELECT 1 FROM document_chunks c WHERE c.document_id = d.id)""",
    "served_before_but_nothing_serves_now": """
        SELECT count(*) FROM documents d
        WHERE {scope:d}
          AND EXISTS (SELECT 1 FROM document_index_generations g
                      WHERE g.document_id = d.id AND g.state = 'superseded')
          AND NOT EXISTS (SELECT 1 FROM document_index_generations g
                          WHERE g.document_id = d.id AND g.state = 'active')""",
    "processing_in_a_workspace_not_served": """
        SELECT count(*) FROM document_index_generations g
        JOIN tenants t ON t.id = g.tenant_id
        WHERE g.state = 'processing' AND {scope:g}
          AND (t.status <> 'active' OR t.deleted_at IS NOT NULL)""",
    "rag_rows_of_purged_workspaces": """
        SELECT (SELECT count(*) FROM knowledge_bases x JOIN tenants t ON t.id = x.tenant_id
                 WHERE t.purged_at IS NOT NULL)
             + (SELECT count(*) FROM documents x JOIN tenants t ON t.id = x.tenant_id
                 WHERE t.purged_at IS NOT NULL)
             + (SELECT count(*) FROM document_index_generations x
                 JOIN tenants t ON t.id = x.tenant_id WHERE t.purged_at IS NOT NULL)
             + (SELECT count(*) FROM document_chunks x JOIN tenants t ON t.id = x.tenant_id
                 WHERE t.purged_at IS NOT NULL)""",
    "impossible_embedding_usage": """
        SELECT count(*) FROM usage_events u
        WHERE event_type IN ('embedding_request', 'embedding_input_token') AND {scope:u}
          AND (quantity <= 0 OR metadata IS NULL OR NOT (metadata ? 'model')
               OR coalesce(metadata ->> 'purpose', '') NOT IN ('ingest', 'query'))""",
}

PRESENCE: Final[dict[str, str]] = {
    "tenants": "SELECT count(DISTINCT tenant_id) FROM documents d WHERE {scope:d}",
    "documents": "SELECT count(*) FROM documents d WHERE {scope:d}",
    "chunks": "SELECT count(*) FROM document_chunks c WHERE {scope:c}",
    "active_generations": (
        "SELECT count(*) FROM document_index_generations g WHERE state = 'active' AND {scope:g}"
    ),
    "superseded_generations": (
        "SELECT count(*) FROM document_index_generations g "
        "WHERE state = 'superseded' AND {scope:g}"
    ),
    "failed_generations": (
        "SELECT count(*) FROM document_index_generations g WHERE state = 'failed' AND {scope:g}"
    ),
    # Not a violation: a document served from an embedding space other than the
    # configured one is a legal state after a model change, and what RAG-06
    # requires is that it is visible (`needs_reindex`, the stale-embedding gauge
    # and command) and never compared with the configured space's queries -
    # which the search filter and its tests prove. Counted, so a sweep report
    # shows how much of it there was.
    "active_generations_in_another_embedding_space": """
        SELECT count(*) FROM document_index_generations g
        WHERE state = 'active' AND {scope:g}
          AND (embedding_provider, embedding_model, embedding_dimensions,
               embedding_schema_version)
              IS DISTINCT FROM (:provider, :model, :dimensions, :schema_version)""",
    "embedding_usage_rows": (
        "SELECT count(*) FROM usage_events u "
        "WHERE event_type IN ('embedding_request', 'embedding_input_token') AND {scope:u}"
    ),
}


@dataclass(frozen=True, slots=True)
class SweepResult:
    violations: dict[str, int]
    presence: dict[str, int]


def _scoped(statement: str, tenants: Sequence[uuid.UUID] | None) -> str:
    for alias in ("c", "g", "d", "u"):
        predicate = "TRUE" if tenants is None else f"{alias}.tenant_id = ANY(:tenants)"
        statement = statement.replace("{scope:" + alias + "}", predicate)
    return statement


async def sweep(
    session: AsyncSession,
    *,
    space: EmbeddingSpace,
    tenants: Sequence[uuid.UUID] | None = None,
) -> SweepResult:
    parameters: dict[str, object] = {
        "lease_cutoff": datetime.now(UTC) - CLAIM_LEASE,
        "max_attempts": MAX_INDEXING_ATTEMPTS,
        "provider": space.provider,
        "model": space.model,
        "dimensions": space.dimensions,
        "schema_version": space.schema_version,
    }
    if tenants is not None:
        parameters["tenants"] = list(tenants)

    async def count(statement: str) -> int:
        sql = _scoped(statement, tenants)
        used = {key: value for key, value in parameters.items() if f":{key}" in sql}
        return int(await session.scalar(text(sql), used) or 0)

    return SweepResult(
        violations={name: await count(sql) for name, sql in VIOLATIONS.items()},
        presence={name: await count(sql) for name, sql in PRESENCE.items()},
    )
