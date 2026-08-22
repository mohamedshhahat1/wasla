"""Deciding what a customer's mood means for the conversation.

Three things happen here, and only the first involves a provider:

1. The newest thing the customer said is read, once.
2. The reading is stored and the conversation's current state updated.
3. If the workspace's agent is configured to, the conversation is handed to a
   person and the agent stops replying.

Order matters more than any of them. This runs before the agent composes a
reply, not after, because an escalation that arrives second means the AI already
answered an angry customer - which is the failure the feature exists to prevent.

Losing a reading must never cost a customer their reply. Every provider failure
here is contained and logged, and the turn continues without a reading.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ExternalServiceError, RateLimitedError
from app.core.logging import get_logger
from app.db.models.conversation import Conversation, ConversationMode
from app.db.models.sentiment import (
    ConversationPriority,
    SentimentLabel,
    is_at_least,
    raised_priority,
)
from app.repositories.conversation_repository import ConversationRepository, MessageRepository
from app.repositories.media_repository import MediaRepository
from app.repositories.sentiment_repository import SentimentRepository
from app.services.sentiment_reader import SentimentAnalyzer, SentimentReading
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

# A model's self-reported confidence is weakly calibrated, so it is used as a
# floor and never as evidence. Below this, a reading still updates the current
# state - a flag on an inbox costs nothing if it is wrong - but it does not stop
# an agent from replying. Silencing an agent on a guess is the expensive
# mistake, because the customer is then waiting on a person who was never told.
MIN_ESCALATION_CONFIDENCE: Final = 0.6

# Conversation.handoff_reason is String(200).
MAX_HANDOFF_REASON_LENGTH: Final = 200


@dataclass(frozen=True, slots=True)
class SentimentOutcome:
    """What assessing one conversation concluded.

    `reading` is None whenever nothing was judged - no provider configured, no
    customer message, or a provider that could not be reached. None of those are
    failures the caller should act on, which is why they share one shape.
    """

    reading: SentimentReading | None = None
    escalated: bool = False
    # False when a stored reading was reused. Distinguishes "this message was
    # analysed just now" from "this message was analysed on an earlier attempt",
    # which is what stops a retried job paying for a second inference.
    analysed: bool = False

    @property
    def blocks_reply(self) -> bool:
        """Whether the agent must stay silent because a person now owns this."""
        return self.escalated


class SentimentService:
    """Reads the mood of one workspace's conversations and acts on it."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        analyzer: SentimentAnalyzer | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        # Optional for the same reason embeddings are: a deployment without a
        # provider must still answer customers, and a test exercising the rules
        # should not have to stand one up.
        self._analyzer = analyzer
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._media = MediaRepository(session, tenant_id=tenant_id)
        self._readings = SentimentRepository(session, tenant_id=tenant_id)
        self._usage = UsageRecorder(session, tenant_id=tenant_id)

    async def assess(
        self,
        *,
        conversation_id: uuid.UUID,
        escalation_sentiment: SentimentLabel | None,
    ) -> SentimentOutcome:
        """Judge the newest customer message and apply what it implies.

        `escalation_sentiment` is the agent's configured threshold, passed in
        rather than looked up: the agent that will answer is the agent whose
        rules apply, and only the caller knows which one that is. None disables
        automatic handoff while still taking the reading.
        """
        conversation = await self._conversations.require_by_id(conversation_id)
        if conversation.mode is ConversationMode.HUMAN:
            # A person already owns it. There is nothing left to escalate, and
            # paying for an inference to say so would be spending money on a
            # decision that has been made.
            return SentimentOutcome()

        message = await self._messages.latest_inbound(conversation_id)
        if message is None:
            return SentimentOutcome()

        stored = await self._readings.get_for_message(message.id)
        if stored is not None:
            # Already judged on an earlier attempt. Re-applying would be a no-op
            # at best, and at worst would re-escalate a conversation a colleague
            # had deliberately handed back to the AI without the customer having
            # said anything new.
            return SentimentOutcome(escalated=stored.escalated)

        if self._analyzer is None:
            return SentimentOutcome()

        text = await self._customer_words(message.id, body=message.body)
        if not text:
            # A photograph with no caption. What is in it is a description this
            # system wrote, not something the customer said, and reading a mood
            # off our own prose would be inventing a signal.
            return SentimentOutcome()

        try:
            reading = await self._analyzer.read(text)
        except (ExternalServiceError, RateLimitedError):
            # Contained deliberately. A reading is an enhancement; the reply is
            # the product, and losing the first must not cost the second.
            logger.warning(
                "sentiment.unavailable",
                extra={"conversation_id": str(conversation_id)},
            )
            return SentimentOutcome()

        # Metered here rather than by the caller, because the caller never sees
        # this call: an assessment is a provider request of its own, on a model
        # of its own, and folding it into the agent turn's figures would hide a
        # cost from the workspace paying it.
        self._usage.ai_request(
            input_tokens=reading.usage.input_tokens,
            output_tokens=reading.usage.output_tokens,
            model=reading.model,
            conversation_id=conversation_id,
        )

        escalated = self._should_escalate(reading, threshold=escalation_sentiment)
        self._apply(conversation, reading)
        if escalated:
            conversation.mode = ConversationMode.HUMAN
            conversation.handoff_reason = _reason(reading)

        await self._readings.record(
            message_id=message.id,
            conversation_id=conversation_id,
            label=reading.label,
            score=reading.score,
            intent=reading.intent,
            confidence=reading.confidence,
            escalated=escalated,
            model=reading.model,
        )

        logger.info(
            "sentiment.escalated" if escalated else "sentiment.recorded",
            extra={
                "conversation_id": str(conversation_id),
                "sentiment": reading.label.value,
                "priority": conversation.priority.value,
                "intent": reading.intent,
                "confidence": round(reading.confidence, 2),
                "escalated": escalated,
            },
        )
        return SentimentOutcome(reading=reading, escalated=escalated, analysed=True)

    async def set_priority(
        self,
        *,
        conversation_id: uuid.UUID,
        priority: ConversationPriority,
    ) -> Conversation:
        """Set the priority by hand.

        The only way it comes down. Automatic assessment raises priority and
        never lowers it, so returning a conversation to the ordinary queue is a
        decision a person makes once they have looked at it.
        """
        conversation = await self._conversations.require_by_id(conversation_id)
        conversation.priority = priority
        logger.info(
            "conversation.priority_changed",
            extra={"conversation_id": str(conversation_id), "priority": priority.value},
        )
        return conversation

    async def _customer_words(self, message_id: uuid.UUID, *, body: str | None) -> str:
        """What the customer actually said, transcribed speech included.

        A voice note is words the customer chose, so its transcript is analysed
        like any other message. A description of a photograph is not - that is
        this system's own prose, and judging a mood from it would be reading our
        inference back as evidence.
        """
        parts = [(body or "").strip()]
        media = await self._media.get_for_message(message_id)
        if media is not None and media.is_voice and media.transcript:
            parts.append(media.transcript.strip())
        return "\n".join(part for part in parts if part)

    def _should_escalate(
        self,
        reading: SentimentReading,
        *,
        threshold: SentimentLabel | None,
    ) -> bool:
        if threshold is None:
            return False
        if not is_at_least(reading.label, threshold):
            return False
        return reading.confidence >= MIN_ESCALATION_CONFIDENCE

    def _apply(self, conversation: Conversation, reading: SentimentReading) -> None:
        conversation.sentiment = reading.label
        conversation.sentiment_score = reading.score
        conversation.intent = reading.intent
        conversation.intent_confidence = reading.confidence
        conversation.priority = raised_priority(conversation.priority, reading.label)


def _reason(reading: SentimentReading) -> str:
    """One line for the colleague picking this up.

    It says the handoff was automatic. A person taking over a conversation needs
    to know whether a customer asked for them or a classifier decided, because
    those call for entirely different opening words.
    """
    detail = f" about {reading.intent}" if reading.intent else ""
    return f"Escalated automatically: the customer sounds {reading.label.value}{detail}."[
        :MAX_HANDOFF_REASON_LENGTH
    ]
