"""The worker that turns submitted documents into retrievable chunks.

Why this is not in the request that submitted the document: extraction, chunking
and embedding call a provider, and a large document is dozens of embedding
requests. An upload endpoint that waited for that would time out, and a webhook
that waited for it would be retried by Meta (claude.md §61).

Failure handling differs from the agent worker's in one way that matters. A
failed agent turn leaves nothing behind but a log line; a failed ingestion
leaves the document in `FAILED` with the reason on the row, so the person who
uploaded it can see what went wrong and fix it. The job is dead-lettered as well,
because the document says what broke and the job says that anything tried.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.db.session import Database
from app.integrations.openai.embeddings import EmbeddingsClient, build_http_client
from app.services.knowledge_service import KnowledgeService
from app.workers.ingestion_queue import (
    BLOCK_SECONDS,
    IngestionJob,
    IngestionQueue,
)
from app.workers.queue import MalformedJobError

logger = get_logger(__name__)


class IngestionWorker:
    """Reads ingestion jobs and indexes the documents they name."""

    def __init__(
        self,
        *,
        database: Database,
        redis: RedisClient,
        settings: Settings,
    ) -> None:
        self._database = database
        self._settings = settings
        self._queue = IngestionQueue(redis.client)
        self._running = False

    @property
    def queue(self) -> IngestionQueue:
        return self._queue

    async def run_forever(self) -> None:
        """Process jobs until asked to stop."""
        self._running = True
        logger.info("knowledge.worker_started")
        while self._running:
            await self.run_once()
        logger.info("knowledge.worker_stopped")

    def stop(self) -> None:
        self._running = False

    async def run_once(self, *, wait_seconds: int = BLOCK_SECONDS) -> bool:
        """Handle at most one job. Returns whether there was one.

        Nothing raised by a single job escapes this method. A worker that dies
        on one unreadable document stops indexing every other workspace's too,
        so the failure is contained to the job.
        """
        raw = await self._queue.reserve(wait_seconds=wait_seconds)
        if raw is None:
            return False

        try:
            job = IngestionJob.decode(raw)
        except MalformedJobError:
            # Retrying would fail identically forever.
            logger.warning("knowledge.job_malformed")
            await self._queue.fail(raw)
            return True

        try:
            await self._handle(job)
        except Exception:
            logger.exception(
                "knowledge.job_failed",
                extra={"document_id": str(job.document_id)},
            )
            await self._queue.fail(raw)
            return True

        await self._queue.release(raw)
        return True

    async def _handle(self, job: IngestionJob) -> None:
        async with self._database.session() as session:
            knowledge = KnowledgeService(session=session, tenant_id=job.tenant_id)
            async with build_http_client() as http:
                embeddings = EmbeddingsClient(
                    http=http,
                    api_key=self._settings.openai_api_key or "",
                    model=self._settings.openai_embedding_model,
                    dimensions=EMBEDDING_DIMENSIONS,
                )
                result = await knowledge.ingest(
                    document_id=job.document_id,
                    embeddings=embeddings,
                )

        logger.info(
            "knowledge.job_completed",
            extra={
                "document_id": str(job.document_id),
                "chunks": result.chunks_written,
                "reused": result.reused,
            },
        )
