"""The worker that answers conversations with an agent.

Why this is not in the webhook (claude.md §61): Meta retries a webhook that does
not answer quickly, and an inference reliably takes long enough to trigger that.
Doing the work there would duplicate it rather than deliver it. The webhook
stores the message and enqueues; this reads the queue.

**The provider is called with no database connection held** (ADR-080). One
session spans the turn, but `AgentOrchestrator` commits it and hands the
connection back before each inference and before each per-round reservation, so
a turn waiting on OpenAI is not a turn occupying a slot in the pool. That is
what stops the effective concurrency of an agent turn being
`pool_size + max_overflow` instead of the queue depth.

What that costs, stated rather than hidden: the turn is no longer one
transaction. Each round commits what the previous round's tools finished, so a
turn that dies partway leaves the work it completed rather than none of it -
which is the better direction, because this queue does not retry after the
provider is engaged and a rolled-back lead is a lead the customer will not give
twice. And a commit ends a snapshot: state read before an inference may be
stale after it, so the orchestrator re-reads the conversation mode before it
offers a reply. A handoff cannot be caught half-committed, because a handoff
ends the loop rather than taking another round.

**Why this queue retries less than the others.** An agent turn is not
idempotent. It reserves an allowance, it may call tools that write rows, and
it ends by sending a customer a WhatsApp message that carries no idempotency
key - so running it twice is a second answer to one question, which is worse
for the customer than no answer at all. What *is* safe to repeat is everything
before the provider is engaged: loading the workspace, reading the allowance,
looking up the agent. Those touch nothing outside a transaction that rolls
back. `_TurnProgress` marks the moment that stops being true, and the moment
it is marked this worker's retry policy becomes `NO_RETRY` (ADR-068).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from app.agents.orchestrator import AgentOrchestrator
from app.agents.registry import ToolRegistry
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.db.models.agent import Agent
from app.db.models.billing import LimitKey
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.db.models.usage import UsageEventType
from app.db.session import Database
from app.integrations.openai.client import ResponsesClient, build_http_client
from app.integrations.openai.embeddings import EmbeddingsClient
from app.repositories.agent_repository import AgentRepository
from app.services.entitlement_service import EntitlementService
from app.services.messaging_service import MessagingService
from app.services.sentiment_reader import SentimentAnalyzer
from app.services.sentiment_service import SentimentService
from app.services.usage_service import UsageRecorder
from app.workers.dispatch import JobIdentity, handle_failure, record_success
from app.workers.queue import (
    BLOCK_SECONDS,
    AgentJob,
    AgentQueue,
    JobEnvelope,
    MalformedJobError,
)
from app.workers.retry import NO_RETRY, FailureCategory, RetryPolicy

logger = get_logger(__name__)

# How long to wait after a failed reserve before trying again.
RETRY_DELAY_SECONDS = 5.0

# The name this queue reports itself under, in metrics and in dead-letter
# records. Short and fixed, because it is a metric label.
JOB_TYPE = "agent"

# Deliberately shorter than the idempotent queues': the only failures this
# policy can ever see are the ones raised before a turn engaged the provider,
# and those are infrastructure blips that either clear in seconds or are not
# clearing today.
AGENT_RETRY = RetryPolicy(max_attempts=3, base_seconds=2.0, max_seconds=30.0)


class _TurnProgress:
    """Whether this turn has done anything the outside world can see.

    Marked immediately before the HTTP client is built, which is the last
    moment at which nothing has left this process. After it, a retry could
    bill a second inference or send a second reply, so `run_once` stops
    offering one.

    **The mark is also written to Redis**, and that is what makes it survive
    the process. An in-memory flag answers "may this worker retry", which is
    only ever asked by a worker that is still alive to ask it; a crash takes
    the flag with it and leaves a reaper guessing whether a customer already
    has a reply. `on_engage` is the reservation's stage transition, so the
    answer outlives the process that knew it (ADR-074).
    """

    __slots__ = ("_on_engage", "engaged")

    def __init__(self, on_engage: Callable[[], Awaitable[object]] | None = None) -> None:
        self.engaged = False
        self._on_engage = on_engage

    async def engage(self) -> None:
        """Record that the turn is about to talk to somebody else's API."""
        self.engaged = True
        if self._on_engage is not None:
            await self._on_engage()


class AgentWorker:
    """Reads agent jobs and answers the conversations they name."""

    def __init__(
        self,
        *,
        database: Database,
        redis: RedisClient,
        settings: Settings,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._registry = registry
        self._queue = AgentQueue(redis.client)
        self._running = False

    @property
    def queue(self) -> AgentQueue:
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
        logger.info("agent.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("agent.reserve_failed")
                # Paced, so a persistent outage is not a spin loop against a
                # Redis that is not there.
                await asyncio.sleep(RETRY_DELAY_SECONDS)
        logger.info("agent.worker_stopped")

    def stop(self) -> None:
        self._running = False

    async def run_once(self, *, wait_seconds: int = BLOCK_SECONDS) -> bool:
        """Handle at most one job. Returns whether there was one.

        Nothing raised by a single job escapes this method. A worker that dies
        on one bad job stops answering every other customer too, so the failure
        is contained to the job and either retried or recorded in the
        dead-letter list.
        """
        raw = await self._queue.reserve(wait_seconds=wait_seconds)
        if raw is None:
            return False

        envelope = JobEnvelope.decode(raw)
        try:
            job = AgentJob.decode(envelope.body)
        except MalformedJobError:
            # Retrying would fail identically forever.
            logger.warning("agent.job_malformed")
            await handle_failure(
                self._queue,
                raw,
                envelope,
                job_type=JOB_TYPE,
                identity=JobIdentity(),
                category=FailureCategory.MALFORMED,
                policy=NO_RETRY,
            )
            return True

        progress = _TurnProgress(lambda: self._queue.mark_engaged(raw))
        try:
            await self._handle(job, progress)
        except Exception as error:
            logger.exception(
                "agent.job_failed",
                extra={"conversation_id": str(job.conversation_id)},
            )
            await handle_failure(
                self._queue,
                raw,
                envelope,
                job_type=JOB_TYPE,
                identity=JobIdentity(tenant_id=job.tenant_id, job_id=job.conversation_id),
                error=error,
                # The whole of this queue's retry safety, in one expression.
                # Once the turn has engaged the provider there is no failure
                # this worker can distinguish from one that already sent a
                # reply, so it stops offering another attempt.
                policy=NO_RETRY if progress.engaged else AGENT_RETRY,
            )
            return True

        await self._queue.release(raw)
        await record_success(job_type=JOB_TYPE)
        return True

    def _reservation(self, tenant_id: uuid.UUID) -> Callable[[], Awaitable[bool]]:
        """One AI request, taken atomically, in a transaction of its own.

        A transaction of its own because `consume` holds an advisory lock until
        its transaction ends, and the turn's own transaction stays open for the
        length of an inference. Reserving on that session would hold a
        workspace's lock across a provider call and serialise every
        conversation that workspace is having.

        The reservation therefore commits before the provider is called, which
        is the safe direction: a crash between reserving and calling bills a
        request that did not happen, and the alternative bills nothing for one
        that did.
        """

        async def reserve() -> bool:
            async with self._database.session() as reservation:
                entitlements = EntitlementService(
                    reservation,
                    tenant_id=tenant_id,
                    default_plan_code=self._settings.default_plan_code,
                )
                outcome = await entitlements.consume(
                    LimitKey.PERIOD_AI_REQUESTS,
                    event_type=UsageEventType.AI_REQUEST,
                )
                return outcome.allowed

        return reserve

    async def _handle(self, job: AgentJob, progress: _TurnProgress) -> None:
        async with self._database.session() as session:
            entitlements = EntitlementService(
                session,
                tenant_id=job.tenant_id,
                default_plan_code=self._settings.default_plan_code,
            )
            # Asked for the balance rather than a yes/no (ADR-054), because the answer
            # decides how many provider calls this turn may make. One agent
            # turn is up to `MAX_ROUNDS` calls and each is metered as a
            # request, so a turn that only checked "may I make one?" could
            # knowingly spend three - which is a workspace being billed past a
            # limit the system had already read.
            # A cheap early exit only. The real enforcement is the per-round
            # reservation below, which is what holds under concurrency; this
            # just avoids building an HTTP client for a workspace that is
            # plainly out of allowance.
            allowance = await entitlements.check(LimitKey.PERIOD_AI_REQUESTS, additional=1)
            if not allowance.allowed:
                # Checked, and *not* raised. A workspace out of AI requests has
                # a billing problem; its customer has a question. The message is
                # already stored and the conversation is waiting for a person,
                # which is the honest outcome - failing the job would dead-letter
                # it and lose that (ADR-030).
                logger.warning(
                    "billing.ai_allowance_exhausted",
                    extra={
                        "event": "billing.ai_allowance_exhausted",
                        "tenant_id": str(job.tenant_id),
                        "conversation_id": str(job.conversation_id),
                    },
                )
                return

            agent: Agent | None = None
            if job.agent_id is not None:
                agents = AgentRepository(session, tenant_id=job.tenant_id)
                agent = await agents.get_by_id(job.agent_id)
                if agent is None:
                    # Retired, or from another workspace. The workspace default
                    # still applies, so the customer is not left unanswered.
                    logger.warning(
                        "agent.requested_agent_missing",
                        extra={"agent_id": str(job.agent_id)},
                    )

            # Past this line the turn can reserve an allowance, call the
            # provider and send a customer a message, none of which a second
            # attempt could tell had already happened. Awaited rather than
            # assigned because it also persists the fact, so a worker that dies
            # after this point is not mistaken for one that died before it.
            await progress.engage()
            async with build_http_client() as http:
                api_key = self._settings.openai_api_key or ""
                client = ResponsesClient(http=http, api_key=api_key)
                # Shares the turn's HTTP client: a knowledge search happens
                # inside the tool loop, so it belongs to the same request.
                embeddings = EmbeddingsClient(
                    http=http,
                    api_key=api_key,
                    model=self._settings.openai_embedding_model,
                    dimensions=EMBEDDING_DIMENSIONS,
                )
                # Shares the turn's client too. One small classification call
                # runs before the agent composes anything, which is the only
                # order in which an escalation can stop a reply rather than
                # follow one.
                sentiment = SentimentService(
                    session=session,
                    tenant_id=job.tenant_id,
                    analyzer=SentimentAnalyzer(
                        responses=client,
                        model=self._settings.openai_sentiment_model,
                    ),
                )
                orchestrator = AgentOrchestrator(
                    session=session,
                    tenant_id=job.tenant_id,
                    client=client,
                    registry=self._registry,
                    reserve_round=self._reservation(job.tenant_id),
                    embeddings=embeddings,
                    sentiment=sentiment,
                )
                outcome = await orchestrator.answer(
                    conversation_id=job.conversation_id,
                    agent=agent,
                )

            # Metered before the reply is sent, and outside the branch that
            # returns early. A turn that ended in a handoff or in silence still
            # called the provider, and a meter that only counted turns which
            # produced words would under-count exactly the conversations that
            # cost the most attention.
            UsageRecorder(session, tenant_id=job.tenant_id).ai_request(
                input_tokens=outcome.usage.input_tokens,
                output_tokens=outcome.usage.output_tokens,
                # Zero, and deliberately: the request meter is written by the
                # per-round reservation before each provider call, so counting
                # them again here would bill every turn twice. Tokens are not
                # reservable - they are only known after the call - so they are
                # still recorded here.
                requests=0,
                # From the outcome, not from `agent`: a job naming no agent
                # is answered by the workspace default, and that is the
                # model the tokens were spent on.
                model=outcome.model,
                conversation_id=job.conversation_id,
            )

            reply = outcome.reply
            if outcome.handed_off or not reply:
                # Silence is a decision here, not a failure: a handoff, an
                # escalation, a conversation a human owns, or nothing worth
                # saying.
                return

            messaging = MessagingService(
                session=session,
                settings=self._settings,
                tenant_id=job.tenant_id,
            )
            await messaging.send_text(conversation_id=job.conversation_id, body=reply)
