"""Tenant-scoped knowledge retrieval.

The read half of RAG. A question becomes an embedding, the embedding finds the
nearest chunks belonging to this workspace, and those chunks become text an
agent can quote.

Two rules matter more than anything else here:

- **Nothing crosses a tenant boundary.** The repository applies the filter; this
  service never constructs a query, and the tenant id comes from the caller's
  authenticated context rather than from anything a model produced.
- **An empty result stays empty.** When nothing relevant is found, the tool says
  so plainly. It must not return an encouraging blank that a model reads as
  permission to answer from memory - the entire point of grounding is that the
  agent knows the difference between what it retrieved and what it invented.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.usage import UsageEventType
from app.integrations.openai.embeddings import EmbeddingsClient
from app.repositories.knowledge_repository import DocumentChunkRepository, ScoredChunk
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

DEFAULT_TOP_K: Final = 4
MAX_TOP_K: Final = 10
# Cosine distance, so smaller is closer. Embeddings of unrelated text sit around
# 0.8 and above with OpenAI's models; this keeps plainly irrelevant passages out
# rather than handing an agent the least-bad match in an empty knowledge base.
MAX_DISTANCE: Final = 0.75
# Retrieved text is pasted into a prompt. Without a ceiling one large document
# could crowd out the conversation the agent is supposed to be answering.
MAX_CONTEXT_CHARACTERS: Final = 6_000


@dataclass(frozen=True, slots=True)
class Passage:
    """One retrieved passage, with enough context to be quoted or cited."""

    document_title: str
    content: str
    distance: float


@dataclass(frozen=True, slots=True)
class Retrieval:
    """What one search found.

    `is_empty` exists so callers stop asking themselves what an empty list
    means. It means the workspace has nothing on this subject, and the agent
    must say so.
    """

    passages: tuple[Passage, ...]
    query: str

    @property
    def is_empty(self) -> bool:
        return not self.passages

    def as_context(self) -> str:
        """The passages as text for a model, or an explicit statement of nothing.

        The wording of the empty case is load-bearing. A model handed an empty
        string will fill the silence from its own training; a model told plainly
        that the knowledge base has nothing on the subject will say so.
        """
        if self.is_empty:
            return (
                "No information about this was found in the company's knowledge base. "
                "Tell the customer you do not have that information rather than guessing, "
                "and offer to pass the question to a colleague."
            )

        blocks: list[str] = []
        budget = MAX_CONTEXT_CHARACTERS
        for index, passage in enumerate(self.passages, start=1):
            block = f"[{index}] From “{passage.document_title}”:\n{passage.content}"
            if len(block) > budget:
                break
            blocks.append(block)
            budget -= len(block)
        return "\n\n".join(blocks)


class RetrievalService:
    """Answers questions from one workspace's own documents."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        embeddings: EmbeddingsClient,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._embeddings = embeddings
        self._chunks = DocumentChunkRepository(session, tenant_id=tenant_id)
        self._usage = UsageRecorder(session, tenant_id=tenant_id)

    async def search(
        self,
        *,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        knowledge_base_id: uuid.UUID | None = None,
        max_distance: float = MAX_DISTANCE,
    ) -> Retrieval:
        """Find the passages in this workspace most relevant to a question."""
        cleaned = query.strip()
        if not cleaned:
            return Retrieval(passages=(), query=query)

        limit = max(1, min(top_k, MAX_TOP_K))
        vector = await self._embeddings.embed_one(cleaned)
        # Counted once the embedding call has been paid for, and regardless of
        # whether anything was found: a search that returns nothing consumed the
        # same provider call as one that returns four passages.
        self._usage.record(UsageEventType.RAG_QUERY)
        scored = await self._chunks.search(
            embedding=vector,
            limit=limit,
            knowledge_base_id=knowledge_base_id,
        )
        passages = tuple(_passage(row) for row in scored if row.distance <= max_distance)

        logger.info(
            "knowledge.searched",
            extra={
                "tenant_id": str(self._tenant_id),
                # The question itself is a customer's words and is not logged.
                "candidates": len(scored),
                "kept": len(passages),
            },
        )
        return Retrieval(passages=passages, query=cleaned)


def _passage(row: ScoredChunk) -> Passage:
    return Passage(
        document_title=row.document_title,
        content=row.chunk.content,
        distance=row.distance,
    )
