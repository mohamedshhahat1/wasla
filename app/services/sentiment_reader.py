"""Working out how a customer's message reads.

Split from `SentimentService` for the same reason `MediaReader` is split from
`MediaService`: this makes one provider call and returns a reading, and that
owns the row, the rules and the escalation. Each is then testable without the
other - the prompt without a database, the rules without a provider.

Nothing here decides anything. Whether a reading is bad enough to act on is a
question about the workspace's configuration, and that belongs to the caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.db.models.sentiment import MAX_INTENT_LENGTH, SentimentLabel
from app.integrations.openai.client import ResponsesClient
from app.integrations.openai.types import StructuredFormat, TokenUsage, Turn

logger = get_logger(__name__)

# Suggested rather than enforced. A fixed enum would guarantee that every
# reading groups cleanly in a report, at the cost of forcing every genuinely new
# intent into "other" - and the intents a business has not thought of yet are
# the ones worth seeing. The list makes the common cases consistent; the escape
# hatch keeps the unusual ones legible.
COMMON_INTENTS: Final = (
    "greeting",
    "question",
    "pricing",
    "purchase",
    "booking",
    "delivery",
    "support",
    "complaint",
    "refund",
    "human_request",
    "other",
)

# Written to be read by code, so it says nothing about tone or politeness. The
# two failure modes it exists to prevent are stated outright: reading the mood
# of a story the customer is telling rather than the mood they are in, and
# treating a blunt language as an angry one.
ANALYSIS_INSTRUCTIONS: Final = """
You classify how a customer sounds in a message they sent a business over
WhatsApp. You are not replying to them and you are not helping them.

Judge only how the customer feels about this business right now. A customer
describing an angry neighbour, a difficult week or a broken product is not
themselves angry unless they are directing it at the business.

Use "angry" only for real hostility, demands for escalation, threats to leave,
accusations of being lied to or ignored, or repeated unanswered complaints.
Disappointment, impatience and blunt questions are "negative" or "neutral".

Customers write in many languages, including Egyptian Arabic and mixtures of
Arabic and English. Directness, short messages and missing pleasantries are
ordinary in some languages and are not evidence of anger. Judge the substance,
never the register.

score: -1 for as hostile as it gets, 0 for neutral, +1 for delighted.
intent: one short lowercase snake_case label for what the customer wants.
confidence: how sure you are of the label, from 0 to 1.
""".strip()

SENTIMENT_SCHEMA: Final = StructuredFormat(
    name="customer_sentiment",
    schema={
        "type": "object",
        "properties": {
            "sentiment": {
                "type": "string",
                "enum": [label.value for label in SentimentLabel],
            },
            "score": {"type": "number"},
            # Nullable rather than optional: strict mode requires every property
            # to be listed in `required`, so "no intent" is expressed as a value.
            "intent": {"type": ["string", "null"]},
            "confidence": {"type": "number"},
        },
        "required": ["sentiment", "score", "intent", "confidence"],
        "additionalProperties": False,
    },
)

# Enough for a reading, and short enough that the analysis cannot become the
# expensive call on the path. Nothing here needs prose.
MAX_ANALYSIS_TOKENS: Final = 120

# The message is bounded before it is sent. A customer can paste a contract into
# WhatsApp, and the mood of a message is legible from its opening far more
# cheaply than from all of it.
MAX_ANALYSED_CHARACTERS: Final = 4_000


@dataclass(frozen=True, slots=True)
class SentimentReading:
    """What one message was judged to be."""

    label: SentimentLabel
    score: float
    intent: str | None
    confidence: float
    model: str
    usage: TokenUsage


def _clamped(value: object, *, low: float, high: float, default: float) -> float:
    """Coerce a provider number into range.

    Out-of-range values are pulled to the bound rather than rejected. A score of
    1.4 is a model being emphatic, not a model being broken, and discarding the
    whole reading over it would lose a signal that was essentially correct.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return max(low, min(high, float(value)))


def _intent(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()[:MAX_INTENT_LENGTH]
    return cleaned or None


class SentimentAnalyzer:
    """Reads one message and reports how it sounds."""

    def __init__(self, *, responses: ResponsesClient, model: str) -> None:
        self._responses = responses
        self._model = model

    async def read(self, text: str) -> SentimentReading:
        """Judge one message. Raises if the provider cannot be understood.

        The caller decides what a failure means. Losing a reading must never
        cost a customer their reply, so nothing here is retried beyond what the
        client already does.
        """
        body = text.strip()[:MAX_ANALYSED_CHARACTERS]
        if not body:
            raise ExternalServiceError("There is nothing to analyse in this message.")

        reply = await self._responses.respond(
            model=self._model,
            instructions=ANALYSIS_INSTRUCTIONS,
            turns=[Turn(role="user", text=body)],
            # Classification, not writing. Sampling would make the same message
            # read differently on a retry, and a rule that fires intermittently
            # is worse than one that does not fire at all.
            temperature=0.0,
            max_output_tokens=MAX_ANALYSIS_TOKENS,
            response_format=SENTIMENT_SCHEMA,
        )
        return self._decode(reply.text, usage=reply.usage)

    def _decode(self, text: str | None, *, usage: TokenUsage) -> SentimentReading:
        if not text:
            raise ExternalServiceError("The AI provider returned no sentiment reading.")

        try:
            payload = json.loads(text)
        except ValueError as error:
            # Schema-constrained output should make this impossible. It is
            # caught anyway because "should be impossible" is not a guarantee
            # about somebody else's service.
            logger.warning("sentiment.unreadable_reading")
            raise ExternalServiceError("The sentiment reading could not be read.") from error

        if not isinstance(payload, dict):
            raise ExternalServiceError("The sentiment reading was not an object.")
        return self._reading(payload, usage=usage)

    def _reading(self, payload: dict[str, Any], *, usage: TokenUsage) -> SentimentReading:
        raw_label = payload.get("sentiment")
        try:
            label = SentimentLabel(str(raw_label))
        except ValueError as error:
            # A label outside the enum is not a mood this system can act on, and
            # guessing which one was meant is how an escalation rule fires on
            # something nobody chose.
            logger.warning("sentiment.unknown_label")
            raise ExternalServiceError("The sentiment reading was not a known label.") from error

        return SentimentReading(
            label=label,
            score=_clamped(payload.get("score"), low=-1.0, high=1.0, default=0.0),
            intent=_intent(payload.get("intent")),
            # Defaults to zero, which is below every escalation floor. An
            # unreported confidence must not be read as certainty.
            confidence=_clamped(payload.get("confidence"), low=0.0, high=1.0, default=0.0),
            model=self._model,
            usage=usage,
        )
