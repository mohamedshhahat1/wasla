"""What an operator can see and do about knowledge indexing - and what never leaks.

* **Secret hygiene** (M15). A provider that quotes the API key back in its error
  body must not put it anywhere this system writes: logs, the document, the
  generation, the audit trail, Redis.
* **Signals** (RAG-05). The gauges the scrape publishes equal the database's
  truth, measured against states these tests put there; the ingestion and
  retrieval counters record what happened.
* **Operator commands** (RAG-01, RAG-06). An outstanding attempt, a failed one
  and a stale embedding space are visible without SQL, without document text, and
  the stale-space re-index needs no SQL either.
* **Invariants** (§73). The database sweep over everything the knowledge tables
  hold after real indexing - with presence checks, so an empty database cannot
  pass it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable
from typing import Any, cast

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select, text

from app.core.logging import JsonFormatter
from app.core.redis import RedisClient
from app.core.telemetry import read_redis_counters, set_counter_sink
from app.db.models.audit import AuditLog
from app.db.models.knowledge import GenerationState
from app.services.document_indexing import committed_units, run_indexing
from app.services.knowledge_service import KnowledgeService
from app.services.metrics_service import MetricsService
from app.services.retrieval_service import KnowledgeSearchUnavailableError, RetrievalService
from app.workers.ingestion_queue import INGESTION_NAMESPACE
from app.workers.queues import (
    failed_documents,
    reindex_stale_embeddings,
    stale_embeddings,
    unindexed_documents,
)
from tests.fake_embeddings import BrokenEmbeddings, FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.rag_harness import LARGE, SMALL, Indexing, long_document, respond
from tests.integration.rag_invariants import sweep

pytestmark = pytest.mark.integration

SENTINEL_KEY = "sk-RAG-OPS-SENTINEL-4c1d9e2a7b"
SECRET_BODY = "Staff discount code is WINTER-77. OPS_BODY_MARKER."


@pytest_asyncio.fixture
async def counters(indexing: Indexing) -> AsyncIterator[Redis]:
    async def clear() -> None:
        async for key in indexing.redis.scan_iter(match="metrics:*"):
            await indexing.redis.delete(key)

    await clear()
    set_counter_sink(indexing.redis)
    try:
        yield indexing.redis
    finally:
        set_counter_sink(None)
        await clear()


# ---------------------------------------------------------------- M15


async def test_a_provider_that_quotes_the_key_leaks_it_nowhere(
    indexing: Indexing, caplog: pytest.LogCaptureFixture
) -> None:
    prose = f"Incorrect API key provided: {SENTINEL_KEY}. You can find your API key at ..."

    async def quoting(_body: dict[str, Any]) -> Any:
        import httpx

        return httpx.Response(
            401,
            json={"error": {"message": prose, "type": "invalid_request_error", "code": prose}},
        )

    indexing.configure(openai_api_key=SENTINEL_KEY)
    indexing.provider.handler = quoting
    captured: list[str] = []
    original = indexing.provider.transport

    def recording_transport() -> Any:
        import httpx

        inner = original()

        async def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request.headers.get("authorization", ""))
            return await inner.handle_async_request(request)

        return httpx.MockTransport(handle)

    indexing.provider.transport = recording_transport  # type: ignore[method-assign]
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, SECRET_BODY)
    caplog.set_level(logging.DEBUG)

    await indexing.drain()

    # Presence: the key genuinely went out, and the failure genuinely happened.
    assert any(SENTINEL_KEY in header for header in captured)
    (generation,) = await indexing.generations(document_id)
    assert generation.last_error_code == "provider_unauthorized"

    rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert "knowledge.ingestion_failed" in rendered
    for leak in (SENTINEL_KEY, "OPS_BODY_MARKER", "Incorrect API key"):
        assert leak not in rendered
    document = await indexing.document(document_id)
    assert document is not None
    stored = repr((document.error, generation.error, generation.last_error_code))
    assert SENTINEL_KEY not in stored
    async with indexing.database.session() as session:
        audit = list(await session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
    assert audit and SENTINEL_KEY not in repr([(row.meta, row.target_label) for row in audit])
    async for key in indexing.redis.scan_iter(match="*"):
        kind = await cast("Awaitable[Any]", indexing.redis.type(key))
        if kind == "string":
            value = await cast("Awaitable[Any]", indexing.redis.get(key))
        elif kind == "list":
            value = await cast("Awaitable[Any]", indexing.redis.lrange(key, 0, -1))
        elif kind == "hash":
            value = await cast("Awaitable[Any]", indexing.redis.hgetall(key))
        elif kind == "zset":
            value = await cast("Awaitable[Any]", indexing.redis.zrange(key, 0, -1))
        else:
            continue
        assert SENTINEL_KEY not in repr(value), key


# ---------------------------------------------------------------- RAG-05


def _gauge(rendered: str, name: str) -> float:
    match = re.search(rf"^{name} (\S+)$", rendered, flags=re.MULTILINE)
    assert match is not None, f"{name} is not in the exposition"
    return float(match.group(1))


async def test_the_indexing_gauges_equal_the_databases_truth(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    await indexing.submit(tenant_id, "One\n\nThe first document.")
    await indexing.drain()
    indexing.provider.handler = respond(401)
    await indexing.submit(tenant_id, "Two\n\nThe second document.")
    await indexing.drain()
    indexing.provider.handler = respond(503)
    await indexing.submit(tenant_id, "Three\n\nThe third document.")
    await indexing.drain()
    indexing.configure(openai_embedding_model=LARGE.model)

    rendered = await MetricsService(None, database=indexing.database, space=LARGE).render()

    async with indexing.database.session() as session:
        truth = dict(
            (
                await session.execute(
                    text("""
                        SELECT
                          (SELECT count(*) FROM document_index_generations
                            WHERE state = 'active') AS serving,
                          (SELECT count(*) FROM document_index_generations
                            WHERE state = 'pending' AND next_retry_at IS NOT NULL) AS waiting,
                          (SELECT count(*) FROM document_index_generations
                            WHERE state = 'processing') AS processing,
                          (SELECT count(*) FROM document_index_generations
                            WHERE state = 'active' AND embedding_model <> :large) AS stale
                        """),
                    {"large": LARGE.model},
                )
            )
            .one()
            ._mapping
        )
    # Non-vacuity: this test's own rows are among what is counted.
    assert truth["serving"] >= 1 and truth["waiting"] >= 1 and truth["stale"] >= 1
    assert _gauge(rendered, "wasla_documents_serving") == truth["serving"]
    assert _gauge(rendered, "wasla_documents_retry_waiting") == truth["waiting"]
    assert _gauge(rendered, "wasla_documents_processing") == truth["processing"]
    assert _gauge(rendered, "wasla_documents_stale_embedding") == truth["stale"]
    assert _gauge(rendered, "wasla_documents_indexing_failed") >= 1
    for name in (
        "wasla_pending_documents",
        "wasla_oldest_pending_document_age_seconds",
        "wasla_oldest_processing_document_age_seconds",
        "wasla_documents_indexing_exhausted",
    ):
        _gauge(rendered, name)


async def test_ingestion_and_retrieval_outcomes_are_counted(
    indexing: Indexing, counters: Redis
) -> None:
    tenant_id = await indexing.workspace()
    await indexing.submit(tenant_id)
    await indexing.drain()
    indexing.provider.handler = respond(404)
    await indexing.submit(tenant_id, "Broken\n\nThis one fails.")
    await indexing.drain()

    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query="refund fourteen days")
        assert not found.is_empty
        empty = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query="zebra xylophone quantum")
        assert empty.is_empty
        with pytest.raises(KnowledgeSearchUnavailableError):
            await RetrievalService(
                session=session,
                tenant_id=tenant_id,
                embeddings=as_embeddings(BrokenEmbeddings(RuntimeError("boom"))),
            ).search(query="refund")

    collected = await read_redis_counters(counters)
    ingestion = {
        labels["outcome"]: value
        for labels, value in collected.get("wasla_rag_ingestion_outcomes_total", [])
    }
    retrievals = {
        labels["outcome"]: value
        for labels, value in collected.get("wasla_rag_retrievals_total", [])
    }
    attempts = {
        (labels["operation"], labels["outcome"]): value
        for labels, value in collected.get("wasla_provider_attempts_total", [])
    }
    assert ingestion == {"published": 1.0, "failed": 1.0}
    assert retrievals == {"found": 1.0, "empty": 1.0, "failed": 1.0}
    assert attempts == {("embed_ingest", "success"): 1.0, ("embed_ingest", "failure"): 1.0}


# ---------------------------------------------------------------- operator


async def test_the_operator_sees_outstanding_failed_and_stale_documents_without_their_text(
    indexing: Indexing, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant_id = await indexing.workspace()
    served = await indexing.submit(tenant_id, SECRET_BODY)
    await indexing.drain()
    indexing.provider.handler = respond(403)
    failed = await indexing.submit(tenant_id, "Second\n\nOPS_BODY_MARKER again.")
    await indexing.drain()
    outstanding = await indexing.submit(tenant_id, "Third\n\nWaiting.", enqueue=False)

    assert await unindexed_documents(indexing.database, limit=500) == 0
    assert await failed_documents(indexing.database, limit=500) == 0
    assert await stale_embeddings(indexing.database, space=LARGE, limit=500) == 0
    printed = capsys.readouterr().out

    assert str(outstanding) in printed
    assert re.search(rf"provider_forbidden\s+{tenant_id}\s+{failed}", printed)
    assert f"configured space: {LARGE.describe()}" in printed
    assert f"{served}  openai/{SMALL.model}/1536d/v1" in printed
    assert "OPS_BODY_MARKER" not in printed
    assert "WINTER-77" not in printed


async def test_the_operator_reindexes_stale_documents_without_sql(
    indexing: Indexing, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id)
    await indexing.drain()
    redis = RedisClient(indexing.settings)
    try:
        assert (
            await reindex_stale_embeddings(
                indexing.database, redis, space=LARGE, limit=500, dry_run=True
            )
            == 0
        )
        assert f"would re-index  {tenant_id}  {document_id}" in capsys.readouterr().out
        assert [g.state for g in await indexing.generations(document_id)] == [
            GenerationState.ACTIVE
        ], "a dry run changes nothing"

        await reindex_stale_embeddings(
            indexing.database, redis, space=LARGE, limit=500, dry_run=False
        )
        await reindex_stale_embeddings(
            indexing.database, redis, space=LARGE, limit=500, dry_run=False
        )
    finally:
        await redis.close()
        async for key in indexing.redis.scan_iter(match=f"{INGESTION_NAMESPACE}*"):
            await indexing.redis.delete(key)

    generations = await indexing.generations(document_id)
    assert [(g.state, g.trigger) for g in generations] == [
        (GenerationState.ACTIVE, "submitted"),
        (GenerationState.PENDING, "stale_embedding"),
    ], "run twice, coalesced into one re-index"

    indexing.configure(openai_embedding_model=LARGE.model)
    run = await run_indexing(
        committed_units(indexing.database, tenant_id=tenant_id),
        document_id=document_id,
        embeddings=indexing.client(),
    )
    assert str(run.outcome) == "published"
    assert await indexing.search(tenant_id, "refund fourteen days", space=LARGE)


# ---------------------------------------------------------------- invariants


async def test_the_knowledge_tables_hold_every_invariant_after_real_indexing(
    indexing: Indexing,
) -> None:
    """Representative state first, then the sweep - which must find it and nothing wrong."""
    tenant_a = await indexing.workspace()
    tenant_b = await indexing.workspace()
    for tenant_id in (tenant_a, tenant_b):
        ready = await indexing.submit(tenant_id, long_document(20))
        await indexing.drain()
        await indexing.reindex(tenant_id, ready)
        await indexing.drain()
    indexing.provider.handler = respond(401)
    failed = await indexing.submit(tenant_a, "Broken\n\nFails permanently.")
    await indexing.drain()
    await indexing.reindex(tenant_b, ready)
    await indexing.drain()
    indexing.provider.handler = None
    async with indexing.database.session() as session:
        await KnowledgeService(session=session, tenant_id=tenant_a).delete_document(failed)

    async with indexing.database.session() as session:
        result = await sweep(session, space=SMALL, tenants=[tenant_a, tenant_b])

    assert result.presence["documents"] >= 2
    assert result.presence["active_generations"] >= 2
    assert result.presence["superseded_generations"] >= 2
    assert result.presence["failed_generations"] >= 1
    assert result.presence["embedding_usage_rows"] >= 4
    assert result.presence["tenants"] == 2
    assert result.violations == dict.fromkeys(result.violations, 0), result.violations
