"""The worker that answers conversations with an agent.

Why this is not in the webhook (claude.md §61): Meta retries a webhook that does
not answer quickly, and an inference reliably takes long enough to trigger that.
Doing the work there would duplicate it rather than deliver it. The webhook
stores the message and enqueues; this reads the queue.

**The provider is called with no database connection held** (ADR-080). One
session spans the turn, but `AgentOrchestrator` commits it and hands the
connection back before each inference and before each per-round meter, so a
turn waiting on OpenAI is not a turn occupying a slot in the pool. That is what
stops the effective concurrency of an agent turn being
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

**What a customer's plan pays for is a turn, not a provider call** (AI-02). One
turn is a sentiment classification and one to three inference rounds. The
allowance is `PERIOD_AI_TURNS`, reserved exactly once, in the same transaction
that engages the turn - so a duplicate job that lost the claim spends nothing,
and a reservation that could not engage is rolled back rather than charged for a
turn somebody else is running. Every provider call is still recorded as
`AI_REQUEST` with its tokens, because that is what the platform pays for; it is
cost accounting and nothing checks it against a limit.

**Why this queue retries less than the others.** An agent turn is not
idempotent. It reserves an allowance, it may call tools that write rows, and
it ends by sending a customer a WhatsApp message - so running it twice is a
second answer to one question, which is worse for the customer than no answer
at all. What *is* safe to repeat is everything before the provider is engaged:
loading the workspace, reading the conversation, looking up the agent. Those
touch nothing outside a transaction that rolls back. `_TurnProgress` marks the
moment that stops being true, and the moment it is marked this worker's retry
policy becomes `NO_RETRY` (ADR-068).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Final

from app.agents.orchestrator import AgentOrchestrator, plan_turn
from app.agents.registry import ToolRegistry
from app.agents.reply import prepare_channel_reply
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.core.tracing import JOB_OUTCOME
from app.db.models.agent import Agent
from app.db.models.analytics import AnalyticsSource
from app.db.models.billing import LimitKey
from app.db.models.conversation import ConversationMode, MessageOrigin
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.db.models.usage import UsageEventType
from app.db.session import Database
from app.integrations.openai.client import ResponsesClient, build_http_client
from app.integrations.openai.embeddings import EmbeddingsClient
from app.repositories.agent_repository import AgentRepository
from app.repositories.agent_turn_repository import AgentTurnRepository
from app.repositories.conversation_repository import ConversationRepository
from app.services.entitlement_service import EntitlementService
from app.services.inbox_service import InboxService
from app.services.messaging_service import MessagingService
from app.services.sentiment_reader import SentimentAnalyzer
from app.services.sentiment_service import SentimentService
from app.services.usage_service import AI_PURPOSE_AGENT, UsageRecorder
from app.workers.dispatch import (
    SUCCEEDED,
    JobIdentity,
    handle_failure,
    job_span,
    record_success,
)
from app.workers.queue import (
    BLOCK_SECONDS,
    AgentJob,
    AgentQueue,
    JobEnvelope,
    MalformedJobError,
)
from app.workers.retry import (
    FIRST_ATTEMPT_TRANSIENT,
    NO_RETRY,
    FailureCategory,
    RetryPolicy,
)

logger = get_logger(__name__)

# How long to wait after a failed reserve before trying again.
RETRY_DELAY_SECONDS = 5.0

# The name this queue reports itself under, in metrics and in dead-letter
# records. Short and fixed, because it is a metric label.
JOB_TYPE = "agent"

# What a colleague reads on a conversation the plan would not let an agent
# answer. Led by a fixed code so an inbox filter or a support script can find
# these without parsing prose; written for the business, never sent to the
# customer, who is owed an answer rather than an explanation of somebody's
# billing (AI-02). Conversation.handoff_reason is String(200).
QUOTA_HANDOFF_REASON: Final = (
    "AI_QUOTA_EXHAUSTED: this workspace's AI turn allowance for the billing period "
    "is used up, so a person needs to answer."
)

# Deliberately shorter than the idempotent queues': the only failures this
# policy can ever see are the ones raised before a turn engaged the provider,
# and those are infrastructure blips that either clear in seconds or are not
# clearing today.
AGENT_RETRY = RetryPolicy(
    max_attempts=3,
    base_seconds=2.0,
    max_seconds=30.0,
    # One more attempt for a conversation that is not visible *yet*. The
    # webhook enqueues this job inside the transaction that created the
    # conversation and commits after the handler returns, so a worker can
    # arrive first (ADR-089). Retried once, and only from the pre-engagement
    # side: the policy below is what a turn that has already called a provider
    # gets, and it carries no such door.
    first_attempt_transient=FIRST_ATTEMPT_TRANSIENT,
)


class _Reservation(StrEnum):
    """What taking a turn's allowance concluded."""

    #: Charged, and the turn is engaged. The provider may now be called.
    RESERVED = "reserved"
    #: The plan has no turn left. Nothing was charged and nothing engaged.
    REFUSED = "refused"
    #: Charged and then rolled back, because the turn was no longer ours to
    #: engage. Somebody else is running it; this attempt does nothing.
    LOST = "lost"


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
            job = AgentJob.decode(envelope.body)
        except MalformedJobError:
            # Retrying would fail identically forever.
            logger.warning("agent.job_malformed")
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

        progress = _TurnProgress(lambda: self._queue.mark_engaged(raw))
        try:
            await self._handle(job, progress)
        except Exception as error:
            logger.exception(
                "agent.job_failed",
                extra={"conversation_id": str(job.conversation_id)},
            )
            outcome = await handle_failure(
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
            return outcome.action

        await self._queue.release(raw)
        await record_success(job_type=JOB_TYPE)
        return SUCCEEDED

    def _round_meter(self, job: AgentJob) -> Callable[[str], Awaitable[None]]:
        """Record one provider request, in a transaction of its own, before it is made.

        Cost accounting and not entitlement (AI-02): nothing here can refuse,
        because the customer's allowance was one turn and it was reserved
        before the turn engaged. A transaction of its own because the turn's
        session has just handed its connection back for the inference, and a
        write on it would check one straight out again (ADR-080).

        Committed before the call, which is the safe direction for a cost
        ledger: a crash between recording and calling records a request that
        did not happen, and the alternative records nothing for one that did.
        """

        async def meter(model: str) -> None:
            async with self._database.session() as metering:
                UsageRecorder(metering, tenant_id=job.tenant_id).ai_request(
                    input_tokens=0,
                    output_tokens=0,
                    requests=1,
                    model=model,
                    conversation_id=job.conversation_id,
                    purpose=AI_PURPOSE_AGENT,
                )

        return meter

    async def _claim_turn(self, job: AgentJob) -> bool:
        """Take ownership of this logical turn, or report that somebody has it.

        A transaction of its own, and committed here rather than with the rest
        of the turn: the turn's own session stays open across an inference, and
        a claim that commits only at the end of the turn is a claim that is
        invisible to the duplicate arriving while the inference runs.
        Committing first is also the safe direction - a crash between claiming
        and engaging leaves a `CLAIMED` row whose lease expires, and the next
        attempt adopts it.

        A job carrying no trigger proceeds. That is a job an older build
        enqueued, and refusing it would leave a customer unanswered in order to
        protect them from a duplicate.
        """
        if job.trigger_message_id is None:
            logger.info(
                "agent.turn_unkeyed",
                extra={
                    "event": "agent.turn_unkeyed",
                    "conversation_id": str(job.conversation_id),
                },
            )
            return True

        async with self._database.session() as claim:
            owned = await AgentTurnRepository(claim, tenant_id=job.tenant_id).claim(
                conversation_id=job.conversation_id,
                trigger_message_id=job.trigger_message_id,
                worker_id=self._queue.worker_id,
            )
        if not owned:
            logger.info(
                "agent.turn_already_answered",
                extra={
                    "event": "agent.turn_already_answered",
                    "conversation_id": str(job.conversation_id),
                    "trigger_message_id": str(job.trigger_message_id),
                },
            )
        return owned

    async def _reserve_turn(self, job: AgentJob) -> _Reservation:
        """Charge this turn to the plan and engage it, as one transaction.

        One transaction because the two facts must not disagree (AI-02). A
        charge without the engagement is a customer billed for a turn that
        another attempt will run and bill again; an engagement without the
        charge is a turn the plan never paid for. Together, the point of no
        return and the charge are the same commit.

        The allowance is taken under `consume`'s advisory lock, so N workers
        racing an allowance of N get exactly N reservations - and the lock is
        released when this short transaction ends, not held across an
        inference. A turn that turns out not to be ours to engage rolls its
        charge back; a trigger-less legacy job has no turn row to engage and
        is charged on its own.
        """
        async with self._database.session() as reservation:
            entitlements = EntitlementService(
                reservation,
                tenant_id=job.tenant_id,
                default_plan_code=self._settings.default_plan_code,
            )
            allowance = await entitlements.consume(
                LimitKey.PERIOD_AI_TURNS,
                event_type=UsageEventType.AI_TURN,
                meta={"conversation_id": str(job.conversation_id)},
            )
            if not allowance.allowed:
                return _Reservation.REFUSED
            if job.trigger_message_id is None:
                return _Reservation.RESERVED
            engaged = await AgentTurnRepository(reservation, tenant_id=job.tenant_id).engage(
                trigger_message_id=job.trigger_message_id
            )
            if not engaged:
                await reservation.rollback()
                logger.info(
                    "agent.turn_engaged_elsewhere",
                    extra={
                        "event": "agent.turn_engaged_elsewhere",
                        "conversation_id": str(job.conversation_id),
                        "trigger_message_id": str(job.trigger_message_id),
                    },
                )
                return _Reservation.LOST
            return _Reservation.RESERVED

    async def _quota_blocked(self, job: AgentJob) -> None:
        """Hand a turn the plan will not pay for to a person, and finish it.

        Not silent (AI-02). This used to return with nothing written: no reply,
        no handoff, no durable trace, the customer waiting on nobody. Now the
        conversation goes to the inbox with a reason a colleague can act on,
        and the analytics handoff records that the platform - not an agent, not
        a person - decided it.

        Not raised either (ADR-030): a workspace out of allowance has a billing
        problem and its customer has a question, and dead-lettering the job
        would lose the second over the first. The customer is not told about
        the business's plan.

        A colleague who took the conversation over since the turn was planned
        keeps their own reason; only an AI-owned conversation is handed over.
        """
        async with self._database.session() as blocked:
            conversation = await ConversationRepository(
                blocked, tenant_id=job.tenant_id
            ).require_by_id(job.conversation_id)
            if conversation.mode is ConversationMode.AI:
                await InboxService(session=blocked, tenant_id=job.tenant_id).set_mode(
                    conversation_id=job.conversation_id,
                    mode=ConversationMode.HUMAN,
                    handoff_reason=QUOTA_HANDOFF_REASON,
                    source=AnalyticsSource.SYSTEM,
                )
            if job.trigger_message_id is not None:
                await AgentTurnRepository(blocked, tenant_id=job.tenant_id).complete(
                    trigger_message_id=job.trigger_message_id
                )
        logger.warning(
            "billing.ai_allowance_exhausted",
            extra={
                "event": "billing.ai_allowance_exhausted",
                "tenant_id": str(job.tenant_id),
                "conversation_id": str(job.conversation_id),
            },
        )

    async def _complete_turn(self, job: AgentJob) -> None:
        """Record that the turn ran to its end.

        Every end counts: a reply sent, a handoff, a conversation a person
        owns, or nothing worth saying. All of them mean the customer's message
        has been dealt with, and a turn left `CLAIMED` after one of them would
        be a turn a later duplicate could adopt.
        """
        if job.trigger_message_id is None:
            return
        async with self._database.session() as marking:
            await AgentTurnRepository(marking, tenant_id=job.tenant_id).complete(
                trigger_message_id=job.trigger_message_id
            )

    def _reply_key(self, job: AgentJob) -> str | None:
        """The idempotency key for this turn's reply, if the turn has an identity.

        Defence in depth behind the claim above, using the machinery the manual
        send path already has (MSG-15): if anything ever produces two turns for
        one message again, `uq_messages_tenant_id_idempotency_key` still refuses
        to put a second copy on the customer's phone.
        """
        if job.trigger_message_id is None:
            return None
        return f"agent-turn:{job.trigger_message_id}"

    async def _handle(self, job: AgentJob, progress: _TurnProgress) -> None:
        async with self._database.session() as session:
            agent: Agent | None = None
            if job.agent_id is not None:
                agent = await AgentRepository(session, tenant_id=job.tenant_id).get_by_id(
                    job.agent_id
                )
                if agent is None:
                    # Retired, or from another workspace. The workspace default
                    # still applies, so the customer is not left unanswered.
                    logger.warning(
                        "agent.requested_agent_missing",
                        extra={"agent_id": str(job.agent_id)},
                    )

            conversations = ConversationRepository(session, tenant_id=job.tenant_id)
            # Asked here, before anything is claimed, charged or engaged, and
            # that placement is the whole of the fix for the enqueue-before-
            # commit race (ADR-089). The orchestrator refuses a missing
            # conversation identically - but only after the turn has engaged,
            # at which point the only honest policy is `NO_RETRY` and a
            # conversation that was merely mid-commit was dead-lettered on
            # attempt one. A read costs one indexed lookup and is safe to
            # repeat, so it belongs on the retryable side of the line.
            await conversations.require_by_id(job.conversation_id)

            # The business-level half of the question the queue barrier asks
            # about the envelope: is this turn already somebody else's? Two
            # envelopes naming one inbound message are one turn, and this is
            # where the second finds that out - before the charge, the
            # sentiment call, the inference or any tool, because a key on the
            # send alone would stop the second reply and still bill the
            # workspace for the second turn that produced it (WQ-01).
            if not await self._claim_turn(job):
                return

            # Before the charge, not after it: a conversation a person owns or
            # a workspace with no agent allowed to answer is a turn the AI will
            # not take, and it costs the customer nothing (AI-02).
            plan = await plan_turn(
                conversations=conversations,
                agents=AgentRepository(session, tenant_id=job.tenant_id),
                conversation_id=job.conversation_id,
                agent=agent,
            )
            if plan.refusal is not None:
                await self._complete_turn(job)
                return

            # The reservation takes a session of its own. Handing this one's
            # connection back first means the turn never needs two at once from
            # a pool that may only have one (ADR-080).
            await session.commit()
            reservation = await self._reserve_turn(job)
            if reservation is _Reservation.LOST:
                return
            if reservation is _Reservation.REFUSED:
                await self._quota_blocked(job)
                return

            # Past this line the turn is charged and engaged: it can call the
            # provider, run tools and send a customer a message, none of which
            # a second attempt could tell had already happened. Awaited rather
            # than assigned because it also persists the fact on the queue, so
            # a worker that dies after this point is not mistaken for one that
            # died before it.
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
                    meter_round=self._round_meter(job),
                    output_ceiling=self._settings.openai_max_output_tokens,
                    embeddings=embeddings,
                    sentiment=sentiment,
                )
                outcome = await orchestrator.answer(
                    conversation_id=job.conversation_id,
                    agent=plan.agent,
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
                # per-round meter before each provider call, so counting them
                # again here would record every call twice. Tokens are only
                # known after the call, so they are recorded here.
                requests=0,
                # From the outcome, not from `agent`: a job naming no agent
                # is answered by the workspace default, and that is the
                # model the tokens were spent on.
                model=outcome.model,
                conversation_id=job.conversation_id,
                purpose=AI_PURPOSE_AGENT,
            )
            # Committed now, before anything about the reply can fail (AI-05).
            # The tokens were spent whatever becomes of the send, and staging
            # them into the send's own transaction meant a send refused before
            # it began - an over-long body, once - rolled them back unmetered.
            await session.commit()

            reply = outcome.reply
            if outcome.handed_off or not reply:
                # Silence is a decision here, not a failure: a handoff, an
                # escalation, a conversation a human owns, or nothing worth
                # saying. The turn is still over, so it is still finished.
                await self._complete_turn(job)
                return

            messaging = MessagingService(
                session=session,
                settings=self._settings,
                tenant_id=job.tenant_id,
            )
            await messaging.send_text(
                conversation_id=job.conversation_id,
                # One message within WhatsApp's limit, shortened at a sentence
                # with an offer to continue if the model ran long (AI-05).
                body=prepare_channel_reply(reply).text,
                origin=MessageOrigin.AGENT,
                # Deterministic, and derived from the message being answered
                # rather than generated here, so the same turn produces the same
                # key however many times it is published (WQ-01).
                idempotency_key=self._reply_key(job),
            )
        await self._complete_turn(job)
