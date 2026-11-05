"""Which embedding client embeds for which space (RQ-25, FX-4.1, RV-11).

A deployment embeds in one space - the configured model - except while an
embedding-model switch is under way, when it embeds in two: the current one,
which every workspace is still served from, and the target one, which shadow
generations are built in and each workspace is flipped to once its coverage is
complete. `SpaceClients` holds both and answers "who embeds this space?" for the
worker claiming a generation and for a search embedding a workspace's query.

A space neither client covers is a configuration the operator has to fix - a
workspace flipped to a model the deployment no longer names - and the answer is
"nobody": the claim waits, the search fails visibly, and nothing is embedded in
the wrong space.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.embedding_space import EmbeddingSpace
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.integrations.openai.embeddings import EMBED_INGEST, EmbeddingsClient


@dataclass(frozen=True, slots=True)
class SpaceClients:
    """The current embedding client and, during a switch, the target one."""

    current: EmbeddingsClient
    next: EmbeddingsClient | None = None

    @classmethod
    def of(cls, embeddings: EmbeddingsClient | SpaceClients) -> SpaceClients:
        """The clients a caller holds: a pair, or one client and the target it carries."""
        if isinstance(embeddings, SpaceClients):
            return embeddings
        return cls(current=embeddings, next=getattr(embeddings, "next_space", None))

    @property
    def default_space(self) -> EmbeddingSpace:
        """The space a workspace with no switch history is queried in."""
        return self.current.space

    def for_space(self, space: EmbeddingSpace) -> EmbeddingsClient | None:
        """The client whose vectors belong to `space`, or None."""
        if self.current.space == space:
            return self.current
        if self.next is not None and self.next.space == space:
            return self.next
        return None


def next_space_client(
    *, http: httpx.AsyncClient, settings: Settings, operation: str = EMBED_INGEST
) -> EmbeddingsClient | None:
    """The target space's client while `OPENAI_EMBEDDING_MODEL_NEXT` names another model."""
    model = settings.openai_embedding_model_next
    if not model or model == settings.openai_embedding_model:
        return None
    return EmbeddingsClient(
        http=http,
        api_key=settings.openai_api_key or "",
        model=model,
        dimensions=EMBEDDING_DIMENSIONS,
        operation=operation,
    )


__all__ = ["SpaceClients", "next_space_client"]
