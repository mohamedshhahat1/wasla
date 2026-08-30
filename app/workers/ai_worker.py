"""The worker that answers conversations with an agent.

Why this is not in the webhook (claude.md §61): Meta retries a webhook that does
not answer quickly, and an inference reliably takes long enough to trigger that.
Doing the work there would duplicate it rather than deliver it. The webhook
stores the message and enqueues; this reads the queue.

Known trade-off, recorded rather than hidden: the provider call happens inside
the database session. Tools mutate rows during the loop, so that session is the
consistency boundary for the whole turn, and the alternative of writing in a
second session would let a handoff commit while the reply that explained it did
not. The cost is a pooled connection held for the length of an inference, which
bounds how many workers one pool supports.
"""

from __future__ import annotations

import asyncio

from app.agents.orchestrator import MAX_ROUNDS, AgentOrchestrator
from app.agents.registry import ToolRegistry
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.db.models.agent import Agent
from app.db.models.billing import LimitKey
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.db.session import Database
from app.integrations.openai.client import ResponsesClient, build_http_client
from app.integrations.openai.embeddings import EmbeddingsClient
from app.repositories.agent_repository import AgentRepository
from app.services.entitlement_service import EntitlementService
from app.services.messaging_service import MessagingService
from app.services.sentiment_reader import SentimentAnalyzer
from app.services.sentiment_service import SentimentService
from app.services.usage_service import UsageRecorder
from app.workers.queue import BLOCK_SECONDS, AgentJob, AgentQueue, MalformedJobError

logger = get_logger(__name__)

# How long to wait after a failed reserve before trying again.
RETRY_DELAY_SECONDS = 5.0


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
        is contained to the job and recorded in the dead-letter list.
        """
        raw = await self._queue.reserve(wait_seconds=wait_seconds)
        if raw is None:
            return False

        try:
            job = AgentJob.decode(raw)
        except MalformedJobError:
            # Retrying would fail identically forever.
            logger.warning("agent.job_malformed")
            await self._queue.fail(raw)
            return True

        try:
            await self._handle(job)
        except Exception:
            logger.exception(
                "agent.job_failed",
                extra={"conversation_id": str(job.conversation_id)},
            )
            await self._queue.fail(raw)
            return True

        await self._queue.release(raw)
        return True

    async def _handle(self, job: AgentJob) -> None:
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

            # `remaining` is None for an unlimited plan, which is the only case
            # that gets the full round budget without arithmetic.
            #
            # This closes the deterministic overshoot, not the concurrent one:
            # two workers answering the same workspace at the same instant both
            # read this balance before either records against it, so N workers
            # can still spend N rounds beyond the limit. Closing that needs an
            # atomic reservation, which the entitlement system does not have for
            # any limit - it is a platform-wide property rather than an AI one,
            # and is recorded in docs/AI_AGENTS.md rather than half-fixed here.
            permitted_rounds = (
                MAX_ROUNDS if allowance.remaining is None else min(MAX_ROUNDS, allowance.remaining)
            )

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
                    max_rounds=permitted_rounds,
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
                # One per provider call, not one per turn: an agent that ran
                # three tool rounds called the provider three times.
                requests=outcome.rounds,
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
