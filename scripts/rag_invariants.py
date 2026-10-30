"""The knowledge tables' invariants after the RAG remediation (RR-01 .. RR-12).

Run by `python -m scripts.omnichannel_invariants verify` with every other
invariant, and on its own as

    python -m scripts.rag_invariants verify

Each check is a count of rows that break it, so all zeros is a statement about
the data. RR-01, RR-02, RR-03 and RR-08 were guarantees before this stage -
partial unique indexes, composite foreign keys, the publish swap - and are
counted now so a sweep says so. RR-04 .. RR-07 are what the remediation made
true: one copy of a text per workspace, one document per source key, no Arabic
presentation forms, and every hash the hash of the stored text.

RR-07 has two halves. The SQL half proves `content_hash` is the SHA-256 of the
stored text; the Python half proves the stored text is `for_storage` text,
which SQL cannot compute. RR-10 asks the object store whether each original a
generation names is there, for a bounded sample - it never lists the bucket
(ADR-087), so an object no row names is not found here; the upload path
deletes an original whose document was not committed.

RR-12 (Part 2) counts active chunks the exact-word channel cannot find: zero
once `reindex-all` has rebuilt what was published before migration 0132. RR-09
(Part 3) counts active generations outside their workspace's query space while
no embedding-model switch is under way - documents no search can find. RR-11 (a
document past `valid_until` returned by search) is a property of the query,
proved by its test, not of stored rows.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Final

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.core.embedding_space import EMBEDDING_SCHEMA_VERSION, OPENAI_PROVIDER
from app.core.storage import MediaStorage
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.db.session import utc_connect_args
from app.services.text_normalisation import for_storage
from scripts.omnichannel_invariants import Check

RAG_INVARIANTS: Final[tuple[Check, ...]] = (
    Check(
        "rr01_document_with_more_than_one_active_generation",
        "SELECT count(*) FROM (SELECT document_id FROM document_index_generations"
        " WHERE state = 'active' GROUP BY document_id HAVING count(*) > 1) x",
    ),
    Check(
        "rr02_ready_document_not_served_by_one_active_generation_with_chunks",
        "SELECT count(*) FROM documents d WHERE d.status = 'ready' AND NOT EXISTS ("
        " SELECT 1 FROM document_index_generations g WHERE g.document_id = d.id"
        " AND g.tenant_id = d.tenant_id AND g.state = 'active'"
        " AND EXISTS (SELECT 1 FROM document_chunks c WHERE c.generation_id = g.id))",
    ),
    Check(
        # Part 3 (RQ-25): a published shadow's chunks are legitimate, and so are
        # a superseded generation's in a workspace flipped and not yet retired
        # - `embedding-switch retire` deletes them.
        "rr03_chunk_of_a_generation_not_active",
        "SELECT count(*) FROM document_chunks c JOIN document_index_generations g"
        " ON g.id = c.generation_id WHERE g.state NOT IN ('active', 'shadow')"
        " AND NOT (g.state = 'superseded' AND EXISTS (SELECT 1 FROM workspace_embedding_spaces w"
        " WHERE w.tenant_id = g.tenant_id AND w.flipped_at IS NOT NULL AND w.retired_at IS NULL))",
    ),
    Check(
        # Part 3 (RQ-25): an active generation outside the space its workspace
        # is queried in, while no switch is under way - a document no search
        # finds. The workspace's row when it has one, else the configured space
        # (`:provider`, `:model`, `:dimensions`, `:schema_version`).
        "rr09_active_generation_outside_the_query_space",
        "SELECT count(*) FROM document_index_generations g"
        " LEFT JOIN workspace_embedding_spaces w ON w.tenant_id = g.tenant_id"
        " WHERE g.state = 'active' AND (w.target_model IS NULL) AND ("
        " (w.tenant_id IS NULL AND (g.embedding_provider, g.embedding_model,"
        " g.embedding_dimensions, g.embedding_schema_version)"
        " IS DISTINCT FROM (:provider, :model, :dimensions, :schema_version))"
        " OR (w.tenant_id IS NOT NULL AND (g.embedding_provider, g.embedding_model,"
        " g.embedding_dimensions, g.embedding_schema_version) IS DISTINCT FROM"
        " (w.query_provider, w.query_model, w.query_dimensions, w.query_schema_version)))",
    ),
    Check(
        "rr04_ready_text_served_twice_in_a_workspace",
        "SELECT coalesce(sum(copies - 1), 0) FROM (SELECT count(*) AS copies FROM documents"
        " WHERE status = 'ready' GROUP BY tenant_id, content_hash HAVING count(*) > 1) x",
    ),
    Check(
        "rr05_source_key_held_twice_in_a_knowledge_base",
        "SELECT count(*) FROM (SELECT 1 FROM documents WHERE source_key IS NOT NULL"
        " GROUP BY tenant_id, knowledge_base_id, source_key HAVING count(*) > 1) x",
    ),
    Check(
        "rr06_stored_text_in_arabic_presentation_forms",
        # Arabic Presentation Forms-A and -B: what a PDF's glyphs extract as.
        "SELECT (SELECT count(*) FROM documents"
        " WHERE content ~ U&'[\\FB50-\\FDFF\\FE70-\\FEFF]')"
        " + (SELECT count(*) FROM document_chunks"
        " WHERE content ~ U&'[\\FB50-\\FDFF\\FE70-\\FEFF]')",
    ),
    Check(
        "rr07_content_hash_not_the_hash_of_the_stored_text",
        "SELECT count(*) FROM documents WHERE content IS NOT NULL"
        " AND content_hash <> encode(sha256(convert_to(content, 'UTF8')), 'hex')",
    ),
    Check(
        "rr08_chunk_disagreeing_with_its_parents",
        "SELECT count(*) FROM document_chunks c"
        " JOIN documents d ON d.id = c.document_id"
        " JOIN document_index_generations g ON g.id = c.generation_id"
        " WHERE c.tenant_id <> d.tenant_id OR c.tenant_id <> g.tenant_id"
        " OR c.knowledge_base_id <> d.knowledge_base_id OR g.document_id <> c.document_id",
    ),
    Check(
        # RAG part 2 (RQ-15): every chunk an active generation serves is
        # searchable by its exact words. NULL only for chunks published before
        # migration 0132, until `reindex-all` rebuilds them; the vector channel
        # still finds those.
        "rr12_active_chunk_without_lexical_text",
        "SELECT count(*) FROM document_chunks c JOIN document_index_generations g"
        " ON g.id = c.generation_id WHERE g.state = 'active'"
        " AND (c.search_text IS NULL OR c.search_tsv IS NULL)",
    ),
)

NOT_NORMALISED: Final = "rr07_stored_text_not_normalised"
MISSING_ORIGINAL: Final = "rr10_original_named_but_not_stored"
BATCH: Final = 200


async def rag_violations(connection: AsyncConnection) -> dict[str, int]:
    """RR-01 .. RR-08 and RR-12: the SQL counts, then the Python half of RR-07."""
    found = {
        check.name: int(
            (
                await connection.execute(
                    text(check.query), configured_space() if ":provider" in check.query else {}
                )
            ).scalar_one()
        )
        for check in RAG_INVARIANTS
    }
    found[NOT_NORMALISED] = await not_normalised(connection)
    return found


def configured_space() -> dict[str, object]:
    """The space a workspace without a switch is queried in, as RR-09 binds it."""
    return {
        "provider": OPENAI_PROVIDER,
        "model": os.environ.get("OPENAI_EMBEDDING_MODEL") or "text-embedding-3-small",
        "dimensions": EMBEDDING_DIMENSIONS,
        "schema_version": EMBEDDING_SCHEMA_VERSION,
    }


async def not_normalised(connection: AsyncConnection) -> int:
    """Documents whose stored text `for_storage` would still change (RR-07)."""
    broken = 0
    after: uuid.UUID | None = None
    while True:
        rows = (
            await connection.execute(
                text(
                    "SELECT id, content FROM documents WHERE content IS NOT NULL"
                    " AND (CAST(:after AS uuid) IS NULL OR id > CAST(:after AS uuid))"
                    " ORDER BY id LIMIT :batch"
                ),
                {"after": after, "batch": BATCH},
            )
        ).all()
        if not rows:
            return broken
        broken += sum(1 for _, content in rows if for_storage(content) != content)
        after = rows[-1][0]


async def missing_originals(
    connection: AsyncConnection, storage: MediaStorage, *, sample: int = 500
) -> dict[str, int]:
    """RR-10: originals a generation names that the store does not hold, in a sample."""
    keys = (
        await connection.scalars(
            text(
                "SELECT DISTINCT original_object_key FROM document_index_generations"
                " WHERE original_object_key IS NOT NULL ORDER BY 1 LIMIT :sample"
            ),
            {"sample": sample},
        )
    ).all()
    missing = 0
    for key in keys:
        if not await storage.exists(key):
            missing += 1
    return {MISSING_ORIGINAL: missing}


async def _verify(url: str) -> int:
    engine = create_async_engine(url, poolclass=NullPool, connect_args=utc_connect_args(url))
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            found = await rag_violations(connection)
    finally:
        await engine.dispose()
    for name, count in found.items():
        sys.stdout.write(f"{name}: {count}\n")
    broken = sum(1 for count in found.values() if count)
    sys.stdout.write(f"rag_invariants verify: {'ok' if not broken else f'{broken} violated'}\n")
    return 1 if broken else 0


def main(argv: list[str]) -> int:
    url = os.environ.get("INVARIANTS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if argv != ["verify"] or not url:
        sys.stderr.write(
            "usage: python -m scripts.rag_invariants verify"
            " (INVARIANTS_DATABASE_URL or DATABASE_URL)\n"
        )
        return 64
    return asyncio.run(_verify(url))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main(sys.argv[1:]))
