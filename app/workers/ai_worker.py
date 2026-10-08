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
allowance is `PERIOD_AI_TURNS`, one per workspace whatever the channel
(ENT-01), and a turn is charged only for a usable outcome (ENT-02): it takes a
**hold** on one unit in the same transaction that engages it - so a duplicate
job that lost the claim holds nothing, and concurrent turns cannot all see "one
left" - and settles it once the provider has answered: one `AI_TURN` charge for
a reply or an executed handoff, the hold given back for an empty answer, an
escalation or a provider failure (ADR-131). Every provider call is still
recorded as `AI_REQUEST` with its tokens, because that is what the platform pays
for; it is cost accounting and nothing checks it against a limit.

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
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.disclosure import compose, disclosure_due, disclosure_for
from app.agents.lifecycle import refusal_now
from app.agents.orchestrator import AgentOrchestrator, AgentOutcome, plan_turn
from app.agents.registry import ToolRegistry
from app.agents.reply import fallback_reply, prepare_channel_reply
from app.channels.policy import ChannelCapabilities
from app.channels.registry import ChannelRegistry, default_registry
from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger
from app.core.redis import RedisClient
from app.core.telemetry import (
    record_agent_turn_outcome,
    record_ai_turn_charge,
    record_entitlement_refusal,
)
from app.core.tracing import JOB_OUTCOME
from app.db.errors import is_retryable, sqlstate
from app.db.models.agent import Agent
from app.db.models.agent_turn import AITurnReleaseReason, TurnOutcome
from app.db.models.analytics import AnalyticsSource
from app.db.models.conversation import Message, MessageDeliveryState, MessageOrigin
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.db.models.tenant import Tenant
from app.db.session import Database
from app.integrations.openai.client import ResponsesClient, build_http_client
from app.integrations.openai.embeddings import EMBED_QUERY, EmbeddingsClient
from app.repositories.agent_repository import AgentRepository
from app.repositories.agent_turn_repository import (
    AgentTurnRepository,
    TriggerNotAnswerableError,
)
from app.repositories.conversation_repository import ConversationRepository, MessageRepository
from app.services.ai_turn_charge import AITurnCharge, SettleResult, record_settlement
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


class UnidentifiedTurnError(Exception):
    """An agent job with no trigger message, which cannot be run at most once.

    Raised before anything is claimed, charged or called, and dead-lettered
    rather than retried: the identity a turn needs is the inbound message it
    answers, and a job that names none has no way to tell a redelivery from a
    second customer message (TOOL-17).
    """


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

# What a colleague reads when the model answered with no words at all. The
# customer has been told a person will follow up; this says why one must.
EMPTY_RESPONSE_HANDOFF_REASON: Final = (
    "AI_EMPTY_RESPONSE: the AI produced no reply, so the customer was told a colleague "
    "will follow up."
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
    """What taking a turn's hold on the allowance concluded."""

    #: Held, and the turn is engaged. The provider may now be called.
    RESERVED = "reserved"
    #: The plan has no turn left. Nothing was held and nothing engaged.
    REFUSED = "refused"
    #: The turn was no longer ours to engage, so it holds nothing. Somebody
    #: else is running it; this attempt does nothing.
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
        channels: ChannelRegistry | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._registry = registry
        # Whose policy bounds a reply: the conversation's channel's (OMNI-008).
        self._channels = channels or default_registry()
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
        except UnidentifiedTurnError:
            # Nothing has been charged, called or sent - the refusal happens
            # before the claim - and retrying would refuse identically for ever,
            # so this takes the same route as a malformed envelope (TOOL-17).
            outcome = await handle_failure(
                self._queue,
                raw,
                envelope,
                job_type=JOB_TYPE,
                identity=JobIdentity(tenant_id=job.tenant_id, job_id=job.conversation_id),
                category=FailureCategory.MALFORMED,
                policy=NO_RETRY,
            )
            return outcome.action
        except Exception as error:
            # The exception class, and not the exception. A database failure
            # formats its statement and - before `hide_parameters` - its bound
            # parameters into its own message, which is a customer's name, an
            # agent's handoff sentence or a follow-up body (TOOL-10). The
            # statement and the constraint name are what identify the bug, and
            # they are still in the driver's own log; what a job failure needs
            # here is which class it was and which conversation it was in.
            logger.error(
                "agent.job_failed",
                extra={
                    "event": "agent.job_failed",
                    "conversation_id": str(job.conversation_id),
                    "reason": type(error).__name__,
                },
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
        because the customer's allowance was one turn and it was held before
        the turn engaged. A transaction of its own because the turn's
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

    async def _claim_turn(self, job: AgentJob) -> uuid.UUID | None:
        """Take ownership of this logical turn, or report that somebody has it.

        Answers the turn's own id when this attempt owns it, because every tool
        the turn runs records which turn asked for it (TOOL-12) and the id is
        already in hand here.

        A transaction of its own, and committed here rather than with the rest
        of the turn: the turn's own session stays open across an inference, and
        a claim that commits only at the end of the turn is a claim that is
        invisible to the duplicate arriving while the inference runs.
        Committing first is also the safe direction - a crash between claiming
        and engaging leaves a `CLAIMED` row whose lease expires, and the next
        attempt adopts it.

        **A job carrying no trigger is refused** (TOOL-17, PD-TOOLS-09). It used
        to proceed, on the reasoning that answering a customer beats protecting
        them from a duplicate - but a job with no trigger message has no
        identity at all, so redelivery ran the inference again, executed the
        tools again and sent the customer a second reply; the same nudge was
        scheduled twice and audited twice. Every enqueue site in this build sets
        a trigger id, so the only jobs this refuses are ones an older build left
        in the queue across a deploy, and the honest thing to do with work whose
        effects cannot be bounded is to stop and say so rather than guess at an
        identity that changes on every redelivery.

        Draining the legacy queue is deployment work; see the remediation
        report's deployment backlog.
        """
        if job.trigger_message_id is None:
            logger.warning(
                "agent.turn_unkeyed",
                extra={
                    "event": "agent.turn_unkeyed",
                    "conversation_id": str(job.conversation_id),
                },
            )
            raise UnidentifiedTurnError(
                "This agent job carries no trigger message and cannot be run once."
            )

        async with self._database.session() as claim:
            turns = AgentTurnRepository(claim, tenant_id=job.tenant_id)
            try:
                owned = await turns.claim(
                    conversation_id=job.conversation_id,
                    trigger_message_id=job.trigger_message_id,
                    worker_id=self._queue.worker_id,
                )
            except TriggerNotAnswerableError:
                # Not a customer's message in this conversation: there is no
                # turn to run, now or on a retry, so the job ends here - with
                # nothing charged, engaged or sent (OMNI-005).
                return None
            turn_id = (
                await turns.id_for(trigger_message_id=job.trigger_message_id) if owned else None
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
        return turn_id

    async def _reserve_turn(self, job: AgentJob, trigger_message_id: uuid.UUID) -> _Reservation:
        """Hold one unit of the plan's AI allowance and engage the turn, as one transaction.

        One transaction because the two facts must not disagree (ENT-03). A hold
        without the engagement is allowance spoken for by a turn that will
        never run; an engagement without the hold is a turn a concurrent one
        could not see when it counted. Together, the point of no return and the
        hold are the same commit.

        The decision is taken under the workspace's `period_ai_turns` advisory
        lock, counting charges and open holds together, so N workers racing an
        allowance of N hold exactly N - and the lock is released when this
        short transaction ends, never held across an inference (ADR-080). A
        turn that turns out not to be ours to engage writes no hold: the
        conditional engage matches no row, and the transaction is rolled back.
        Nothing is charged here; the turn is charged when it settles.

        Contention outlasting even the hold's own lock wait - a burst of turns
        on one workspace - is retried rather than lost (ENT-03): nothing has
        engaged and nothing is held, so the claim is given back and the job
        fails as a dependency that is unavailable, which the queue retries
        before engagement. A turn refused for contention is not a customer's
        message to dead-letter.
        """
        try:
            return await self._hold_and_engage(job, trigger_message_id)
        except DBAPIError as error:
            if not is_retryable(error):
                raise
            async with self._database.session() as giving_back:
                await AgentTurnRepository(giving_back, tenant_id=job.tenant_id).release_claim(
                    trigger_message_id=trigger_message_id, worker_id=self._queue.worker_id
                )
            logger.warning(
                "agent.turn_hold_contended",
                extra={
                    "event": "agent.turn_hold_contended",
                    "conversation_id": str(job.conversation_id),
                    "sqlstate": sqlstate(error),
                },
            )
            raise DependencyUnavailableError(
                "The workspace's AI allowance is busy; the turn will be retried."
            ) from error

    async def _hold_and_engage(self, job: AgentJob, trigger_message_id: uuid.UUID) -> _Reservation:
        """The reservation itself: hold under the workspace's lock, then engage, one commit."""
        async with self._database.session() as reservation:
            entitlements = EntitlementService(
                reservation,
                tenant_id=job.tenant_id,
                default_plan_code=self._settings.default_plan_code,
                ai_turn_hold_ttl=timedelta(seconds=self._settings.ai_turn_hold_ttl_seconds),
            )
            allowance = await entitlements.hold_ai_turn()
            if not allowance.allowed:
                return _Reservation.REFUSED
            engaged = await AgentTurnRepository(reservation, tenant_id=job.tenant_id).engage(
                trigger_message_id=trigger_message_id, hold=True
            )
            if not engaged:
                await reservation.rollback()
                logger.info(
                    "agent.turn_engaged_elsewhere",
                    extra={
                        "event": "agent.turn_engaged_elsewhere",
                        "conversation_id": str(job.conversation_id),
                        "trigger_message_id": str(trigger_message_id),
                    },
                )
                return _Reservation.LOST
            return _Reservation.RESERVED

    async def _release_hold(self, job: AgentJob, turn_id: uuid.UUID) -> None:
        """Give a failed turn's hold back, in a transaction of its own (ENT-02).

        The turn raised before it produced an outcome - a provider error or
        timeout past its retries, or anything else on the way - so it is not
        charged. A transaction of its own, because the turn's session may be
        the thing that failed. If this fails too, the hold stops counting at
        its TTL and the billing sweep releases it.
        """
        try:
            async with self._database.session() as releasing:
                settled = await AITurnCharge(releasing, tenant_id=job.tenant_id).settle(
                    agent_turn_id=turn_id,
                    chargeable=False,
                    reason=AITurnReleaseReason.GENERATION_FAILED,
                )
            await record_settlement(settled)
        except Exception as error:
            logger.error(
                "agent.turn_hold_release_failed",
                extra={
                    "event": "agent.turn_hold_release_failed",
                    "conversation_id": str(job.conversation_id),
                    "reason": type(error).__name__,
                },
            )

    @staticmethod
    async def _settle_turn(
        session: AsyncSession, job: AgentJob, turn_id: uuid.UUID, outcome: AgentOutcome
    ) -> SettleResult:
        """Charge the turn for a usable outcome, or give its hold back (ENT-02).

        Staged on the turn's session and committed with its token meter,
        before anything about the reply can fail: a reply that was generated
        is charged whether or not it is then delivered, withheld by a re-read
        or refused by the channel - the generation happened.
        """
        return await AITurnCharge(session, tenant_id=job.tenant_id).settle(
            agent_turn_id=turn_id,
            chargeable=outcome.chargeable,
        )

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
        keeps their own reason; only an AI-owned conversation is handed over,
        and that is decided at the write rather than from an earlier read
        (CRM-02).
        """
        async with self._database.session() as blocked:
            await InboxService(session=blocked, tenant_id=job.tenant_id).hand_off(
                conversation_id=job.conversation_id,
                reason=QUOTA_HANDOFF_REASON,
                source=AnalyticsSource.SYSTEM,
            )
            if job.trigger_message_id is not None:
                await AgentTurnRepository(blocked, tenant_id=job.tenant_id).complete(
                    trigger_message_id=job.trigger_message_id,
                    outcome=TurnOutcome.QUOTA_BLOCKED,
                )
        logger.warning(
            "billing.ai_allowance_exhausted",
            extra={
                "event": "billing.ai_allowance_exhausted",
                "tenant_id": str(job.tenant_id),
                "conversation_id": str(job.conversation_id),
            },
        )

    async def _complete_turn(
        self,
        job: AgentJob,
        outcome: TurnOutcome,
        *,
        response_id: str | None = None,
    ) -> None:
        """Record that the turn ran to its end, and which end it was.

        Every end counts: a reply sent, a handoff, a conversation a person
        owns, a workspace no longer served. All of them mean the customer's
        message has been dealt with, and a turn left `CLAIMED` after one of them
        would be a turn a later duplicate could adopt. None of them is recorded
        as an unexplained silence.
        """
        logger.info(
            "agent.turn_outcome",
            extra={
                "event": "agent.turn_outcome",
                "outcome": outcome.value,
                "conversation_id": str(job.conversation_id),
            },
        )
        # A series per ending, so a surge of quota refusals, suppressions or
        # empty answers is a graph and an alert rather than log lines (AI-09).
        await record_agent_turn_outcome(outcome.value)
        if job.trigger_message_id is None:
            return
        async with self._database.session() as marking:
            await AgentTurnRepository(marking, tenant_id=job.tenant_id).complete(
                trigger_message_id=job.trigger_message_id,
                outcome=outcome,
                provider_response_id=response_id,
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
            conversation = await conversations.require_by_id(job.conversation_id)

            # The business-level half of the question the queue barrier asks
            # about the envelope: is this turn already somebody else's? Two
            # envelopes naming one inbound message are one turn, and this is
            # where the second finds that out - before the charge, the
            # sentiment call, the inference or any tool, because a key on the
            # send alone would stop the second reply and still bill the
            # workspace for the second turn that produced it (WQ-01).
            turn_id = await self._claim_turn(job)
            if turn_id is None:
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
            if plan.agent is None or plan.refusal is not None:
                await self._complete_turn(job, plan.refusal or TurnOutcome.SUPPRESSED_AGENT)
                return

            # The workspace, the conversation's status and the number, read as
            # columns before anything is charged or called (AI-06). A suspended
            # workspace, or a deleted one for however long retention keeps its
            # data, gets no provider call, no tool and no message - nor does a
            # conversation on a channel the plan in force excludes (ENT-16).
            refusal = await refusal_now(
                session,
                tenant_id=job.tenant_id,
                conversation_id=job.conversation_id,
                agent_id=plan.agent.id,
                channels=self._channels,
                default_plan_code=self._settings.default_plan_code,
            )
            if refusal is not None:
                await self._complete_turn(job, refusal)
                return

            # The reservation takes a session of its own. Handing this one's
            # connection back first means the turn never needs two at once from
            # a pool that may only have one (ADR-080).
            await session.commit()
            if job.trigger_message_id is None:  # pragma: no cover - `_claim_turn` refused it
                raise UnidentifiedTurnError("This agent job carries no trigger message.")
            reservation = await self._reserve_turn(job, job.trigger_message_id)
            if reservation is _Reservation.LOST:
                return
            if reservation is _Reservation.REFUSED:
                await record_entitlement_refusal("period_ai_turns", "quota_exhausted")
                await self._quota_blocked(job)
                return
            await record_ai_turn_charge("held")

            # Past this line the turn holds its allowance and is engaged: it
            # can call the provider, run tools and send a customer a message,
            # none of which a second attempt could tell had already happened.
            # Awaited rather than assigned because it also persists the fact on
            # the queue, so a worker that dies after this point is not mistaken
            # for one that died before it.
            await progress.engage()
            try:
                outcome = await self._generate(session, job, plan.agent, turn_id)
            except Exception:
                # No outcome, so no charge: the hold goes back before the job
                # is dead-lettered (ENT-02).
                await self._release_hold(job, turn_id)
                raise

            # The charge, or the hold given back, commits with the tokens the
            # turn spent (ENT-02).
            settled = await self._settle_turn(session, job, turn_id, outcome)
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
            await record_settlement(settled)

            reply = outcome.reply
            ending = outcome.effective_outcome
            if not reply and ending is not TurnOutcome.EMPTY_RESPONSE:
                # Nothing to send - and never an unexplained nothing. A handoff,
                # an escalation or a colleague who took over mid-turn is each
                # recorded as what it was.
                await self._complete_turn(job, ending, response_id=outcome.response_id)
                return

            # Everything a reply depends on, read again now, as columns (AI-06,
            # AI-07). The provider was called with no transaction open, so the
            # workspace, the agent, the conversation and the number read before
            # it are precisely what could have moved underneath it. A number
            # disabled mid-turn is refused here too, rather than raised by the
            # send and left stranding the turn `ENGAGED`.
            refusal = await refusal_now(
                session,
                tenant_id=job.tenant_id,
                conversation_id=job.conversation_id,
                # The agent that answered, and the one planned if the outcome
                # does not say: a reply is never checked against no agent at all.
                agent_id=outcome.agent_id or plan.agent.id,
                channels=self._channels,
                default_plan_code=self._settings.default_plan_code,
            )
            if refusal is not None:
                logger.info(
                    "agent.reply_suppressed",
                    extra={
                        "event": "agent.reply_suppressed",
                        "outcome": refusal.value,
                        "conversation_id": str(job.conversation_id),
                    },
                )
                await self._complete_turn(job, refusal, response_id=outcome.response_id)
                return

            messaging = MessagingService(
                session=session,
                settings=self._settings,
                tenant_id=job.tenant_id,
                channels=self._channels,
            )
            if reply:
                capabilities = self._channels.policy_for(conversation.channel).capabilities
                body, disclosed = await self._with_disclosure(
                    session, job, reply=reply, capabilities=capabilities
                )
                sent = await messaging.send_text(
                    conversation_id=job.conversation_id,
                    # One message within the conversation's channel's limit, in
                    # its own unit, shortened at a sentence with an offer to
                    # continue if the model ran long (AI-05, OMNI-008) - the
                    # automation disclosure included, where it is due.
                    body=body,
                    origin=MessageOrigin.AGENT,
                    # Deterministic, and derived from the message being answered
                    # rather than generated here, so the same turn produces the
                    # same key however many times it is published (WQ-01).
                    idempotency_key=self._reply_key(job),
                )
                if disclosed:
                    await self._record_disclosure(session, job, sent)
                final = TurnOutcome.REPLIED
            else:
                await self._answer_emptiness(messaging, session, job)
                final = TurnOutcome.EMPTY_RESPONSE
        await self._complete_turn(job, final, response_id=outcome.response_id)

    async def _generate(
        self, session: AsyncSession, job: AgentJob, agent: Agent, turn_id: uuid.UUID
    ) -> AgentOutcome:
        """Run the turn against the provider: the classifier, then the agent's rounds.

        Everything that can raise for want of an outcome is in here, so the
        caller has one place to give the hold back from.
        """
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
                operation=EMBED_QUERY,
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
                # Which turn is asking, so every tool call it makes is
                # recoverable from the customer's message afterwards
                # (TOOL-12).
                agent_turn_id=turn_id,
                trigger_message_id=job.trigger_message_id,
                # A way to open a clean transaction if the turn's own is
                # lost, so a terminal tool outcome is still written down.
                unit_of_work=self._database.session,
                # The worker's own channels, so a turn is answered under
                # the registry that admitted it.
                channels=self._channels,
            )
            return await orchestrator.answer(conversation_id=job.conversation_id, agent=agent)

    async def _with_disclosure(
        self,
        session: AsyncSession,
        job: AgentJob,
        *,
        reply: str,
        capabilities: ChannelCapabilities,
    ) -> tuple[str, bool]:
        """The reply to send, and whether it opens with the automation disclosure (OMNI-041).

        Only on a channel whose policy requires it, and only when it is due -
        first AI reply, a long gap, or a hand-back from a colleague - read from
        the conversation's columns now, after the inference.
        """
        if not capabilities.disclosure_required:
            return prepare_channel_reply(reply, capabilities).text, False
        disclosed_at, resumed_at = await ConversationRepository(
            session, tenant_id=job.tenant_id
        ).disclosure_marks(job.conversation_id)
        gap = timedelta(hours=self._settings.automation_disclosure_gap_hours)
        if not disclosure_due(
            disclosed_at=disclosed_at, resumed_at=resumed_at, now=datetime.now(UTC), gap=gap
        ):
            return prepare_channel_reply(reply, capabilities).text, False
        tenant = await session.get(Tenant, job.tenant_id)
        wording = disclosure_for(reply, tenant.automation_disclosure if tenant else None)
        return compose(reply, wording, capabilities), True

    @staticmethod
    async def _record_disclosure(session: AsyncSession, job: AgentJob, sent: Message) -> None:
        """Record a disclosure only once its reply was delivered (OMNI-041).

        An undelivered reply - declined, or left uncertain - leaves the next one
        still owing the disclosure: the customer was not told.
        """
        if sent.delivery_state is not MessageDeliveryState.SENT:
            return
        await ConversationRepository(session, tenant_id=job.tenant_id).record_disclosure(
            job.conversation_id, at=sent.sent_at or datetime.now(UTC)
        )

    async def _answer_emptiness(
        self,
        messaging: MessagingService,
        session: AsyncSession,
        job: AgentJob,
    ) -> None:
        """A model that answered with nothing is not a turn that succeeded (PD-3).

        It used to complete as though it had: no reply, no handoff, no signal, a
        customer ignored and every alert green - a provider that began returning
        refusal parts instead of text would have silenced every customer on the
        platform at once. Now the customer is told, in their own language, that
        a colleague will follow up, and a colleague is actually asked to: the
        conversation is handed over with a reason that says the AI produced
        nothing. Same idempotency key as a reply would carry, so a repeated turn
        cannot send it twice.
        """
        latest = await MessageRepository(session, tenant_id=job.tenant_id).latest_inbound(
            job.conversation_id
        )
        await messaging.send_text(
            conversation_id=job.conversation_id,
            body=fallback_reply((latest.body if latest is not None else None) or ""),
            origin=MessageOrigin.AGENT,
            idempotency_key=self._reply_key(job),
        )
        # Conditional like every automated handoff (CRM-02): a colleague who
        # took the conversation over meanwhile keeps their reason and owns it.
        await InboxService(session=session, tenant_id=job.tenant_id).hand_off(
            conversation_id=job.conversation_id,
            reason=EMPTY_RESPONSE_HANDOFF_REASON,
            source=AnalyticsSource.SYSTEM,
        )
        logger.warning(
            "agent.empty_response",
            extra={
                "event": "agent.empty_response",
                "conversation_id": str(job.conversation_id),
            },
        )
