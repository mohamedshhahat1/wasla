"""A repeated question costs no embedding call, and no cache crosses a workspace (RQ-26, P-R30).

Real Redis (the test instance, decoding replies as text as the deployment's
client does), real PostgreSQL, the real `RetrievalService` and repository;
embeddings faked at the transport and counted. P-R30 at the base head: the same
question asked 20 times made 20 embedding requests.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.core.embedding_space import OPENAI_PROVIDER, EmbeddingSpace
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.db.models.usage import UsageEvent
from app.integrations.openai.embeddings import EmbeddingsClient
from app.services.embedding_switch import EmbeddingSwitch
from app.services.knowledge_service import KnowledgeService
from app.services.query_embedding_cache import KEY_PREFIX, QueryEmbeddingCache, cache_key
from app.services.retrieval_service import RetrievalService
from app.services.text_normalisation import for_matching
from tests.fake_embeddings import FAKE_MODEL
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("WASLA_TEST_REDIS_URL", "redis://localhost:6379/15")
LARGE = "text-embedding-3-large"
SMALL_SPACE = EmbeddingSpace(
    provider=OPENAI_PROVIDER, model=FAKE_MODEL, dimensions=EMBEDDING_DIMENSIONS
)
LARGE_SPACE = EmbeddingSpace(provider=OPENAI_PROVIDER, model=LARGE, dimensions=EMBEDDING_DIMENSIONS)
DELIVERY = "Delivery\n\nDelivery to Cairo takes one day. Model BK-35 ships flat-packed."


@pytest.fixture
async def redis() -> Any:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    async for key in client.scan_iter(match=f"{KEY_PREFIX}:*"):
        await client.delete(key)
    await client.aclose()


def _client(indexing: Indexing) -> EmbeddingsClient:
    http = httpx.AsyncClient(transport=indexing.provider.transport())
    return EmbeddingsClient(
        http=http,
        api_key="sk-test-not-real",
        model=FAKE_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        next_space=EmbeddingsClient(
            http=http, api_key="sk-test-not-real", model=LARGE, dimensions=EMBEDDING_DIMENSIONS
        ),
    )


async def _search(
    indexing: Indexing, tenant_id: uuid.UUID, cache: QueryEmbeddingCache | None, query: str
) -> list[tuple[uuid.UUID | None, str]]:
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=_client(indexing), cache=cache
        ).search(query=query, top_k=4)
    return [(p.document_id, p.content) for p in found.passages]


async def _metered(indexing: Indexing, tenant_id: uuid.UUID) -> tuple[int, int]:
    """(embedding requests, knowledge searches) metered for a workspace so far."""
    async with indexing.database.session() as session:
        counts = []
        for kind in ("embedding_request", "rag_query"):
            counts.append(
                int(
                    await session.scalar(
                        select(func.count(UsageEvent.id)).where(
                            UsageEvent.tenant_id == tenant_id, UsageEvent.event_type == kind
                        )
                    )
                    or 0
                )
            )
    return counts[0], counts[1]


async def _workspace(indexing: Indexing, body: str = DELIVERY) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, body)
    await indexing.drain()
    indexing.provider.requests.clear()
    return tenant_id, document_id


async def test_the_same_question_twenty_times_is_embedded_once(
    indexing: Indexing, redis: Redis
) -> None:
    tenant_id, _ = await _workspace(indexing)
    cache = QueryEmbeddingCache(redis, ttl_seconds=86_400)
    before = await _metered(indexing, tenant_id)
    results = [
        await _search(indexing, tenant_id, cache, "how long is delivery to Cairo?")
        for _ in range(20)
    ]
    assert len(indexing.provider.requests) == 1
    assert all(result == results[0] for result in results) and results[0]
    after = await _metered(indexing, tenant_id)
    # The provider was paid once and that call is on the meter; every search is.
    assert (after[0] - before[0], after[1] - before[1]) == (1, 20)
    ttl = await redis.ttl(
        cache_key(tenant_id, SMALL_SPACE, for_matching("how long is delivery to Cairo?"))
    )
    assert 0 < ttl <= 86_400


async def test_two_workspaces_asking_the_same_words_share_nothing(
    indexing: Indexing, redis: Redis
) -> None:
    first, _ = await _workspace(indexing)
    second, _ = await _workspace(indexing)
    cache = QueryEmbeddingCache(redis, ttl_seconds=3_600)
    await _search(indexing, first, cache, "delivery to Cairo")
    await _search(indexing, second, cache, "delivery to Cairo")
    assert len(indexing.provider.requests) == 2
    keys = [key async for key in redis.scan_iter(match=f"{KEY_PREFIX}:*")]
    assert len(keys) == 2
    assert {key.split(":")[3] for key in keys} == {str(first), str(second)}
    # The query is not in Redis, only its hash.
    assert not any("Cairo" in key or "cairo" in key for key in keys)


async def test_a_deleted_or_replaced_document_is_never_served_from_the_cache(
    indexing: Indexing, redis: Redis
) -> None:
    tenant_id, document_id = await _workspace(indexing)
    cache = QueryEmbeddingCache(redis, ttl_seconds=3_600)
    query = "when does model BK-35 ship"
    assert any(d == document_id for d, _ in await _search(indexing, tenant_id, cache, query))
    async with indexing.database.session() as session:
        await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=indexing.queue()
        ).replace_content(document_id, raw=DELIVERY.replace("one day", "two days"))
    await indexing.drain()
    indexing.provider.requests.clear()
    served = await _search(indexing, tenant_id, cache, query)
    assert served and all("two days" in text and "one day" not in text for _, text in served)
    async with indexing.database.session() as session:
        await KnowledgeService(session=session, tenant_id=tenant_id).delete_document(document_id)
    assert await _search(indexing, tenant_id, cache, query) == []
    # Both searches after the replacement were hits: the cache holds the
    # question's vector, and what it finds is decided by the database.
    assert indexing.provider.requests == []


async def test_a_hit_ranks_exactly_as_the_miss_that_filled_it(
    indexing: Indexing, redis: Redis
) -> None:
    """Hybrid and fused results unchanged by the cache: the vector round-trips bit for bit."""
    tenant_id = await indexing.workspace()
    for n in range(6):
        await indexing.submit(
            tenant_id, f"Item {n}\n\nModel BK-{30 + n} costs {4000 + n * 100} EGP."
        )
    await indexing.drain()
    cache = QueryEmbeddingCache(redis, ttl_seconds=3_600)
    without = await _search(indexing, tenant_id, None, "price of model BK-33")
    miss = await _search(indexing, tenant_id, cache, "price of model BK-33")
    hit = await _search(indexing, tenant_id, cache, "price of model BK-33")
    assert without == miss == hit
    assert "BK-33" in hit[0][1]


async def test_a_flipped_workspace_never_reads_a_vector_of_its_old_space(
    indexing: Indexing, redis: Redis
) -> None:
    """M-R39: the space is in the key - a flip is a miss, embedded in the new model."""
    indexing.configure(openai_embedding_model_next=LARGE)
    tenant_id, _ = await _workspace(indexing)
    cache = QueryEmbeddingCache(redis, ttl_seconds=3_600)
    await _search(indexing, tenant_id, cache, "delivery to Cairo")
    async with indexing.database.session() as session:
        switch = EmbeddingSwitch(
            session, tenant_id=tenant_id, default_space=SMALL_SPACE, queue=indexing.queue()
        )
        await switch.start(LARGE_SPACE)
        await switch.enqueue(await switch.request_shadows(limit=10))
    await indexing.drain()
    async with indexing.database.session() as session:
        await EmbeddingSwitch(session, tenant_id=tenant_id, default_space=SMALL_SPACE).flip()
    indexing.provider.requests.clear()
    assert await _search(indexing, tenant_id, cache, "delivery to Cairo")
    assert [b["model"] for b in indexing.provider.requests] == [LARGE]
    assert await redis.exists(cache_key(tenant_id, LARGE_SPACE, for_matching("delivery to Cairo")))


async def test_redis_unavailable_is_a_miss_and_the_search_goes_on(indexing: Indexing) -> None:
    tenant_id, _ = await _workspace(indexing)
    broken = Redis.from_url(
        "redis://127.0.0.1:1/0", decode_responses=True, socket_connect_timeout=0.2
    )
    cache = QueryEmbeddingCache(broken, ttl_seconds=3_600)
    assert await _search(indexing, tenant_id, cache, "delivery to Cairo")
    assert len(indexing.provider.requests) == 1
    await broken.aclose()
