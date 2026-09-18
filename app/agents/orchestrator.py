"""Turning a conversation into an agent reply.

The orchestrator decides what to say; it does not say it. Sending belongs to the
messaging service, and keeping the WhatsApp client out of this loop is what
makes the loop testable with a mocked provider and no HTTP.

The transaction belongs to the caller. Tools mutate rows through their own
services and nothing here commits, which matches every other service in the
project and lets a worker decide whether a turn is kept or rolled back.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Awaitable, Callable, Sequence, Set
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from time import perf_counter
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.lifecycle import serving_state, tool_refusal_for
from app.agents.memory import build_window
from app.agents.registry import (
    HANDOFF_TOOL,
    ToolArgumentError,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    build_default_registry,
    validate_arguments,
)
from app.core.exceptions import WaslaError
from app.core.logging import get_logger
from app.core.telemetry import UNKNOWN_TOOL, record_tool_execution
from app.db.models.agent import DEFAULT_MAX_OUTPUT_TOKENS, Agent
from app.db.models.agent_turn import TurnOutcome
from app.db.models.conversation import Conversation, ConversationMode, Message, MessageDirection
from app.db.models.tool_execution import (
    ToolExecution as ToolExecutionRecord,
)
from app.db.models.tool_execution import (
    ToolExecutionReason,
    ToolExecutionState,
    meta_for,
)
from app.db.session import released
from app.integrations.openai.client import ResponsesClient
from app.integrations.openai.embeddings import EmbeddingsClient
from app.integrations.openai.types import TokenUsage, ToolCall, ToolResult, Turn
from app.repositories.agent_repository import AgentRepository, AgentToolRepository
from app.repositories.conversation_repository import ConversationRepository, MessageRepository
from app.repositories.media_repository import MediaRepository
from app.repositories.tool_execution_repository import (
    ToolExecutionRepository,
    terminal_values,
)
from app.services.messaging_service import WHATSAPP_TEXT_MAX_CHARS
from app.services.sentiment_service import SentimentService

logger = get_logger(__name__)

MAX_ROUNDS: Final = 3
# Failed sends are filtered out of the window after loading, so fetching exactly
# the message limit could leave the window short.
HISTORY_MULTIPLIER: Final = 2

# How many tool calls one model response may make, and how many one turn may
# make across all its rounds (TOOL-08, PD-TOOLS-03). Server constants, and that
# is the point: nothing in a provider response chooses them. Before this the
# executor ran whatever it was given - one scripted response of eighty calls
# executed all eighty, made forty embedding requests, wrote forty audit rows and
# grew the next request from 2.4 kB to 30 kB. The practical ceiling was the
# model's output-token budget and the 1 MiB body limit, neither of which is a
# decision anybody took.
#
# Eight and twelve, because the tools that exist are used one or two at a time
# and a legitimate turn has never needed more: search, record what was learned,
# schedule a nudge, hand over. A model asking for more than eight things at once
# has lost the thread, and the cost of being wrong is one refused call the model
# reads and can retry on the next round - against the cost of an unbounded fan
# out into somebody else's API.
MAX_TOOL_CALLS_PER_RESPONSE: Final = 8
MAX_TOOL_CALLS_PER_TURN: Final = 12

# What the model reads when a call was not run. Fixed sentences: they carry no
# database state, no exception text and no customer content, and they say enough
# for the model to do something else.
_OVER_BUDGET_OUTPUT: Final = (
    "That tool call was not run: this turn has made too many tool calls. "
    "Answer the customer with what you already know, or hand the conversation "
    "to a colleague."
)
_UNAVAILABLE_NOW_OUTPUT: Final = (
    "That tool is not available on this conversation right now. Do not retry it."
)
_ROUND_LIMIT_OUTPUT: Final = (
    "That tool call was not run: there is no round left to read its result. "
    "Answer the customer with what you already know."
)
_DUPLICATE_OUTPUT: Final = "That tool call was already made in this turn; it was not run again."
_AFTER_HANDOFF_OUTPUT: Final = (
    "This conversation has been handed to a colleague, so that call was not run. "
    "Do not reply further."
)
_FAILED_OUTPUT: Final = (
    "That tool did not work. Do not try it again; answer the customer with what "
    "you already know, or hand the conversation to a colleague."
)


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """What one agent turn concluded.

    `reply` is `None` whenever nothing should be sent, so a caller never has to
    work out whether silence was a decision or a failure.
    """

    reply: str | None
    handed_off: bool
    tools_run: tuple[str, ...]
    usage: TokenUsage
    rounds: int
    agent_id: uuid.UUID | None = None
    # A handoff the classifier decided rather than one the model asked for.
    # Both stop the reply; only this one happened before a word was composed.
    escalated: bool = False
    # The model that was actually called, which is not always the one the job
    # asked for: a job naming no agent is answered by the workspace default.
    # Carried out so usage can be attributed to the model that was billed.
    model: str | None = None
    # How the turn ended, named rather than left to be inferred from a null.
    # `REPLIED` is the default and is refined by `effective_outcome` for callers
    # that built an outcome without saying.
    outcome: TurnOutcome = TurnOutcome.REPLIED
    # The provider's id for the last response, for support correlation (AI-12).
    response_id: str | None = None

    @property
    def should_send(self) -> bool:
        return bool(self.reply) and not self.handed_off

    @property
    def effective_outcome(self) -> TurnOutcome:
        """The ending, never `REPLIED` for a turn that has nothing to send."""
        if self.outcome is not TurnOutcome.REPLIED:
            return self.outcome
        if self.handed_off:
            return TurnOutcome.HANDED_OFF
        if not self.reply:
            return TurnOutcome.EMPTY_RESPONSE if self.rounds > 0 else TurnOutcome.NOTHING_TO_ANSWER
        return TurnOutcome.REPLIED


@dataclass(slots=True)
class _CallBudget:
    """How many tool calls this turn has left, and this response (TOOL-08).

    **Every call the provider asked for is counted**, including the ones refused
    for a bad argument, denied for a missing grant or suppressed as a duplicate.
    Counting only the calls that ran would let a model spend the budget on
    malformed ones for free, which is the shape of the bypass the cap exists to
    close.

    Two numbers rather than one because they answer different questions: the
    per-response cap bounds one fan-out, the per-turn cap bounds a model that
    fans out modestly three rounds running.
    """

    per_response: int
    per_turn: int
    turn_used: int = 0
    response_used: int = 0

    def start_response(self) -> None:
        self.response_used = 0

    def take(self) -> ToolExecutionReason | None:
        """Charge one call, and say which cap it broke - or None if it fits."""
        self.turn_used += 1
        self.response_used += 1
        if self.turn_used > self.per_turn:
            return ToolExecutionReason.TURN_CALL_LIMIT
        if self.response_used > self.per_response:
            return ToolExecutionReason.RESPONSE_CALL_LIMIT
        return None


@dataclass(slots=True)
class _TurnTools:
    """The tool state of one turn: identity, budget and what has already run."""

    budget: _CallBudget
    agent_turn_id: uuid.UUID | None = None
    trigger_message_id: uuid.UUID | None = None
    #: Provider call ids already executed in this turn. The provider is not
    #: required to make these unique and has been observed repeating one inside
    #: a single response, so this is what stops a repeat becoming a second
    #: business effect (TOOL-08, PD-TOOLS-04). The unique index on
    #: `tool_executions` is the backstop behind it.
    seen_call_ids: set[str] = field(default_factory=set)
    #: Set once a handoff has succeeded in the response being executed. Every
    #: later call of that response is recorded and not run (PD-TOOLS-02).
    handed_over: bool = False
    #: Executions whose row could not be written. Kept so a record that was
    #: rolled back with its savepoint is not updated again against a row that no
    #: longer exists.
    unrecorded: set[uuid.UUID] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """What running one requested tool call actually did.

    `output` is what the model reads next round. `succeeded` is whether the
    server ran the handler to completion: the tool was granted, its arguments
    validated, and the handler returned. A tool name the model emitted is a
    request, never an event, and only this flag says the event happened.
    """

    output: str
    succeeded: bool


# What every agent is told about the channel it is answering on, appended to
# whatever the workspace wrote. Two reasons it is here rather than in the
# workspace's own prompt: a workspace cannot be relied on to know Meta's limit,
# and a workspace that deleted the sentence would get the failure back.
#
# Guidance, not a guarantee. A model asked for brevity usually obliges and
# sometimes does not, and tokens are not characters - a budget in tokens cannot
# bound a length in characters, least of all across languages. So this reduces
# how often a reply has to be shortened; `app.agents.reply` is what guarantees
# the reply that is sent fits (AI-05).
_CHANNEL_INSTRUCTIONS = (
    "\n\nYou are replying over WhatsApp. Keep every reply under "
    f"{WHATSAPP_TEXT_MAX_CHARS} characters - WhatsApp will not deliver a longer "
    "one, and it will not be split for you. Prefer several short paragraphs to "
    "one long message, and offer to go into detail rather than doing it "
    "unasked."
)


def _reply_instructions(system_prompt: str) -> str:
    """The workspace's prompt plus what it cannot be expected to know.

    Appended rather than prepended so the workspace's own instructions lead,
    and so an agent's personality is not introduced by a paragraph about
    provider limits.
    """
    return f"{system_prompt}{_CHANNEL_INSTRUCTIONS}"


def _nothing(
    *,
    outcome: TurnOutcome,
    agent_id: uuid.UUID | None = None,
    handed_off: bool = False,
    escalated: bool = False,
) -> AgentOutcome:
    return AgentOutcome(
        reply=None,
        handed_off=handed_off,
        tools_run=(),
        usage=TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0),
        rounds=0,
        agent_id=agent_id,
        escalated=escalated,
        outcome=outcome,
    )


def _sentiment_subject(history: Sequence[Message]) -> Message | None:
    """The message whose mood gates this turn: the newest customer message it reads.

    The rule, stated because two turns can share a conversation (AI-04). A
    single message is judged on itself. A burst delivered together and answered
    by one turn is judged on its last message, which is where a customer's mood
    has got to. Two independent turns each judge the newest customer message in
    the history *they* load - and while bursts are not coalesced (AI-08), two
    turns that both load a history ending in the same message both judge that
    message. That is why the reading is stored atomically and the second turn
    follows the first's decision rather than racing it.

    By `sequence`, so of messages that share a transaction timestamp this is
    the one sent last (AI-01).
    """
    inbound = [message for message in history if message.direction is MessageDirection.INBOUND]
    return max(inbound, key=lambda message: message.sequence) if inbound else None


@dataclass(frozen=True, slots=True)
class TurnPlan:
    """Which agent answers a turn, or the outcome that says why none will."""

    agent: Agent | None
    refusal: TurnOutcome | None = None


async def plan_turn(
    *,
    conversations: ConversationRepository,
    agents: AgentRepository,
    conversation_id: uuid.UUID,
    agent: Agent | None,
) -> TurnPlan:
    """Decide whether this turn may spend anything, and who answers it.

    Asked by the worker *before* a turn is charged or engaged (AI-02): a
    conversation a person owns, or a workspace with no agent allowed to answer,
    costs the customer no AI turn and the platform no provider call. Asked again
    by `AgentOrchestrator.answer`, so no caller can skip it.

    Only reads, so it is safe on either side of the engagement barrier and safe
    to repeat. It is not a promise about later: the mode is read again before a
    reply is offered, because an inference is long enough for it to change.
    """
    conversation = await conversations.require_by_id(conversation_id)
    if conversation.mode is ConversationMode.HUMAN:
        logger.info(
            "agent.skipped_human_mode",
            extra={"conversation_id": str(conversation_id)},
        )
        return TurnPlan(agent=None, refusal=TurnOutcome.SUPPRESSED_HUMAN)

    resolved = agent if agent is not None else await agents.get_answering_default()
    if resolved is None:
        logger.warning(
            "agent.no_active_default",
            extra={"conversation_id": str(conversation_id)},
        )
        return TurnPlan(agent=None, refusal=TurnOutcome.SUPPRESSED_AGENT)
    if not resolved.is_answering:
        logger.info("agent.not_active", extra={"agent_id": str(resolved.id)})
        return TurnPlan(agent=resolved, refusal=TurnOutcome.SUPPRESSED_AGENT)
    return TurnPlan(agent=resolved)


class AgentOrchestrator:
    """Runs one agent turn for one conversation in one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        client: ResponsesClient,
        registry: ToolRegistry | None = None,
        max_rounds: int = MAX_ROUNDS,
        embeddings: EmbeddingsClient | None = None,
        sentiment: SentimentService | None = None,
        meter_round: Callable[[str], Awaitable[None]] | None = None,
        output_ceiling: int = DEFAULT_MAX_OUTPUT_TOKENS,
        agent_turn_id: uuid.UUID | None = None,
        trigger_message_id: uuid.UUID | None = None,
        unit_of_work: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._client = client
        # Optional: an agent granted no knowledge tool never needs one, and a
        # deployment without an embedding provider should still answer.
        self._embeddings = embeddings
        # Optional in the same way, and for the same reason: no assessor means
        # no assessment, and a deployment without a provider still answers. The
        # worker always supplies one, which is the path customers arrive on.
        self._sentiment = sentiment
        # Called before each provider round, with the model about to be called,
        # to record the request it is about to make. Cost accounting and never a
        # refusal: the customer's allowance is one turn, reserved by the worker
        # before the turn engaged (AI-02), so nothing inside this loop can run
        # out of it. Optional so a unit test can drive the loop without a
        # database; the worker always supplies one.
        self._meter_round = meter_round
        # The deployment's per-call output ceiling (AI-05). Every request carries
        # the smaller of this and the agent's own figure, so an agent configured
        # before a deployment lowered its ceiling is still held to the new one.
        self._output_ceiling = max(1, output_ceiling)
        self._registry = registry if registry is not None else build_default_registry()
        self._max_rounds = max(1, max_rounds)
        self._agents = AgentRepository(session, tenant_id=tenant_id)
        self._grants = AgentToolRepository(session, tenant_id=tenant_id)
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._media = MediaRepository(session, tenant_id=tenant_id)
        self._executions = ToolExecutionRepository(session, tenant_id=tenant_id)
        # The turn this orchestrator is running, so every tool call it executes
        # is recoverable from the customer's message afterwards (TOOL-12). Both
        # are optional because a unit test may drive the loop with no turn row
        # at all; the worker always supplies them.
        self._tools = _TurnTools(
            budget=_CallBudget(
                per_response=MAX_TOOL_CALLS_PER_RESPONSE,
                per_turn=MAX_TOOL_CALLS_PER_TURN,
            ),
            agent_turn_id=agent_turn_id,
            trigger_message_id=trigger_message_id,
        )
        # A way to open a *clean* transaction, used only when the turn's own has
        # been lost and a terminal execution outcome still has to be written.
        # Optional for the same reason as the meter: a unit test without a
        # database drives the loop without one, and then a lost transaction
        # simply loses the record it could not write.
        self._unit_of_work = unit_of_work

    async def answer(
        self,
        *,
        conversation_id: uuid.UUID,
        agent: Agent | None = None,
    ) -> AgentOutcome:
        """Decide what this agent should reply, if anything.

        Returns an empty outcome rather than raising when there is nothing to
        do: no configured agent, a conversation a human owns, or no history are
        all ordinary states, not failures.
        """
        # Asked here as well as by the worker, so no caller can skip it.
        plan = await plan_turn(
            conversations=self._conversations,
            agents=self._agents,
            conversation_id=conversation_id,
            agent=agent,
        )
        if plan.agent is None or plan.refusal is not None:
            return _nothing(
                agent_id=plan.agent.id if plan.agent is not None else None,
                outcome=plan.refusal or TurnOutcome.SUPPRESSED_AGENT,
            )
        resolved = plan.agent

        # Read before the assessment, so the mood that gates this reply is taken
        # from exactly the history the reply answers (AI-04).
        history = await self._messages.list_for_conversation(
            conversation_id=conversation_id,
            limit=resolved.memory_message_limit * HISTORY_MULTIPLIER,
        )

        subject = _sentiment_subject(history)
        if self._sentiment is not None and subject is not None:
            # Before a word is composed, not after. An escalation that arrives
            # second means the agent already answered an angry customer, which
            # is the thing this is here to prevent.
            mood = await self._sentiment.assess(
                conversation_id=conversation_id,
                escalation_sentiment=resolved.escalation_sentiment,
                subject=subject,
            )
            if mood.blocks_reply:
                logger.info(
                    "agent.escalated_before_reply",
                    extra={"conversation_id": str(conversation_id)},
                )
                return _nothing(
                    agent_id=resolved.id,
                    handed_off=True,
                    escalated=True,
                    outcome=TurnOutcome.ESCALATED,
                )
        # Fetched for the whole window at once. What a customer attached is
        # part of what they said, and an agent answering a photograph with
        # "[image]" is the thing this phase exists to stop.
        attachments = await self._media.map_for_messages([message.id for message in history])
        window = build_window(
            history,
            message_limit=resolved.memory_message_limit,
            token_budget=resolved.memory_token_budget,
            media=attachments,
        )
        if window.is_empty:
            # Nothing was ever said, so there is nothing to answer.
            return _nothing(agent_id=resolved.id, outcome=TurnOutcome.NOTHING_TO_ANSWER)

        grants = await self._grants.list_for_agent(agent_id=resolved.id, enabled_only=True)
        # Kept as a set as well as a spec list, because these are two different
        # questions and only one of them was being asked. `specs` decides what
        # the model is *offered*; `granted` decides what it is allowed to
        # actually run. See `_run`.
        granted = {grant.name for grant in grants}
        specs = self._registry.specs(grant.name for grant in grants)

        turns = list(window.turns)
        results: list[ToolResult] = []
        tools_run: list[str] = []
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        handed_off = False
        pending = False
        text: str | None = None
        rounds = 0
        response_id: str | None = None

        for round_number in range(1, self._max_rounds + 1):
            # The turn's connection goes back to the pool here, and stays
            # there for both the reservation and the inference (ADR-080).
            #
            # The meter is inside the block, not before it, and that ordering is
            # the difference between working and deadlocking on a small pool:
            # `meter_round` takes a *second* session, so asking for it while
            # this one still holds a connection needs two at once from a pool
            # that may only have one.
            #
            # `released` commits, and at this line that is what should happen.
            # On the first round nothing is staged - the phase above only read,
            # and the sentiment assessment, the one thing before this that
            # writes, committed its own reading. On a later round what is
            # staged is the finished work of the previous round's tools, not a
            # half-written anything. A handoff is never caught mid-commit here,
            # because a handoff breaks the loop rather than taking a round.
            async with released(self._session):
                if self._meter_round is not None:
                    # Before the call, so a request that was made is never left
                    # unrecorded; a crash between the two records one that was
                    # not, which is the cheaper mistake to make in a cost ledger.
                    await self._meter_round(resolved.model)
                rounds = round_number
                reply = await self._client.respond(
                    model=resolved.model,
                    instructions=_reply_instructions(resolved.system_prompt),
                    turns=turns,
                    tools=specs,
                    tool_results=results,
                    temperature=resolved.temperature,
                    max_output_tokens=self._max_output_tokens(resolved),
                )
            input_tokens += reply.usage.input_tokens
            output_tokens += reply.usage.output_tokens
            total_tokens += reply.usage.total_tokens
            text = reply.text or text
            response_id = reply.response_id or response_id

            pending = reply.wants_tools
            if not pending:
                break

            if reply.text:
                # Tool results replay the call but not the words around it, so
                # anything it said has to be carried forward explicitly.
                turns.append(Turn(role="assistant", text=reply.text))

            context = ToolContext(
                tenant_id=self._tenant_id,
                conversation_id=conversation_id,
                session=self._session,
                embeddings=self._embeddings,
            )
            self._tools.budget.start_response()
            self._tools.handed_over = False
            for ordinal, call in enumerate(reply.tool_calls, start=1):
                execution = await self._execute(
                    call,
                    context,
                    granted,
                    agent=resolved,
                    round_number=round_number,
                    ordinal=ordinal,
                )
                results.append(ToolResult.for_call(call, output=execution.output))
                tools_run.append(call.name)
                if call.name == HANDOFF_TOOL and execution.succeeded:
                    # From what the server did, never from what the model named
                    # (AI-03). A refused, invalid or failed handoff changed
                    # nothing about who owns the conversation, so it must not
                    # silence the agent either: the model reads that the tool
                    # did not work and answers on the next round.
                    handed_off = True
                    # And the rest of *this response* stops here (PD-TOOLS-02).
                    # The loop used to run on, so a response of
                    # `[handoff, schedule_follow_up]` handed the conversation to
                    # a colleague - cancelling the nudges that existed - and then
                    # planted a fresh nudge on it. Recorded rather than silently
                    # dropped, so the trail shows what the model asked for.
                    self._tools.handed_over = True

            if handed_off:
                # The conversation belongs to a person now. Another round could
                # only produce a reply that must not be sent.
                break

        if pending and not handed_off:
            # It kept asking for tools until the budget ran out. Whatever text it
            # produced along the way is still worth sending.
            logger.warning(
                "agent.round_limit_reached",
                extra={"conversation_id": str(conversation_id), "rounds": rounds},
            )

        if text and not handed_off and await self._taken_over(conversation_id):
            # A colleague opened this conversation while the model was
            # composing. The reply is discarded rather than sent: it was
            # written for a conversation the AI owned, and the customer is now
            # talking to a person who has not seen it (ADR-080).
            #
            # Read again rather than trusted from the snapshot above. The mode
            # was checked at the top of this method, and between then and here
            # the provider was called with no transaction open - so the row
            # this decision depends on is precisely the one that could have
            # moved underneath it.
            logger.info(
                "agent.taken_over_during_turn",
                extra={
                    "event": "agent.taken_over_during_turn",
                    "conversation_id": str(conversation_id),
                    "rounds": rounds,
                },
            )
            return AgentOutcome(
                reply=None,
                handed_off=True,
                tools_run=tuple(tools_run),
                usage=TokenUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                ),
                rounds=rounds,
                agent_id=resolved.id,
                model=resolved.model,
                outcome=TurnOutcome.SUPPRESSED_HUMAN,
                response_id=response_id,
            )

        logger.info(
            "agent.turn_completed",
            extra={
                "conversation_id": str(conversation_id),
                "agent_id": str(resolved.id),
                "rounds": rounds,
                "tools_run": len(tools_run),
                "handed_off": handed_off,
                "estimated_context_tokens": window.estimated_tokens,
                "dropped_messages": window.dropped,
                "total_tokens": total_tokens,
            },
        )

        if handed_off:
            ending = TurnOutcome.HANDED_OFF
        elif text:
            ending = TurnOutcome.REPLIED
        else:
            # The provider answered, and said nothing that could be sent and did
            # nothing that could stand in for words. Named, never left as a null.
            ending = TurnOutcome.EMPTY_RESPONSE
        return AgentOutcome(
            reply=None if handed_off else text,
            handed_off=handed_off,
            tools_run=tuple(tools_run),
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            ),
            rounds=rounds,
            agent_id=resolved.id,
            model=resolved.model,
            outcome=ending,
            response_id=response_id,
        )

    def _max_output_tokens(self, agent: Agent) -> int:
        """The output ceiling this request carries, never absent (AI-05)."""
        configured = agent.max_output_tokens or self._output_ceiling
        return min(configured, self._output_ceiling)

    async def _taken_over(self, conversation_id: uuid.UUID) -> bool:
        """Whether a person has taken this conversation since the turn began.

        A scalar column read, not a repository fetch, and deliberately: the
        conversation is already in this session's identity map, and a `select`
        that returns the mapped object hands back the instance that is there
        with the attributes it was loaded with. Asking for the column value
        gets the row as it is now, which is the entire point of asking again.

        Still tenant-scoped. A conversation id from another workspace matches
        nothing and reads as "not taken over", which is the safe direction -
        the reply is refused a line later by the messaging service, which is
        scoped too.
        """
        mode = await self._session.scalar(
            select(Conversation.mode).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self._tenant_id,
            )
        )
        return mode is ConversationMode.HUMAN

    async def _run(
        self,
        call: ToolCall,
        context: ToolContext,
        granted: Set[str],
        *,
        agent: Agent | None = None,
        round_number: int = 1,
        ordinal: int = 1,
    ) -> str:
        """The output the model reads for one call; see `_execute`."""
        return (
            await self._execute(
                call,
                context,
                granted,
                agent=agent,
                round_number=round_number,
                ordinal=ordinal,
            )
        ).output

    async def _execute(
        self,
        call: ToolCall,
        context: ToolContext,
        granted: Set[str],
        *,
        agent: Agent | None = None,
        round_number: int = 1,
        ordinal: int = 1,
    ) -> ToolExecution:
        """Run one call, turning every refusal into output the model can learn from.

        Answers two things, because they are two facts: what the model should
        read, and whether the handler actually ran to completion. Only the
        second may drive a decision the application makes on the model's
        behalf (AI-03). Deciding from the output text instead would be string
        matching against sentences written for a model, and deciding from the
        call's name - which is what this replaced - let a refused tool silence
        an agent that had been granted nothing.

        Neither a rejected argument nor a failed operation should end the turn.
        The model is told what went wrong and gets a chance to adapt, which is
        the whole reason tool output exists.

        The gates, in order, and each one is a finding:

        1. **The call is recorded before anything is decided** (TOOL-12). A
           refusal and a success now leave the same evidence, so "no row" means
           "the provider never asked" rather than "something happened and
           nothing recorded it".
        2. **The budget is charged** (TOOL-08), for every call including the
           ones about to be refused, so malformed calls cannot buy a bypass.
        3. **A handoff already made in this response stops the rest of it**
           (PD-TOOLS-02).
        4. **A provider call id seen before in this turn is a duplicate**
           (PD-TOOLS-04), not a second execution.
        5. **The grant is re-read from the database** (TOOL-05, PD-TOOLS-07).
           Offering a tool and permitting a tool were previously the same act:
           only granted tools were described to the model, but `ToolRegistry.run`
           looks a name up in the whole deployment registry, so any tool the
           deployment implements would execute if the model named it - and the
           tool names are ordinary English in a conversation containing text a
           stranger wrote. The `granted` set closed that; what it did not close
           is revocation, because the set is a snapshot taken at turn start. An
           administrator withdrawing a capability now stops the calls of the
           turn already running.
        6. **The workspace, agent, conversation and number are re-read**
           (TOOL-03). Everything a *reply* depends on was already re-read before
           sending; nothing a *tool* depends on was re-read at all, so a
           workspace suspended or soft-deleted while the model was composing
           went on writing CRM records and scheduling customer messages. This is
           also what makes human ownership outrank a stale AI decision
           (PD-TOOLS-01) and what makes the freshness explicit rather than
           accidental (TOOL-21).
        7. **Side-effecting calls do not run on the final round**
           (PD-TOOLS-05, TOOL-19). Their result could not be read by any round,
           so the only thing they can produce is an effect the model never got
           to reason about. A handoff still runs: it *is* the ending.
        8. **Arguments are validated against published bounds** (TOOL-01).
        9. **The handler runs inside a savepoint**, so a failure loses the call
           rather than the turn, and **any** exception is contained - not only
           the two this method was originally written to expect.
        """
        started = perf_counter()
        record = self._executions.start(
            conversation_id=context.conversation_id,
            tool_name=call.name,
            round_number=round_number,
            call_ordinal=ordinal,
            agent_turn_id=self._tools.agent_turn_id,
            trigger_message_id=self._tools.trigger_message_id,
            agent_id=agent.id if agent is not None else None,
            provider_call_id=call.call_id or None,
            # Names, never values. Tool arguments are where a customer's name,
            # a handoff sentence and a follow-up body live (ADR-052).
            argument_fields=meta_for(call.arguments),
        )
        execution = await self._gated(
            call,
            context,
            granted,
            agent=agent,
            round_number=round_number,
            record=record,
        )
        await self._report(record, tool=call.name, seconds=perf_counter() - started)
        return execution

    async def _gated(
        self,
        call: ToolCall,
        context: ToolContext,
        granted: Set[str],
        *,
        agent: Agent | None,
        round_number: int,
        record: ToolExecutionRecord,
    ) -> ToolExecution:
        """Every check that can refuse a call, then the call. See `_execute`."""
        over = self._tools.budget.take()
        if over is not None:
            logger.warning(
                "agent.tool_budget_exhausted",
                extra={
                    "event": "agent.tool_budget_exhausted",
                    "tool": call.name,
                    "conversation_id": str(context.conversation_id),
                    "reason": over.value,
                },
            )
            return await self._refuse(record, over, _OVER_BUDGET_OUTPUT)

        if self._tools.handed_over:
            return await self._refuse(
                record,
                ToolExecutionReason.HANDOFF_COMPLETED,
                _AFTER_HANDOFF_OUTPUT,
            )

        if call.call_id:
            # Claimed here rather than after the handler returns, so a provider
            # call id is one logical execution whatever became of the first one
            # (PD-TOOLS-04). Claiming it only on success would let a call that
            # was refused come back under the same id and run.
            if call.call_id in self._tools.seen_call_ids:
                logger.info(
                    "agent.tool_call_duplicate",
                    extra={
                        "event": "agent.tool_call_duplicate",
                        "tool": call.name,
                        "conversation_id": str(context.conversation_id),
                    },
                )
                await self._settle(
                    record,
                    state=ToolExecutionState.DUPLICATE,
                    reason=ToolExecutionReason.DUPLICATE_CALL,
                )
                return ToolExecution(output=_DUPLICATE_OUTPUT, succeeded=False)
            self._tools.seen_call_ids.add(call.call_id)

        if call.name not in granted:
            return await self._deny(call, context, record, ToolExecutionReason.NOT_GRANTED)

        definition = self._registry.get(call.name)
        if definition is None:
            # A grant naming a tool this build does not implement. Grants
            # outlive code, so this is a deployment fact rather than an
            # authorization one, and the model is told plainly that the name
            # does not exist here.
            logger.warning(
                "agent.tool_not_implemented",
                extra={"event": "agent.tool_not_implemented", "tool": call.name},
            )
            return await self._refuse(
                record,
                ToolExecutionReason.TOOL_NOT_IMPLEMENTED,
                f"There is no tool named {call.name}.",
            )

        fresh = await self._grant_refusal(agent, call.name)
        if fresh is not None:
            return await self._deny(call, context, record, fresh)

        state = await serving_state(
            self._session,
            tenant_id=context.tenant_id,
            conversation_id=context.conversation_id,
            agent_id=agent.id if agent is not None else None,
        )
        blocked = tool_refusal_for(state)
        if blocked is not None:
            logger.info(
                "agent.tool_not_served",
                extra={
                    "event": "agent.tool_not_served",
                    "tool": call.name,
                    "conversation_id": str(context.conversation_id),
                    "reason": blocked.value,
                },
            )
            return await self._refuse(record, blocked, _UNAVAILABLE_NOW_OUTPUT)

        if round_number >= self._max_rounds and not definition.terminal:
            # No round left to read the result, so the only thing this call can
            # produce is an effect nothing reasoned about (PD-TOOLS-05). A
            # knowledge search would spend an embedding call for nobody; a lead
            # write and a follow-up would land in a turn the model never got to
            # think about.
            return await self._refuse(
                record,
                ToolExecutionReason.ROUND_LIMIT,
                _ROUND_LIMIT_OUTPUT,
            )

        try:
            arguments = validate_arguments(definition, call.arguments)
        except ToolArgumentError as error:
            logger.info("agent.tool_rejected", extra={"tool": call.name})
            return await self._refuse(record, error.reason, str(error))

        ToolExecutionRepository.authorize(record)
        return await self._invoke(definition, context, arguments, record=record)

    async def _grant_refusal(self, agent: Agent | None, name: str) -> ToolExecutionReason | None:
        """Whether this agent may run `name` **now**, read from the database.

        `populate_existing` so an administrator's change is seen rather than the
        grant row this session loaded when the turn began (PD-TOOLS-07). A turn
        start snapshot is enough to decide what to *offer*; it is not
        authorization, and an inference is long enough - up to three rounds of
        three attempts - for a capability to be withdrawn inside one.

        A turn with no agent at all cannot be checked against a grant and is
        refused, which is the safe direction: a tool running for nobody is a
        tool nobody authorised.
        """
        if agent is None:
            return ToolExecutionReason.NOT_GRANTED
        grant = await self._grants.get(agent_id=agent.id, name=name, populate_existing=True)
        if grant is None:
            return ToolExecutionReason.NOT_GRANTED
        if not grant.enabled:
            return ToolExecutionReason.TOOL_DISABLED
        return None

    async def _invoke(
        self,
        definition: ToolDefinition,
        context: ToolContext,
        arguments: dict[str, object],
        *,
        record: ToolExecutionRecord,
    ) -> ToolExecution:
        """Run the handler, containing whatever it does (TOOL-01, TOOL-02).

        **The execution record is written before the savepoint opens**, and the
        record is not touched again inside it. A savepoint rollback undoes every
        statement issued since the savepoint *and expires the objects those
        statements wrote*, so a record marked `started` inside the savepoint
        would come back expired - and the next plain attribute read of it would
        be a lazy load with no greenlet to run in, which is a second, worse way
        to lose the turn. Marked and flushed first, it is clean by the time the
        handler runs and survives whatever the handler does.

        **Every exception is contained, not only the two that were expected.**
        `ToolArgumentError` and `WaslaError` were handled and everything else
        escaped to the worker, which - past engagement - has no honest response
        but to give up. So a NUL in a handoff reason, a lone surrogate in a lead
        field, an out-of-range delay, or an ordinary concurrent duplicate ended
        the customer's turn: no reply, no handoff, no explanation, the
        conversation left in AI mode so nobody was asked to pick it up, and the
        earlier rounds' writes still committed. The model can cause those, so
        "unexpected" was never the right word for them.

        `asyncio.CancelledError` is a `BaseException` and is deliberately not
        caught: a worker shutting down is not a tool that failed.
        """
        ToolExecutionRepository.begin(record)
        await self._flush(record)
        try:
            if definition.releases_session:
                # A tool that hands the connection back cannot be wrapped in a
                # savepoint - releasing commits, which would close it (TOOL-09).
                # Such a tool contains its own database work in a nested
                # transaction of its own; `search_knowledge` always has.
                output = await definition.handler(context, arguments)
            else:
                async with self._session.begin_nested():
                    output = await definition.handler(context, arguments)
        except ToolArgumentError as error:
            # A handler may still reject what the declaration could not express.
            # Recorded as a *failure* rather than a rejection, and the
            # distinction is the one this vocabulary is built on: `rejected`
            # means the call never ran, and this one did.
            logger.info("agent.tool_rejected", extra={"tool": definition.name})
            await self._settle(
                record,
                state=ToolExecutionState.FAILED,
                reason=error.reason,
            )
            return ToolExecution(output=str(error), succeeded=False)
        except WaslaError as error:
            logger.warning("agent.tool_failed", extra={"tool": definition.name})
            await self._settle(
                record,
                state=ToolExecutionState.FAILED,
                reason=ToolExecutionReason.DOMAIN_ERROR,
            )
            return ToolExecution(output="That did not work: " + str(error), succeeded=False)
        except Exception as error:
            # Safe metadata only. The exception itself is not formatted into the
            # log: a driver error quotes its SQL and its bound parameters, which
            # is a customer's name, a handoff sentence or a follow-up body
            # (TOOL-10).
            logger.warning(
                "agent.tool_crashed",
                extra={
                    "event": "agent.tool_crashed",
                    "tool": definition.name,
                    "conversation_id": str(context.conversation_id),
                    "reason": type(error).__name__,
                },
            )
            await self._settle(
                record,
                state=ToolExecutionState.FAILED,
                reason=ToolExecutionReason.INTERNAL_ERROR,
            )
            # A fixed sentence: never the exception, which could carry SQL, a
            # provider body or the customer's own values back to the model.
            return ToolExecution(output=_FAILED_OUTPUT, succeeded=False)

        await self._settle(record, state=ToolExecutionState.SUCCEEDED, reason=None)
        return ToolExecution(output=output, succeeded=True)

    async def _deny(
        self,
        call: ToolCall,
        context: ToolContext,
        record: ToolExecutionRecord,
        reason: ToolExecutionReason,
    ) -> ToolExecution:
        """Refuse a call the agent is not authorised to make."""
        logger.warning(
            "agent.tool_not_granted",
            extra={
                "event": "agent.tool_not_granted",
                "tool": call.name,
                # The conversation, under its own name. This carried the
                # conversation id under the key `agent_id`, so the one signal
                # that a model tried to use a capability it does not have
                # pointed an investigator at an agent that does not exist
                # (TOOL-15).
                "conversation_id": str(context.conversation_id),
                "agent_id": str(record.agent_id) if record.agent_id else None,
                "tenant_id": str(context.tenant_id),
                "reason": reason.value,
            },
        )
        # Phrased for the model rather than for a log reader: it gets a chance
        # to answer without the tool instead of the turn collapsing. One
        # sentence for both refusals, because "you were never given this" and
        # "this was taken away" are the same instruction to the model and the
        # difference belongs in the record, not in the prompt.
        return await self._refuse(
            record,
            reason,
            f"The tool {call.name} is not available to this agent.",
        )

    async def _refuse(
        self,
        record: ToolExecutionRecord,
        reason: ToolExecutionReason,
        output: str,
    ) -> ToolExecution:
        await self._settle(record, state=ToolExecutionState.REJECTED, reason=reason)
        return ToolExecution(output=output, succeeded=False)

    async def _settle(
        self,
        record: ToolExecutionRecord,
        *,
        state: ToolExecutionState,
        reason: ToolExecutionReason | None,
    ) -> None:
        """Close the execution record out, however the turn's transaction is.

        The ordinary path stages the terminal state in the turn's own session,
        so a successful call's record commits with the mutation it describes -
        a row saying `succeeded` beside a rolled-back lead would be worse than
        no row at all.

        The fallback exists because the whole point of this table is answering
        questions after something went wrong, and "the transaction was broken,
        so nothing was written" is exactly the answer it must not give.
        """
        ToolExecutionRepository.settle(record, state=state, reason=reason)
        await self._flush(record)

    async def _flush(self, record: ToolExecutionRecord) -> None:
        """Write the record, without letting bookkeeping cost the turn its work.

        **Inside a savepoint of its own.** Writing a record must never be the
        thing that ends a customer's turn: a failure here rolls back the
        record's own statement and leaves everything the turn has finished
        exactly where it was. Without that, an unflushable record would abort
        the whole transaction - so the table built to explain failures would
        have become a new way to cause them.

        A record whose write failed is not written again. Its statements were
        undone with the savepoint, so a later update would address a row that
        does not exist; the execution is reported once as unrecorded and the
        turn carries on.
        """
        if record.id in self._tools.unrecorded:
            return
        try:
            async with self._session.begin_nested():
                await self._session.flush()
            return
        except Exception as error:
            self._tools.unrecorded.add(record.id)
            # Detached, not merely abandoned. A savepoint rollback puts the row
            # back among the session's pending objects, so the *next* flush -
            # the one that commits a tool's actual work - would reissue the
            # statement that just failed, outside any savepoint, and take the
            # turn's transaction with it.
            with contextlib.suppress(Exception):
                self._session.expunge(record)
            logger.warning(
                "agent.tool_execution_unrecorded",
                extra={
                    "event": "agent.tool_execution_unrecorded",
                    "tool": record.tool_name,
                    "reason": type(error).__name__,
                },
            )
        if record.is_terminal:
            await self._settle_out_of_band(
                record,
                state=record.state,
                reason=record.reason_code,
            )

    async def _settle_out_of_band(
        self,
        record: ToolExecutionRecord,
        *,
        state: ToolExecutionState,
        reason: ToolExecutionReason | None,
    ) -> None:
        """Write a terminal outcome through a transaction of its own.

        The last resort, reached when the turn's session could not hold the
        record even inside a savepoint. An insert rather than an update: the
        savepoint took the original row with it, so there is nothing to update,
        and a terminal outcome nobody can read is the one answer this table must
        never give.

        Failure here is logged and swallowed. A metric is an observation of the
        work and so is this record; neither may become a participant in it.
        """
        if self._unit_of_work is None:
            return
        try:
            async with self._unit_of_work() as clean:
                clean.add(
                    ToolExecutionRecord(
                        id=record.id,
                        tenant_id=self._tenant_id,
                        agent_turn_id=record.agent_turn_id,
                        trigger_message_id=record.trigger_message_id,
                        agent_id=record.agent_id,
                        conversation_id=record.conversation_id,
                        tool_name=record.tool_name,
                        provider_call_id=record.provider_call_id,
                        round_number=record.round_number,
                        call_ordinal=record.call_ordinal,
                        requested_at=record.requested_at,
                        authorized_at=record.authorized_at,
                        started_at=record.started_at,
                        argument_fields=record.argument_fields,
                        **terminal_values(state=state, reason=reason),
                    )
                )
        except Exception as error:
            logger.warning(
                "agent.tool_execution_unrecorded",
                extra={
                    "event": "agent.tool_execution_unrecorded",
                    "reason": type(error).__name__,
                },
            )

    async def _report(self, record: ToolExecutionRecord, *, tool: str, seconds: float) -> None:
        """Count one tool call, by tool and by closed outcome (TOOL-11).

        The tool label comes from the registry, never from the call: a name the
        model invented is counted as `unknown`, because the conversation
        contains text a stranger wrote and a label domain a stranger can extend
        is a cardinality leak.
        """
        await record_tool_execution(
            tool=tool if self._registry.knows(tool) else UNKNOWN_TOOL,
            outcome=_tool_outcome(record),
            duration_seconds=seconds,
        )


#: Reasons that mean "this agent was not allowed to call this tool", as opposed
#: to the many other ways a call can be refused. Separated because an operator
#: alerts on them differently: a denial spike is a configuration change or an
#: injection attempt, and a rejection spike is a model behaving badly.
_DENIAL_REASONS: Final = frozenset(
    {ToolExecutionReason.NOT_GRANTED, ToolExecutionReason.TOOL_DISABLED}
)


def _tool_outcome(record: ToolExecutionRecord) -> str:
    """The metric label for one finished execution, from its own state."""
    if record.state is ToolExecutionState.SUCCEEDED:
        return "succeeded"
    if record.state is ToolExecutionState.DUPLICATE:
        return "duplicate"
    if record.state is ToolExecutionState.FAILED:
        return "failed"
    if record.state is ToolExecutionState.AMBIGUOUS:
        return "ambiguous"
    if record.reason_code in _DENIAL_REASONS:
        return "denied"
    return "rejected"
