"""A repeated question costs no embedding call (RQ-26, FX-4.6, IMP-19).

Every search embeds its query. The same question asked again - a FAQ, a
customer retrying, an agent searching twice in a turn - embeds the same text
into the same vector, so the vector is kept in Redis for a day:

    wasla:rag:qemb:{tenant}:{space}:{sha256(folded query)}

**Vectors only, never passages.** What a search returns is decided against the
database every time, so a document deleted, replaced or flipped to another
space a second ago is never served from here: there is nothing here to serve
it from. That is why this needs no invalidation, and why the result cache the
review also sketched is not built (DEFERRED - LATER PRODUCT).

**Nothing is shared** (G3). The workspace is in the key, so two workspaces
asking the same words hold two entries and each pays for its own first
embedding; the space is in the key, so a workspace flipped to another model
never reads a vector of the old one. The query itself is not in Redis - only
its hash - and an entry expires on its own; a workspace's entries end with the
TTL long before its retention period lets it be purged.

A Redis failure is a miss: the provider is asked, the search goes on, and the
error is counted (`wasla_rag_query_embedding_cache_total{outcome="error"}`).
"""

from __future__ import annotations

import base64
import hashlib
import struct
import uuid
from collections.abc import Sequence
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.embedding_space import EmbeddingSpace
from app.core.logging import get_logger
from app.core.telemetry import record_query_cache

logger = get_logger(__name__)

KEY_PREFIX: Final = "wasla:rag:qemb"
# Eight bytes a dimension, base64 because the deployment's Redis client decodes
# replies as text: the vector comes back exactly as it went in, so a hit ranks
# exactly as the miss that filled it did.
_FORMAT: Final = "<{}d"


def cache_key(tenant_id: uuid.UUID, space: EmbeddingSpace, query: str) -> str:
    """The entry for one workspace, one space, one folded query."""
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    label = f"{space.provider}:{space.model}:{space.dimensions}:v{space.schema_version}"
    return f"{KEY_PREFIX}:{tenant_id}:{label}:{digest}"


def _pack(vector: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(_FORMAT.format(len(vector)), *vector)).decode("ascii")


def _unpack(value: object, dimensions: int) -> list[float] | None:
    if not isinstance(value, str | bytes):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != dimensions * 8:
        return None
    return list(struct.unpack(_FORMAT.format(dimensions), raw))


class QueryEmbeddingCache:
    """Read-through cache of query vectors, per workspace and space."""

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def get_many(
        self, tenant_id: uuid.UUID, space: EmbeddingSpace, queries: Sequence[str]
    ) -> list[list[float] | None]:
        """One vector or None per query, in order. A Redis failure is all misses."""
        keys = [cache_key(tenant_id, space, query) for query in queries]
        try:
            raw = await self._redis.mget(keys)
        except (RedisError, OSError) as error:
            logger.warning("knowledge.query_cache_failed", extra={"reason": type(error).__name__})
            await record_query_cache("error")
            return [None] * len(queries)
        found: list[list[float] | None] = []
        for value in raw:
            vector = _unpack(value, space.dimensions)
            found.append(vector)
            await record_query_cache("hit" if vector is not None else "miss")
        return found

    async def put_many(
        self,
        tenant_id: uuid.UUID,
        space: EmbeddingSpace,
        entries: Sequence[tuple[str, Sequence[float]]],
    ) -> None:
        """Keep vectors the provider just returned. A failure loses nothing but the saving."""
        if not entries:
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for query, vector in entries:
                    pipe.set(cache_key(tenant_id, space, query), _pack(vector), ex=self._ttl)
                await pipe.execute()
        except (RedisError, OSError) as error:
            logger.warning("knowledge.query_cache_failed", extra={"reason": type(error).__name__})
            await record_query_cache("error")


__all__ = ["KEY_PREFIX", "QueryEmbeddingCache", "cache_key"]
