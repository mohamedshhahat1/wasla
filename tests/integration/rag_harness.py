"""Scaffolding for document indexing driven through the real, committing worker.

Real PostgreSQL and real Redis; the embeddings provider faked at the HTTP
transport, programmable per test and counted, with a gate a test can close to
hold a worker inside a provider call while it does something concurrent. State
is read back on fresh sessions, so what a test sees is what was committed.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete, select, text, update

import app.workers.ingestion_worker as ingestion_worker_module
from app.core.config import Settings
from app.core.embedding_space import OPENAI_PROVIDER, EmbeddingSpace
from app.core.redis import RedisClient
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.enums import TenantStatus
from app.db.models.knowledge import (
    EMBEDDING_DIMENSIONS,
    Document,
    DocumentChunk,
    DocumentIndexGeneration,
    GenerationState,
)
from app.db.models.tenant import Tenant
from app.db.session import Database
from app.integrations.openai.embeddings import EmbeddingsClient
from app.repositories.knowledge_repository import (
    DocumentChunkRepository,
)
from app.services.knowledge_service import KnowledgeService
from app.workers.ingestion_queue import IngestionQueue
from app.workers.ingestion_recovery import IngestionRecoveryWorker
from app.workers.ingestion_worker import IngestionWorker
from tests.fake_embeddings import FAKE_MODEL, embed_text
from tests.integration.ai_harness import JsonObject, embedding_response, settings_for

REDIS_URL = os.environ.get("WASLA_TEST_REDIS_URL", "redis://localhost:6379/15")
POLICY = "Refund policy\n\nItems may be returned within fourteen days for a full refund."
SMALL = EmbeddingSpace(provider=OPENAI_PROVIDER, model=FAKE_MODEL, dimensions=EMBEDDING_DIMENSIONS)
LARGE = EmbeddingSpace(
    provider=OPENAI_PROVIDER, model="text-embedding-3-large", dimensions=EMBEDDING_DIMENSIONS
)

Handler = Callable[[JsonObject], Awaitable[httpx.Response]]


def long_document(paragraphs: int = 110) -> str:
    """Enough paragraphs for more than one embedding batch."""
    return "\n\n".join(
        f"Section {n}. " + " ".join(f"clause{n}w{w}" for w in range(120)) for n in range(paragraphs)
    )


@dataclass
class Provider:
    """The embeddings endpoint, programmable and counted.

    `gate`, when set, holds each request until released, and `entered` fires as
    a request arrives - which is how a test knows a worker is genuinely inside
    a provider call before it does something concurrent with it.
    """

    handler: Handler | None = None
    requests: list[JsonObject] = field(default_factory=list)
    gate: asyncio.Event | None = None
    gate_first_only: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    def transport(self) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            import json

            body: JsonObject = json.loads(request.content)
            self.requests.append(body)
            self.entered.set()
            gate = self.gate
            if gate is not None and (not self.gate_first_only or len(self.requests) == 1):
                await gate.wait()
            if self.handler is not None:
                return await self.handler(body)
            return embedding_response(body)

        return httpx.MockTransport(handle)


def respond(status: int, *, retry_after: str | None = "0") -> Handler:
    async def handle(_body: JsonObject) -> httpx.Response:
        headers = {"retry-after": retry_after} if retry_after is not None else {}
        return httpx.Response(status, json={"error": {"code": "scripted"}}, headers=headers)

    return handle


def vectors_of(value: object = ..., *, width: int | None = None) -> Handler:
    async def handle(body: JsonObject) -> httpx.Response:
        return embedding_response(body, value=value, width=width)

    return handle


@dataclass
class Indexing:
    url: str
    database: Database
    redis: Redis
    namespace: str
    provider: Provider
    settings: Settings
    tenants: list[uuid.UUID] = field(default_factory=list)

    def configure(self, **overrides: Any) -> None:
        self.settings = settings_for(self.url, **overrides)

    def queue(self) -> IngestionQueue:
        return IngestionQueue(self.redis, namespace=self.namespace, visibility_timeout_seconds=60)

    def worker(self, database: Database | None = None) -> IngestionWorker:
        worker = IngestionWorker(
            database=database or self.database,
            redis=RedisClient(self.settings),
            settings=self.settings,
        )
        worker._queue = self.queue()
        return worker

    def recovery(self) -> IngestionRecoveryWorker:
        worker = IngestionRecoveryWorker(
            database=self.database,
            redis=RedisClient(self.settings),
            settings=self.settings,
            grace_seconds=0,
        )
        worker._queue = self.queue()
        return worker

    def client(self) -> EmbeddingsClient:
        return EmbeddingsClient(
            http=httpx.AsyncClient(transport=self.provider.transport()),
            api_key="sk-test-not-real",
            model=self.settings.openai_embedding_model,
            dimensions=EMBEDDING_DIMENSIONS,
        )

    async def workspace(self) -> uuid.UUID:
        async with self.database.session() as session:
            tenant = Tenant(name="Indexing", slug=f"idx-{uuid.uuid4().hex[:10]}")
            session.add(tenant)
            await session.flush()
            tenant_id = tenant.id
        self.tenants.append(tenant_id)
        return tenant_id

    async def submit(
        self, tenant_id: uuid.UUID, body: str = POLICY, *, enqueue: bool = True
    ) -> uuid.UUID:
        async with self.database.session() as session:
            knowledge = KnowledgeService(
                session=session, tenant_id=tenant_id, queue=self.queue() if enqueue else None
            )
            base = await knowledge.ensure_default_knowledge_base()
            view, _ = await knowledge.submit(knowledge_base_id=base.id, title="Doc", raw=body)
            return view.document.id

    async def reindex(self, tenant_id: uuid.UUID, document_id: uuid.UUID, **kwargs: Any) -> None:
        async with self.database.session() as session:
            await KnowledgeService(
                session=session, tenant_id=tenant_id, queue=self.queue()
            ).reindex(document_id, **kwargs)

    async def drain(self, *, budget: int = 20, database: Database | None = None) -> int:
        worker = self.worker(database)
        consumed = 0
        while consumed < budget and await worker.run_once(wait_seconds=1):
            consumed += 1
        return consumed

    async def queued(self) -> int:
        return int(await self.redis.llen(f"{self.namespace}:pending"))  # type: ignore[misc]

    async def dead(self) -> int:
        return int(await self.redis.llen(f"{self.namespace}:dead"))  # type: ignore[misc]

    async def generations(self, document_id: uuid.UUID) -> list[DocumentIndexGeneration]:
        async with self.database.session() as session:
            rows = await session.scalars(
                select(DocumentIndexGeneration)
                .where(DocumentIndexGeneration.document_id == document_id)
                .order_by(DocumentIndexGeneration.number)
            )
            return list(rows)

    async def document(self, document_id: uuid.UUID) -> Document | None:
        async with self.database.session() as session:
            return await session.get(Document, document_id)

    async def chunk_generations(self, document_id: uuid.UUID) -> list[uuid.UUID]:
        async with self.database.session() as session:
            rows = await session.scalars(
                select(DocumentChunk.generation_id).where(DocumentChunk.document_id == document_id)
            )
            return list(rows)

    async def execute(self, statement: Any) -> None:
        async with self.database.session() as session:
            await session.execute(statement)

    async def make_due(self, document_id: uuid.UUID) -> None:
        """Let a transient failure's backoff elapse, without waiting for it."""
        await self.execute(
            update(DocumentIndexGeneration)
            .where(
                DocumentIndexGeneration.document_id == document_id,
                DocumentIndexGeneration.state == GenerationState.PENDING,
            )
            .values(next_retry_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    async def expire_lease(self, document_id: uuid.UUID) -> None:
        await self.execute(
            update(DocumentIndexGeneration)
            .where(
                DocumentIndexGeneration.document_id == document_id,
                DocumentIndexGeneration.state == GenerationState.PROCESSING,
            )
            .values(claimed_at=datetime.now(UTC) - timedelta(hours=1))
        )

    async def set_workspace(
        self, tenant_id: uuid.UUID, *, status: TenantStatus | None = None, deleted: bool = False
    ) -> None:
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status
        if deleted:
            values["deleted_at"] = datetime.now(UTC)
        await self.execute(update(Tenant).where(Tenant.id == tenant_id).values(**values))

    async def search(
        self, tenant_id: uuid.UUID, query: str, *, space: EmbeddingSpace = SMALL
    ) -> list[tuple[uuid.UUID, str]]:
        async with self.database.session() as session:
            found = await DocumentChunkRepository(session, tenant_id=tenant_id).search(
                embedding=embed_text(query), space=space, limit=10
            )
            return [(row.chunk.generation_id, row.chunk.content) for row in found]

    async def audit(self, tenant_id: uuid.UUID, action: AuditAction) -> list[AuditLog]:
        async with self.database.session() as session:
            rows = await session.scalars(
                select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == action)
            )
            return list(rows)

    async def cleanup(self) -> None:
        # A test that failed while a worker was held at the provider gate would
        # otherwise leave that worker - and any lock it holds - waiting for ever,
        # and the delete below waiting behind it. Released first, and given a
        # moment to finish, so a failing concurrency test fails instead of hangs.
        if self.provider.gate is not None and not self.provider.gate.is_set():
            self.provider.gate.set()
            await asyncio.sleep(0.5)
        async with self.database.session() as session:
            if self.tenants:
                await session.execute(delete(AuditLog).where(AuditLog.tenant_id.in_(self.tenants)))
                await session.execute(delete(Tenant).where(Tenant.id.in_(self.tenants)))
        async for key in self.redis.scan_iter(match=f"{self.namespace}*"):
            await self.redis.delete(key)
        # These suites commit and delete thousands of chunks. Deleted tuples stay
        # in the HNSW graph until vacuum and spend an approximate scan's candidate
        # budget, so a vector-index suite running afterwards would be measuring
        # this suite's garbage. Autovacuum does this in a deployment; a test run
        # is too short to wait for it.
        async with self.database.engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text("VACUUM (ANALYZE) document_chunks"))


@pytest_asyncio.fixture
async def indexing(
    prepared_database: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Indexing]:
    provider = Provider()
    monkeypatch.setattr(
        ingestion_worker_module,
        "build_http_client",
        lambda *args, **kwargs: httpx.AsyncClient(transport=provider.transport()),
    )
    settings = settings_for(prepared_database)
    database = Database(settings)
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    harness = Indexing(
        url=prepared_database,
        database=database,
        redis=redis,
        namespace=f"test:ingest:{uuid.uuid4().hex[:10]}",
        provider=provider,
        settings=settings,
    )
    try:
        yield harness
    finally:
        try:
            await harness.cleanup()
        finally:
            await redis.aclose()
            await database.dispose()
