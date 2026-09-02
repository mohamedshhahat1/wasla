"""A corpus shaped like the problem the ANN index has to solve.

Two workspaces of very different size in one table, because that difference is
what makes an approximate index hard here. The index spans every workspace, so a
scan on behalf of the small one spends its candidate budget looking at the large
one's vectors - and how much of the corpus a workspace owns decides both how
fast the approximate path is and whether it finds anything at all.

Vectors are clustered rather than uniform. Uniform points in 1,536 dimensions
are all almost exactly the same distance apart, so recall measured against them
is a number about the geometry of high-dimensional noise and not about
retrieval. Real embeddings sit in topical clusters, so these do too.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import (
    EMBEDDING_DIMENSIONS,
    Document,
    DocumentChunk,
    DocumentSource,
    DocumentStatus,
    KnowledgeBase,
)
from app.db.models.tenant import Tenant

ANN_INDEX = "ix_document_chunks_embedding_hnsw"

# The small workspace owns 2% of the corpus. Enough that the default candidate
# budget, spent in global distance order, reliably lands on other workspaces'
# vectors - which is the failure this corpus exists to reproduce.
LARGE_CHUNKS = 980
SMALL_CHUNKS = 20
CLUSTERS = 8
RECALL_QUERIES = 8
SEED = 20260902


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return [1.0] + [0.0] * (len(vector) - 1)
    return [value / norm for value in vector]


def clustered_vector(around: list[float], *, spread: float, rng: random.Random | None = None):
    """A unit vector near `around`. `spread=0` returns `around` itself."""
    if spread == 0.0:
        return list(around)
    source = rng or random.Random(SEED)  # noqa: S311 - test fixtures, not keys
    return _unit([value + source.gauss(0.0, spread) for value in around])


@dataclass
class Corpus:
    """What a test needs to address the seeded rows."""

    large: Tenant
    small: Tenant
    query: list[float]
    small_base_id: uuid.UUID
    small_document_id: uuid.UUID
    planted_id: uuid.UUID
    unready_chunk_ids: set[uuid.UUID] = field(default_factory=set)
    recall_queries: list[list[float]] = field(default_factory=list)


async def _workspace(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _base(session: AsyncSession, *, tenant: Tenant, name: str) -> KnowledgeBase:
    base = KnowledgeBase(tenant_id=tenant.id, name=name)
    session.add(base)
    await session.flush()
    return base


async def _document(
    session: AsyncSession,
    *,
    tenant: Tenant,
    base: KnowledgeBase,
    title: str,
    status: DocumentStatus = DocumentStatus.READY,
) -> Document:
    document = Document(
        tenant_id=tenant.id,
        knowledge_base_id=base.id,
        title=title,
        source=DocumentSource.TEXT,
        status=status,
        content_hash=uuid.uuid4().hex,
        byte_size=0,
        chunk_count=0,
    )
    session.add(document)
    await session.flush()
    return document


async def seed_corpus(session: AsyncSession) -> Corpus:
    """Write both workspaces and return the handles a test needs.

    One document in the small workspace is `FAILED`, so the READY join has
    something to exclude on the approximate path as well as the exact one, and
    the neighbouring workspace holds a chunk placed exactly on the query so an
    unfiltered scan would return it first.
    """
    rng = random.Random(SEED)  # noqa: S311 - test fixtures, not keys
    centroids = [
        _unit([rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIMENSIONS)]) for _ in range(CLUSTERS)
    ]
    # The question. Near a centroid rather than on it, which is where a real
    # question lands relative to the passages that answer it.
    query = clustered_vector(centroids[0], spread=0.2, rng=rng)

    large = await _workspace(session, slug="vector-large")
    small = await _workspace(session, slug="vector-small")

    large_base = await _base(session, tenant=large, name="Catalogue")
    large_document = await _document(session, tenant=large, base=large_base, title="Catalogue")
    rows: list[DocumentChunk] = []
    for ordinal in range(LARGE_CHUNKS):
        rows.append(
            DocumentChunk(
                tenant_id=large.id,
                document_id=large_document.id,
                knowledge_base_id=large_base.id,
                ordinal=ordinal,
                content=f"Neighbour passage {ordinal}.",
                token_estimate=8,
                embedding=clustered_vector(centroids[ordinal % CLUSTERS], spread=0.3, rng=rng),
            )
        )
    # Placed on the query itself. Any answer containing this row is an answer
    # that crossed a workspace boundary.
    planted = DocumentChunk(
        tenant_id=large.id,
        document_id=large_document.id,
        knowledge_base_id=large_base.id,
        ordinal=LARGE_CHUNKS,
        content="The neighbour's closest passage.",
        token_estimate=8,
        embedding=list(query),
    )
    rows.append(planted)

    small_base = await _base(session, tenant=small, name="Handbook")
    small_document = await _document(session, tenant=small, base=small_base, title="Handbook")
    for ordinal in range(SMALL_CHUNKS):
        rows.append(
            DocumentChunk(
                tenant_id=small.id,
                document_id=small_document.id,
                knowledge_base_id=small_base.id,
                ordinal=ordinal,
                content=f"Handbook passage {ordinal}.",
                token_estimate=8,
                embedding=clustered_vector(centroids[0], spread=0.25, rng=rng),
            )
        )

    unready = await _document(
        session,
        tenant=small,
        base=small_base,
        title="Half-ingested",
        status=DocumentStatus.FAILED,
    )
    unready_ids = set()
    for ordinal in range(4):
        # On the query, so only the READY join can keep them out of the answer.
        chunk = DocumentChunk(
            tenant_id=small.id,
            document_id=unready.id,
            knowledge_base_id=small_base.id,
            ordinal=ordinal,
            content=f"Unready passage {ordinal}.",
            token_estimate=8,
            embedding=list(query),
        )
        rows.append(chunk)
        unready_ids.add(chunk.id)

    session.add_all(rows)
    await session.flush()
    # `id` is a client-side default, so the planted rows carry their ids before
    # the flush; the set above is built from them and is complete here.
    unready_ids = {chunk.id for chunk in rows if chunk.document_id == unready.id}

    return Corpus(
        large=large,
        small=small,
        query=query,
        small_base_id=small_base.id,
        small_document_id=small_document.id,
        planted_id=planted.id,
        unready_chunk_ids=unready_ids,
        recall_queries=[
            clustered_vector(centroids[index % CLUSTERS], spread=0.2, rng=rng)
            for index in range(RECALL_QUERIES)
        ],
    )
