"""A deterministic stand-in for the embedding provider.

What is faked and what is not, because the distinction is the whole value of
these tests: the *embedding model* is faked, the *vector search* is not. Chunks
are written into a real `vector` column and retrieved by real pgvector cosine
distance ordering. Only the mapping from text to vector is local, because
reaching OpenAI from a test suite is not something to build on.

The mapping is a hashed bag of words. Every token is hashed to a coordinate and
accumulated, then the vector is normalised. Two passages sharing vocabulary
therefore land close together and two about unrelated subjects land far apart,
which is the only property retrieval tests actually depend on. It is not a
semantic model and does not pretend to be: it will not match "cost" to "price",
so tests phrase queries with words the documents use.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any

from app.core.embedding_space import OPENAI_PROVIDER, EmbeddingSpace
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.integrations.openai.embeddings import EmbeddingBatch

#: The model the fakes claim to be, which is the deployment default.
FAKE_MODEL = "text-embedding-3-small"

_TOKEN = re.compile(r"\w+", re.UNICODE)


def _coordinate(token: str) -> int:
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS


def embed_text(text: str, *, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """A unit-length vector derived from the words in `text`."""
    vector = [0.0] * dimensions
    for token in _TOKEN.findall(text.lower()):
        vector[_coordinate(token) % dimensions] += 1.0

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        # An empty or punctuation-only text still needs a valid vector; a zero
        # vector has undefined cosine distance and pgvector would return NaN.
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


class FakeEmbeddings:
    """Duck-types `EmbeddingsClient` for tests.

    Records what it was asked to embed, so a test can assert that ingestion
    embedded the chunks it produced and that retrieval embedded the question.
    `calls` counts provider requests - one per batch - which is the unit a
    cost assertion is about.
    """

    def __init__(self, *, dimensions: int = EMBEDDING_DIMENSIONS, model: str = FAKE_MODEL) -> None:
        self.dimensions = dimensions
        self.model = model
        self.embedded: list[str] = []
        self.calls = 0

    @property
    def space(self) -> EmbeddingSpace:
        return EmbeddingSpace(
            provider=OPENAI_PROVIDER, model=self.model, dimensions=self.dimensions
        )

    async def embed_batch(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.calls += 1
        self.embedded.extend(texts)
        return EmbeddingBatch(
            vectors=[embed_text(text, dimensions=self.dimensions) for text in texts],
            input_tokens=sum(len(text.split()) for text in texts),
            characters=sum(len(text) for text in texts),
        )

    async def embed(self, texts: Sequence[str]) -> list[Any]:
        return (await self.embed_batch(texts)).vectors

    async def embed_one(self, text: str) -> list[float]:
        vector: list[float] = (await self.embed_batch([text])).vectors[0]
        return vector


class BrokenEmbeddings:
    """Fails the way a provider outage or rejection does, for the failure paths."""

    def __init__(self, error: Exception, *, model: str = FAKE_MODEL) -> None:
        self.error = error
        self.dimensions = EMBEDDING_DIMENSIONS
        self.model = model
        self.calls = 0

    @property
    def space(self) -> EmbeddingSpace:
        return EmbeddingSpace(
            provider=OPENAI_PROVIDER, model=self.model, dimensions=self.dimensions
        )

    async def embed_batch(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.calls += 1
        raise self.error

    async def embed(self, texts: Sequence[str]) -> None:
        await self.embed_batch(texts)

    async def embed_one(self, text: str) -> None:
        await self.embed_batch([text])
