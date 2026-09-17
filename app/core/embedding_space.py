"""Which vector space an embedding belongs to.

Two vectors of the same width are not comparable merely because they fit the
same column. `text-embedding-3-small` and `text-embedding-3-large` truncated to
1,536 dimensions both fit `vector(1536)`, and a cosine distance between one of
each is a number that means nothing - retrieval would rank passages by noise and
report it as relevance (RAG-06).

So every indexed generation records the space its vectors were made in, and a
search only compares a query against generations made in the space the query was
embedded in. A leaf module, because the database model, the repository, the
client and the worker all need the same value and none of them should import
another to get it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# The version of the contract between an indexed passage and a query vector:
# what text is embedded and how, and what a stored vector is expected to look
# like. Bumped when a change makes vectors produced before it incomparable with
# queries embedded after it - a normalisation of the embedded text, a prefix,
# a vector post-processing step. **Not** bumped for a chunking change: a passage
# cut differently is still embedded the same way, and hiding every document in
# every workspace until it is re-indexed would be an outage bought for nothing.
EMBEDDING_SCHEMA_VERSION: Final = 1

OPENAI_PROVIDER: Final = "openai"


@dataclass(frozen=True, slots=True)
class EmbeddingSpace:
    """The identity of a vector space. Equal spaces produce comparable vectors."""

    provider: str
    model: str
    dimensions: int
    schema_version: int = EMBEDDING_SCHEMA_VERSION

    def describe(self) -> str:
        """A short label for an operator, never a metric label."""
        return f"{self.provider}/{self.model}/{self.dimensions}d/v{self.schema_version}"


__all__ = ["EMBEDDING_SCHEMA_VERSION", "OPENAI_PROVIDER", "EmbeddingSpace"]
