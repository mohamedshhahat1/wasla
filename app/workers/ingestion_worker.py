"""The worker that turns submitted documents into retrievable generations.

Why this is not in the request that submitted the document: chunking and
embedding call a provider, and a large document is several embedding requests.
An upload endpoint that waited for that would time out, and a webhook that waited
for it would be retried by Meta (claude.md §61).

**What this worker does not do any more is hold a transaction while it waits for
the provider** (RAG-04). Each job runs `run_indexing` over *committed units* - a
claim, a renewal and a meter per batch, and a publish or a failure record, each
its own short transaction - with the embedding calls between them and no
connection held. See `app.services.document_indexing` for the state machine.

**The queue's retry policy is not the document's retry budget** (RAG-01). A
provider failure is recorded on the generation - retry later, or failed - and the
job is then *acknowledged*: the generation's `next_retry_at` and attempt count are
what bring it back, through the recovery sweep, and what stop it. The queue's own
`IDEMPOTENT_RETRY` is left for failures that happen before anything could be
recorded: a job that arrived before the upload committed, a database that is not
there.
"""

from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.core.telemetry import record_ingestion_outcome
from app.core.tracing import JOB_OUTCOME
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.db.session import Database
from app.integrations.openai.embeddings import (
    EMBED_INGEST,
    EmbeddingsClient,
    build_http_client,
)
from app.services.document_indexing import IndexingOutcome, committed_units, run_indexing
from app.workers.dispatch import (
    SUCCEEDED,
    JobIdentity,
    handle_failure,
    job_span,
    record_success,
)
from app.workers.ingestion_queue import (
    BLOCK_SECONDS,
    IngestionJob,
    IngestionQueue,
)
from app.workers.queue import JobEnvelope, MalformedJobError
from app.workers.retry import IDEMPOTENT_RETRY, NO_RETRY, FailureCategory

logger = get_logger(__name__)

# How long to wait after a failed reserve before trying again.
RETRY_DELAY_SECONDS = 5.0

# The name this queue reports itself under, in metrics and in dead-letter
# records. Short and fixed, because it is a metric label.
JOB_TYPE = "ingestion"


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
        """Process jobs until asked to stop.

        A failure reserving work is caught here rather than allowed out. Every
        worker in this process shares one event loop, so an exception escaping
        this loop takes the others down with it - and the most likely cause is a
        momentary Redis hiccup, which is not a reason to stop answering
        customers. The job itself is already protected inside `run_once`.
        """
        self._running = True
        logger.info("knowledge.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("knowledge.reserve_failed")
                # Paced, so a persistent outage is not a spin loop against a
                # Redis that is not there.
                await asyncio.sleep(RETRY_DELAY_SECONDS)
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

        envelope = JobEnvelope.decode(raw)
        # One span per attempt, rooted in the trace the job was queued
        # from. A carrier the envelope could not carry starts a new trace
        # and the attempt runs identically - see `job_span`.
        with job_span(job_type=JOB_TYPE, envelope=envelope) as attempt:
            attempt.set_attribute(JOB_OUTCOME, await self._attempt(raw, envelope))
        return True

    async def _attempt(self, raw: str, envelope: JobEnvelope) -> str:
        """Run one reserved job, and report how the attempt ended.

        Split out of `run_once` so the whole attempt - decoding, handling,
        and whatever is written down when it fails - happens inside one
        span, and the value it returns is that span's outcome. The four
        strings it can answer are the domain of `wasla.job_outcome`.
        """
        try:
            job = IngestionJob.decode(envelope.body)
        except MalformedJobError:
            # Retrying would fail identically forever.
            logger.warning("knowledge.job_malformed")
            outcome = await handle_failure(
                self._queue,
                raw,
                envelope,
                job_type=JOB_TYPE,
                identity=JobIdentity(),
                category=FailureCategory.MALFORMED,
                policy=NO_RETRY,
            )
            return outcome.action

        try:
            await self._handle(job)
        except Exception as error:
            logger.exception(
                "knowledge.job_failed",
                extra={"document_id": str(job.document_id)},
            )
            outcome = await handle_failure(
                self._queue,
                raw,
                envelope,
                job_type=JOB_TYPE,
                identity=JobIdentity(tenant_id=job.tenant_id, job_id=job.document_id),
                error=error,
                policy=IDEMPOTENT_RETRY,
            )
            return outcome.action

        await self._queue.release(raw)
        await record_success(job_type=JOB_TYPE)
        return SUCCEEDED

    async def _handle(self, job: IngestionJob) -> None:
        if not self._settings.openai_api_key:
            # Configuration, not the document's fault: nothing is claimed and no
            # attempt is spent, so the backlog drains the moment a key is set.
            # The backlog gauge and its alert make the wait visible.
            logger.warning(
                "knowledge.embeddings_not_configured",
                extra={"document_id": str(job.document_id)},
            )
            await record_ingestion_outcome(str(IndexingOutcome.SKIPPED))
            return

        async with build_http_client() as http:
            embeddings = EmbeddingsClient(
                http=http,
                api_key=self._settings.openai_api_key,
                model=self._settings.openai_embedding_model,
                dimensions=EMBEDDING_DIMENSIONS,
                operation=EMBED_INGEST,
            )
            run = await run_indexing(
                committed_units(self._database, tenant_id=job.tenant_id),
                document_id=job.document_id,
                embeddings=embeddings,
            )

        await record_ingestion_outcome(str(run.outcome))
        logger.info(
            "knowledge.job_completed",
            extra={
                "document_id": str(job.document_id),
                "outcome": str(run.outcome),
                "refusal": str(run.refusal) if run.refusal is not None else None,
                "generation": run.generation,
                "chunks": run.chunks,
                "failure_code": run.failure.code if run.failure is not None else None,
            },
        )
