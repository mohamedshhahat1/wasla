"""The approximate index, and the two ways it can be wrong.

`test_knowledge_rag.py` proves retrieval is correct and tenant-isolated. This
file is about the index underneath it, and it exists because "add an HNSW index"
has two failure modes that a passing retrieval suite does not notice.

**The opclass can disagree with the operator.** `search` orders by `<=>`, so the
index must be `vector_cosine_ops`. Built with `vector_l2_ops` it is catalogued,
occupies as much disk, and is never once considered by the planner - retrieval
stays exactly as slow as it was and nothing says so.

**The approximate scan can answer with fewer passages than were asked for.**
pgvector indexes one column, so the tenant, knowledge-base and READY filters are
applied *after* the index has chosen its candidates. By default the scan visits
`ef_search` candidates in global distance order and answers with whichever
survive the filters, which for a workspace holding a small share of the corpus
is close to none. The agent is then told the knowledge base had no answer, and
that is a lie the system cannot detect: the query succeeded, the documents are
there, and the reply is "I do not have that information" (ADR-079).

The plan assertions here defeat the index's alternatives on purpose. On a corpus
this size PostgreSQL is right to prefer the exact scan, and these tests are
about whether the approximate path is *correct when taken*, not about when it is
taken. The measurement of when it is taken - the cost crossover, at roughly
26,000 chunks in one workspace - is a local drill recorded in `docs/RAG.md`,
because seeding that corpus takes minutes and hundreds of megabytes.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.db.models.knowledge import DocumentChunk, DocumentStatus
from app.repositories.knowledge_repository import DocumentChunkRepository
from tests.integration.vector_corpus import (
    ANN_INDEX,
    Corpus,
    clustered_vector,
    seed_corpus,
)

pytestmark = pytest.mark.integration

TOP_K = 5


# ---------------------------------------------------------------- the schema


async def test_the_chunk_embedding_carries_an_ann_index(db_connection: AsyncConnection) -> None:
    """A schema property, read off the catalogue rather than off the migration."""
    indexes = await db_connection.run_sync(
        lambda sync: inspect(sync).get_indexes("document_chunks")
    )
    names = {index["name"] for index in indexes}

    assert ANN_INDEX in names


async def test_the_ann_index_is_built_for_the_operator_retrieval_uses(
    db_connection: AsyncConnection,
) -> None:
    """`vector_cosine_ops`, because `search` orders by `<=>`.

    Asserted against `pg_am`/`pg_opclass` rather than against the index
    definition text, so a rename or a reformatting of the DDL does not fail it
    and a genuinely mismatched opclass does.
    """
    row = (
        await db_connection.execute(
            text(
                "SELECT am.amname, opc.opcname"
                " FROM pg_index i"
                " JOIN pg_class c ON c.oid = i.indexrelid"
                " JOIN pg_am am ON am.oid = c.relam"
                " JOIN pg_opclass opc ON opc.oid = i.indclass[0]"
                " WHERE c.relname = :name"
            ),
            {"name": ANN_INDEX},
        )
    ).one()

    assert row.amname == "hnsw"
    assert row.opcname == "vector_cosine_ops"


# ------------------------------------------------- the approximate path works


async def test_the_approximate_scan_returns_every_passage_that_was_asked_for(
    db_session: AsyncSession,
) -> None:
    """The regression test for a silently short answer.

    The workspace under test holds 2% of the corpus. Without the iterative
    setting `search` applies, the approximate scan visits its candidate budget
    in *global* distance order, discards the 98% belonging to other workspaces,
    and hands back whatever is left - reliably fewer than five passages, and
    often none at all.
    """
    corpus = await seed_corpus(db_session)
    chunks = DocumentChunkRepository(db_session, tenant_id=corpus.small.id)

    async with _only_the_ann_index(db_session):
        found = await chunks.search(embedding=corpus.query, limit=TOP_K)
        # Inside the block, because outside it the exact path is back and the
        # plan would say so.
        took_the_index = await _plan_uses_the_ann_index(db_session, corpus, corpus.small.id)

    assert took_the_index, "the plan under test was not the approximate one"
    assert len(found) == TOP_K


async def test_the_approximate_scan_stays_inside_the_workspace(db_session: AsyncSession) -> None:
    """Indexing is not authorization.

    One index spans every workspace's vectors, so the question this answers is
    whether the *approximate* path is filtered as thoroughly as the exact one.
    The neighbour holds a chunk deliberately closer to the query than anything
    the small workspace owns, so an unfiltered scan would return it first.
    """
    corpus = await seed_corpus(db_session)
    chunks = DocumentChunkRepository(db_session, tenant_id=corpus.small.id)

    async with _only_the_ann_index(db_session):
        found = await chunks.search(embedding=corpus.query, limit=TOP_K)

    assert found
    assert {scored.chunk.tenant_id for scored in found} == {corpus.small.id}
    assert corpus.planted_id not in {scored.chunk.id for scored in found}


async def test_the_approximate_scan_skips_a_document_that_is_not_ready(
    db_session: AsyncSession,
) -> None:
    """The READY join is a post-filter on the approximate path too.

    A failed document's chunks keep their embeddings, so they are in the index
    and the scan will visit them. Only the join excludes them, and that join
    runs after the index has spoken.
    """
    corpus = await seed_corpus(db_session)
    chunks = DocumentChunkRepository(db_session, tenant_id=corpus.small.id)

    async with _only_the_ann_index(db_session):
        found = await chunks.search(embedding=corpus.query, limit=TOP_K)

    assert found
    assert corpus.unready_chunk_ids.isdisjoint({scored.chunk.id for scored in found})


async def test_a_chunk_written_after_the_index_exists_is_retrievable(
    db_session: AsyncSession,
) -> None:
    """HNSW takes inserts; this proves the write path still reaches the index."""
    corpus = await seed_corpus(db_session)
    chunks = DocumentChunkRepository(db_session, tenant_id=corpus.small.id)
    fresh = clustered_vector(corpus.query, spread=0.0)
    db_session.add(
        DocumentChunk(
            tenant_id=corpus.small.id,
            document_id=corpus.small_document_id,
            knowledge_base_id=corpus.small_base_id,
            ordinal=9_000,
            content="Written after the index existed.",
            token_estimate=8,
            embedding=fresh,
        )
    )
    await db_session.flush()

    async with _only_the_ann_index(db_session):
        found = await chunks.search(embedding=corpus.query, limit=TOP_K)

    assert found
    assert found[0].chunk.ordinal == 9_000
    assert found[0].distance == pytest.approx(0.0, abs=1e-6)


async def test_the_approximate_answer_is_as_close_as_the_exact_one(
    db_session: AsyncSession,
) -> None:
    """Recall, measured - and measured as the thing retrieval actually needs.

    HNSW is approximate and may return the sixth-nearest passage instead of the
    fifth. Counting matching ids calls that a miss, which on this corpus is
    misleading: the workspace's 980 chunks sit in eight tight clusters, so the
    fifth and sixth nearest are near-tied and which one an approximate scan
    reaches is close to arbitrary. Measured here, id overlap is 0.80 while the
    *worst passage returned* is within 0.36% of the distance of the exact
    answer's worst. Nothing a customer reads changes at four decimal places.

    So the primary assertion is on distance, which is stable and is what
    "relevant" means, and the id-overlap floor is kept as a coarse second
    signal. At the scale where the planner actually chooses this path - 45,000
    chunks in one workspace - the local drill in `docs/RAG.md` measures id
    overlap of 1.000 over 20 queries.
    """
    corpus = await seed_corpus(db_session)
    chunks = DocumentChunkRepository(db_session, tenant_id=corpus.large.id)

    hits = truth = 0
    for query in corpus.recall_queries:
        exact = await chunks.search(embedding=query, limit=TOP_K)
        async with _only_the_ann_index(db_session):
            approximate = await chunks.search(embedding=query, limit=TOP_K)

        assert len(approximate) == len(exact) == TOP_K
        furthest_exact = max(scored.distance for scored in exact)
        furthest_approximate = max(scored.distance for scored in approximate)
        # 2% of the exact distance. Ten times the worst gap measured, so a real
        # regression in recall trips it and tie-shuffling does not.
        assert furthest_approximate <= furthest_exact * 1.02

        hits += len({scored.chunk.id for scored in exact} & {s.chunk.id for s in approximate})
        truth += len(exact)

    assert truth
    assert hits / truth >= 0.75


# ---------------------------------------------------------------- machinery


class _only_the_ann_index:  # noqa: N801 - a context manager, used as one
    """Leave the planner no way to answer except the approximate index.

    The B-tree indexes on `document_chunks` are dropped and sequential scans
    refused, inside the test's own transaction, so the rollback at teardown
    puts them back. Diagnostic, and deliberately not evidence of what the
    planner prefers: at this corpus size it prefers the exact scan and is right
    to. What these tests need is the approximate path *exercised*, because its
    two failure modes are invisible on any plan the planner would pick here.
    """

    _DROPS = (
        "DROP INDEX ix_document_chunks_tenant_id",
        "DROP INDEX ix_document_chunks_tenant_id_knowledge_base_id",
        "DROP INDEX ix_document_chunks_document_id",
        "ALTER TABLE document_chunks"
        " DROP CONSTRAINT uq_document_chunks_tenant_id_document_id_ordinal",
        "ALTER TABLE document_chunks DROP CONSTRAINT pk_document_chunks CASCADE",
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> _only_the_ann_index:
        await self._session.flush()
        self._savepoint = await self._session.begin_nested()
        for statement in self._DROPS:
            await self._session.execute(text(statement))
        await self._session.execute(text("SET LOCAL enable_seqscan = off"))
        return self

    async def __aexit__(self, *_: object) -> bool:
        await self._savepoint.rollback()
        return False


async def _plan_uses_the_ann_index(
    session: AsyncSession, corpus: Corpus, tenant_id: uuid.UUID
) -> bool:
    """Whether the plan for the retrieval query names the ANN index.

    Runs the query `search` builds, through the same repository, so the shape
    under EXPLAIN is the shape that ships rather than one written out again here
    and free to drift from it.
    """
    plan = "\n".join(
        row[0]
        for row in await session.execute(
            text(
                "EXPLAIN SELECT document_chunks.id"
                " FROM document_chunks"
                " JOIN documents ON documents.id = document_chunks.document_id"
                " WHERE document_chunks.tenant_id = :tenant"
                "   AND documents.tenant_id = :tenant"
                "   AND documents.status = :ready"
                "   AND document_chunks.embedding IS NOT NULL"
                " ORDER BY document_chunks.embedding <=> CAST(:query AS vector)"
                " LIMIT :k"
            ),
            {
                "tenant": tenant_id,
                "ready": DocumentStatus.READY.value,
                "k": TOP_K,
                "query": _literal(corpus.query),
            },
        )
    )
    return ANN_INDEX in plan


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"
