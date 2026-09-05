"""The worker that reads what customers attach, then lets an agent answer.

This worker sits *in front of* the agent worker rather than beside it, and that
is the whole design. A photograph is a question, and answering it before anyone
has looked at the picture produces a reply about nothing. So the webhook enqueues
a media job instead of an agent job, and the agent job is enqueued here, once
there is something to answer with.

The hard part is not the reading. It is deciding when the conversation is ready.
One delivery can carry two photographs, which become two jobs, possibly on two
workers. Each finishes and asks "is anything still unread here?" - and if both
ask at the same moment, both see nothing and both ask an agent to reply. The
customer gets two answers to one question, because an agent turn is not
idempotent.

`ConversationMediaGate` turns that race into a queue: the row lock makes the
second worker wait for the first to commit before it counts. Cheapest correct
answer available - no new table, no Redis key, and the lock is held for one
count.

The second hard part is *when* the agent job may exist, and it is a different
question from who may create it. The transcript this worker writes is the whole
reason an agent turn is being asked for, so the job must not be queued until
that write is durable:

    read the file
    resolve the row, take the gate, count the siblings   -> COMMIT
    --  the transcript is now visible to every other transaction
    release this job and queue the agent turn, in that order

Queueing from inside the transaction - which is what this worker used to do -
put a job on a queue that a worker could consume before the transcript
committed. Unlike ADR-089's race the conversation exists, so nothing answers
`not_found` and nothing retries: the turn reads `[image, not yet read]` and
answers a photograph it has not seen (ADR-092).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.core.storage import MediaStorage, build_media_storage
from app.core.tracing import JOB_OUTCOME
from app.db.models.media import MediaStatus, MessageMedia
from app.db.session import Database
from app.integrations.openai.client import ResponsesClient
from app.integrations.openai.client import build_http_client as build_openai_client
from app.integrations.openai.transcription import TranscriptionClient
from app.integrations.whatsapp.client import WhatsAppClient
from app.integrations.whatsapp.client import build_http_client as build_whatsapp_client
from app.repositories.media_repository import ConversationMediaGate, MediaRepository
from app.services.media_reader import MediaReader
from app.services.media_service import MediaService
from app.workers.dispatch import (
    SUCCEEDED,
    JobIdentity,
    handle_failure,
    job_span,
    record_success,
)
from app.workers.media_queue import BLOCK_SECONDS, MediaJob, MediaQueue
from app.workers.queue import AgentJob, AgentQueue, JobEnvelope, MalformedJobError
from app.workers.retry import IDEMPOTENT_RETRY, NO_RETRY, FailureCategory

logger = get_logger(__name__)

# How long to wait after a failed reserve before trying again.
RETRY_DELAY_SECONDS = 5.0

# The name this queue reports itself under, in metrics and in dead-letter
# records. Short and fixed, because it is a metric label.
JOB_TYPE = "media"

# The two halves that talk to somebody else's service, injectable so a test can
# drive the loop without a network. The same shape `FollowUpWorker` uses for its
# messaging service, and for the same reason: a worker that can only be
# exercised against real providers is a worker whose failure paths are never
# exercised at all.
WhatsAppFactory = Callable[[httpx.AsyncClient], WhatsAppClient | None]
ReaderFactory = Callable[[httpx.AsyncClient], MediaReader]


class MediaWorker:
    """Reads media jobs, understands the files they name, and releases the reply."""

    def __init__(
        self,
        *,
        database: Database,
        redis: RedisClient,
        settings: Settings,
        storage: MediaStorage | None = None,
        whatsapp_factory: WhatsAppFactory | None = None,
        reader_factory: ReaderFactory | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._whatsapp_factory = whatsapp_factory
        self._reader_factory = reader_factory
        # Injected so a test can hand in a store backed by a temporary
        # directory; otherwise built from the same factory the API uses, so the
        # two processes cannot end up writing to different places (ADR-077).
        self._storage = storage or build_media_storage(settings)
        self._queue = MediaQueue(redis.client)
        self._agents = AgentQueue(redis.client)
        self._running = False

    @property
    def queue(self) -> MediaQueue:
        return self._queue

    async def run_forever(self) -> None:
        """Process jobs until asked to stop.

        A failure reserving work is caught here rather than allowed out. Every
        worker in this process shares one event loop, so an exception escaping
        this loop takes the others down with it - and the most likely cause is a
        momentary Redis hiccup, which is not a reason to stop reading files.
        The job itself is already protected inside `run_once`.
        """
        self._running = True
        logger.info("media.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("media.reserve_failed")
                # Paced, so a persistent outage is not a spin loop against a
                # Redis that is not there.
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        logger.info("media.worker_stopped")

    def stop(self) -> None:
        self._running = False

    async def run_once(self, *, wait_seconds: int = BLOCK_SECONDS) -> bool:
        """Handle at most one job. Returns whether there was one.

        Nothing raised by a single job escapes this method. A worker that dies
        on one malformed attachment stops reading every other workspace's too,
        so the failure is contained to the job and recorded in the dead-letter
        list.
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
            job = MediaJob.decode(envelope.body)
        except MalformedJobError:
            # Retrying would fail identically forever.
            logger.warning("media.job_malformed")
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
            follow_up = await self._handle(job)
        except Exception as error:
            logger.exception("media.job_failed", extra={"media_id": str(job.media_id)})
            outcome = await handle_failure(
                self._queue,
                raw,
                envelope,
                job_type=JOB_TYPE,
                identity=JobIdentity(tenant_id=job.tenant_id, job_id=job.media_id),
                error=error,
                policy=IDEMPOTENT_RETRY,
            )
            return outcome.action

        # `_handle` has returned, so its session has committed: the transcript
        # this agent turn exists to read is durable *before* the job that reads
        # it is queued (ADR-092). The release is what makes the pair happen
        # once - `_claim_inflight` can only succeed for one caller - so a
        # worker whose lease was reaped mid-job declines here and leaves the
        # requeued attempt to do it.
        released = await self._queue.release(raw)
        if released and follow_up is not None:
            await self._enqueue_agent(follow_up)
        await record_success(job_type=JOB_TYPE)
        return SUCCEEDED

    async def _enqueue_agent(self, job: AgentJob) -> None:
        """Ask an agent to answer, without letting a queue failure undo the read.

        Logged rather than raised. Everything the turn needs is committed by
        now, and a Redis outage that costs the reply must not also cost the
        transcript by turning a finished job into a retry that re-reads the
        file at a provider's expense.
        """
        try:
            await self._agents.enqueue(job)
        except Exception:
            logger.exception(
                "media.agent_enqueue_failed",
                extra={"conversation_id": str(job.conversation_id)},
            )
            return

        logger.info(
            "media.conversation_released",
            extra={"conversation_id": str(job.conversation_id)},
        )

    async def _handle(self, job: MediaJob) -> AgentJob | None:
        """Read one file, and say whether the conversation is now answerable.

        Returns the agent job rather than queueing it. The decision needs the
        row lock and the count, which belong in this transaction; the queueing
        needs the transaction to have *committed*, which cannot happen inside
        it. Splitting the two is the whole of ADR-092.
        """
        async with self._database.session() as session:
            # `require_by_id` rather than a `None` check that returned quietly,
            # and the difference is the enqueue-before-commit race (ADR-089).
            # The webhook enqueues this job inside the transaction that created
            # the media row, so a worker can arrive before the row is visible -
            # and returning successfully there released the job, leaving the
            # customer's photograph never downloaded, never read, and never
            # answered, with nothing written down anywhere. Raising hands the
            # decision to the retry policy: attempt one asks again two seconds
            # later, and a row that is genuinely gone dead-letters on attempt
            # two where an operator can see it.
            media = await MediaRepository(session, tenant_id=job.tenant_id).require_by_id(
                job.media_id
            )

            async with (
                build_whatsapp_client() as whatsapp_http,
                build_openai_client() as openai_http,
            ):
                service = MediaService(
                    session=session,
                    tenant_id=job.tenant_id,
                    settings=self._settings,
                    storage=self._storage,
                    whatsapp=self._whatsapp(whatsapp_http),
                )
                await self._process(
                    service=service,
                    media=media,
                    reader=self._reader(openai_http),
                )

            return await self._release_conversation(session=session, job=job, media=media)

    async def _process(
        self,
        *,
        service: MediaService,
        media: MessageMedia,
        reader: MediaReader,
    ) -> None:
        """Fetch the file, then read it. Either step may decide to stop."""
        outcome = await service.download(media)
        if outcome.status is not MediaStatus.STORED:
            # Skipped, failed, or already past this point. Nothing to read.
            return

        await service.understand(media, reader=reader)

    def _whatsapp(self, http: httpx.AsyncClient) -> WhatsAppClient | None:
        """A client for fetching from Meta, or None if there is no token.

        None rather than a client that raises on construction. Not every job
        needs Meta - a file already in the store is read without going near it -
        and building the client eagerly turns a missing token into a failure for
        those jobs too, which is how a deployment without one loses every
        attachment it had already downloaded.

        A job that genuinely needs a download and finds no client is dead-
        lettered with its row left PENDING, so it can be retried once the token
        is configured rather than being marked unreadable.
        """
        if self._whatsapp_factory is not None:
            return self._whatsapp_factory(http)
        if not self._settings.meta_access_token:
            logger.warning("media.whatsapp_not_configured")
            return None
        return WhatsAppClient(
            http=http,
            access_token=self._settings.meta_access_token,
            api_version=self._settings.meta_api_version,
        )

    def _reader(self, http: httpx.AsyncClient) -> MediaReader:
        """A reader over the HTTP client this job already opened.

        Built per job rather than held on the worker: the client it wraps is
        scoped to the job, and a reader outliving it would hold a closed
        connection pool.

        Both provider clients are constructed only when there is a key. Without
        one, documents are still read - extraction needs no provider - and an
        image or a voice note is recorded as unreadable rather than crashing the
        worker.
        """
        if self._reader_factory is not None:
            return self._reader_factory(http)

        api_key = self._settings.openai_api_key
        if not api_key:
            return MediaReader(vision_model=self._settings.openai_vision_model)

        return MediaReader(
            responses=ResponsesClient(http=http, api_key=api_key),
            transcription=TranscriptionClient(
                http=http,
                api_key=api_key,
                model=self._settings.openai_transcription_model,
            ),
            vision_model=self._settings.openai_vision_model,
        )

    async def _release_conversation(
        self,
        *,
        session: AsyncSession,
        job: MediaJob,
        media: MessageMedia,
    ) -> AgentJob | None:
        """The agent turn this conversation is now owed, if it is owed one.

        Decides; it does not queue. An agent turn reads the transcript this
        transaction is still holding, so a job queued from here can be consumed
        before the row it names is visible - and unlike ADR-089's race the
        conversation *is* visible, so nothing answers `not_found` and nothing
        retries. The turn simply reads `[image, not yet read]` and answers a
        photograph it has not seen (ADR-092). The caller queues it once this
        transaction has committed.

        The lock is taken **here**, and it used to be taken at the top of
        `_handle`. Two reasons, and the first is a correctness requirement
        rather than a preference: the download now commits its object intent
        part-way through (ADR-087), and a commit ends the transaction the lock
        lives in - so a lock taken before it would be gone by the time this
        question is asked, and the race it exists to stop would be back.

        Taking it here is equally correct and strictly cheaper. What has to be
        serialised is *this* pair - the media row's final state and the count
        that reads every sibling's - and both are in this transaction, which
        commits while the lock is still held. A sibling worker therefore waits
        here, then counts, then sees the truth. The intermediate states are
        safe to leave unlocked because every one of them - PENDING,
        DOWNLOADING, STORED - is unresolved, so a sibling counting mid-download
        sees work outstanding and correctly declines to release the reply.

        What it stops holding is the whole of a Meta fetch, an object write and
        an inference - a row lock across three network calls, which is the
        thing ADR-080 exists to keep out of this codebase.

        **Deciding under the lock is what keeps the answer single.** Only the
        worker that finishes last can see a count of zero, because a sibling
        cannot commit its own row without first waiting here - so exactly one
        caller is handed a job, and moving the queueing outside the transaction
        does not change that.
        """
        await ConversationMediaGate(session, tenant_id=job.tenant_id).lock(media.conversation_id)

        remaining = await MediaRepository(session, tenant_id=job.tenant_id).count_unresolved(
            media.conversation_id
        )
        if remaining:
            logger.info(
                "media.conversation_still_waiting",
                extra={"conversation_id": str(media.conversation_id), "remaining": remaining},
            )
            return None

        return AgentJob(tenant_id=job.tenant_id, conversation_id=media.conversation_id)
