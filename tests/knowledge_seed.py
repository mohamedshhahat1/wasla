"""Seeding documents that are already indexed, without running the pipeline.

For tests about retrieval and the vector index, which need rows in the shape a
publish leaves behind - a document, its generation, chunks pointing at that
generation - and do not want to pay for chunking and embedding to get them.
The shape is the published one exactly: an `active` generation carries the
embedding space its vectors were made in, because retrieval filters on it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import OPENAI_PROVIDER, EmbeddingSpace
from app.db.models.knowledge import (
    EMBEDDING_DIMENSIONS,
    Document,
    DocumentIndexGeneration,
    DocumentStatus,
    GenerationState,
)
from tests.fake_embeddings import FAKE_MODEL

#: The space `FakeEmbeddings` embeds in, and seeded generations are made in.
FAKE_SPACE = EmbeddingSpace(
    provider=OPENAI_PROVIDER, model=FAKE_MODEL, dimensions=EMBEDDING_DIMENSIONS
)


def generation_for(
    document: Document,
    *,
    state: GenerationState | None = None,
    number: int = 1,
    space: EmbeddingSpace = FAKE_SPACE,
) -> DocumentIndexGeneration:
    """A generation for `document`, in the state its status implies unless told."""
    if state is None:
        state = (
            GenerationState.ACTIVE
            if document.status is DocumentStatus.READY
            else (
                GenerationState.FAILED
                if document.status is DocumentStatus.FAILED
                else GenerationState.PENDING
            )
        )
    published = state in (GenerationState.ACTIVE, GenerationState.SUPERSEDED)
    return DocumentIndexGeneration(
        id=uuid.uuid4(),
        tenant_id=document.tenant_id,
        document_id=document.id,
        number=number,
        state=state,
        trigger="submitted",
        attempts=1 if state is not GenerationState.PENDING else 0,
        chunk_count=0,
        published_at=datetime.now(UTC) if published else None,
        embedding_provider=space.provider if published else None,
        embedding_model=space.model if published else None,
        embedding_dimensions=space.dimensions if published else None,
        embedding_schema_version=space.schema_version if published else None,
    )


async def add_generation(
    session: AsyncSession,
    document: Document,
    **kwargs: object,
) -> DocumentIndexGeneration:
    generation = generation_for(document, **kwargs)  # type: ignore[arg-type]
    session.add(generation)
    await session.flush()
    return generation
