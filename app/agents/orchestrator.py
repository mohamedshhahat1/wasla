"""Turning a conversation into an agent reply.

The orchestrator decides what to say; it does not say it. Sending belongs to the
messaging service, and keeping the WhatsApp client out of this loop is what
makes the loop testable with a mocked provider and no HTTP.

The transaction belongs to the caller. Tools mutate rows through their own
services and nothing here commits, which matches every other service in the
project and lets a worker decide whether a turn is kept or rolled back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.memory import build_window
from app.agents.registry import (
    HANDOFF_TOOL,
    ToolArgumentError,
    ToolContext,
    ToolRegistry,
    build_default_registry,
)
from app.core.exceptions import WaslaError
from app.core.logging import get_logger
from app.db.models.agent import Agent
from app.db.models.conversation import ConversationMode
from app.integrations.openai.client import ResponsesClient
from app.integrations.openai.embeddings import EmbeddingsClient
from app.integrations.openai.types import TokenUsage, ToolCall, ToolResult, Turn
from app.repositories.agent_repository import AgentRepository, AgentToolRepository
from app.repositories.conversation_repository import ConversationRepository, MessageRepository
from app.repositories.media_repository import MediaRepository
from app.services.sentiment_service import SentimentService

logger = get_logger(__name__)

MAX_ROUNDS: Final = 3
# Failed sends are filtered out of the window after loading, so fetching exactly
# the message limit could leave the window short.
HISTORY_MULTIPLIER: Final = 2


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

    @property
    def should_send(self) -> bool:
        return bool(self.reply) and not self.handed_off


def _nothing(
    *,
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
    )


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
        self._registry = registry if registry is not None else build_default_registry()
        self._max_rounds = max(1, max_rounds)
        self._agents = AgentRepository(session, tenant_id=tenant_id)
        self._grants = AgentToolRepository(session, tenant_id=tenant_id)
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._media = MediaRepository(session, tenant_id=tenant_id)

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
        conversation = await self._conversations.require_by_id(conversation_id)
        if conversation.mode is ConversationMode.HUMAN:
            # Checked here rather than in the worker so no caller can skip it.
            logger.info(
                "agent.skipped_human_mode",
                extra={"conversation_id": str(conversation_id)},
            )
            return _nothing()

        resolved = agent if agent is not None else await self._agents.get_answering_default()
        if resolved is None:
            logger.warning(
                "agent.no_active_default",
                extra={"tenant_id": str(self._tenant_id)},
            )
            return _nothing()
        if not resolved.is_answering:
            logger.info("agent.not_active", extra={"agent_id": str(resolved.id)})
            return _nothing(agent_id=resolved.id)

        if self._sentiment is not None:
            # Before a word is composed, not after. An escalation that arrives
            # second means the agent already answered an angry customer, which
            # is the thing this is here to prevent.
            mood = await self._sentiment.assess(
                conversation_id=conversation_id,
                escalation_sentiment=resolved.escalation_sentiment,
            )
            if mood.blocks_reply:
                logger.info(
                    "agent.escalated_before_reply",
                    extra={"conversation_id": str(conversation_id)},
                )
                return _nothing(agent_id=resolved.id, handed_off=True, escalated=True)

        history = await self._messages.list_for_conversation(
            conversation_id=conversation_id,
            limit=resolved.memory_message_limit * HISTORY_MULTIPLIER,
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
            return _nothing(agent_id=resolved.id)

        grants = await self._grants.list_for_agent(agent_id=resolved.id, enabled_only=True)
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

        for round_number in range(1, self._max_rounds + 1):
            rounds = round_number
            reply = await self._client.respond(
                model=resolved.model,
                instructions=resolved.system_prompt,
                turns=turns,
                tools=specs,
                tool_results=results,
                temperature=resolved.temperature,
                max_output_tokens=resolved.max_output_tokens,
            )
            input_tokens += reply.usage.input_tokens
            output_tokens += reply.usage.output_tokens
            total_tokens += reply.usage.total_tokens
            text = reply.text or text

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
            for call in reply.tool_calls:
                results.append(ToolResult.for_call(call, output=await self._run(call, context)))
                tools_run.append(call.name)
                if call.name == HANDOFF_TOOL:
                    handed_off = True

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
        )

    async def _run(self, call: ToolCall, context: ToolContext) -> str:
        """Run one call, turning refusals into output the model can learn from.

        Neither a rejected argument nor a failed operation should end the turn.
        The model is told what went wrong and gets a chance to adapt, which is
        the whole reason tool output exists.
        """
        try:
            return await self._registry.run(
                name=call.name,
                arguments=call.arguments,
                context=context,
            )
        except ToolArgumentError as error:
            logger.info("agent.tool_rejected", extra={"tool": call.name})
            return str(error)
        except WaslaError as error:
            logger.warning("agent.tool_failed", extra={"tool": call.name})
            return "That did not work: " + str(error)
