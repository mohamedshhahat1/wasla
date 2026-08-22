"""The rules that turn a reading into an action.

No database and no provider. What is asserted here is the decision: which
readings stop an agent replying, which merely raise a flag, and the several
cases in which nothing should be judged at all.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import ExternalServiceError, RateLimitedError
from app.db.models.analytics import AnalyticsSource
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.sentiment import ConversationPriority, MessageSentiment, SentimentLabel
from app.integrations.openai.types import TokenUsage
from app.services import sentiment_service as service_module
from app.services.sentiment_reader import SentimentReading
from app.services.sentiment_service import (
    MIN_ESCALATION_CONFIDENCE,
    SentimentService,
)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
MESSAGE = uuid.UUID("33333333-3333-3333-3333-333333333333")

# Distinguishes "use the default message" from "this conversation has none".
DEFAULT = object()


def _reading(
    label: SentimentLabel = SentimentLabel.ANGRY,
    *,
    score: float = -0.9,
    intent: str | None = "complaint",
    confidence: float = 0.9,
) -> SentimentReading:
    return SentimentReading(
        label=label,
        score=score,
        intent=intent,
        confidence=confidence,
        model="gpt-4.1-mini",
        usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
    )


class StubAnalyzer:
    """Returns a queued reading, or raises what it was given."""

    def __init__(self, reading=None, *, raises: Exception | None = None) -> None:
        self._reading = reading if reading is not None else _reading()
        self._raises = raises
        self.seen: list[str] = []

    async def read(self, text: str) -> SentimentReading:
        self.seen.append(text)
        if self._raises is not None:
            raise self._raises
        return self._reading


class FakeConversations:
    def __init__(self, conversation) -> None:
        self._conversation = conversation

    async def require_by_id(self, conversation_id):
        return self._conversation


class FakeMessages:
    def __init__(self, message) -> None:
        self._message = message

    async def latest_inbound(self, conversation_id):
        return self._message


class FakeMedia:
    def __init__(self, media=None) -> None:
        self._media = media

    async def get_for_message(self, message_id):
        return self._media


class FakeReadings:
    """Remembers what was stored, and what was already there."""

    def __init__(self, stored=None) -> None:
        self._stored = stored
        self.recorded: list[dict] = []

    async def get_for_message(self, message_id):
        return self._stored

    async def record(self, **fields):
        self.recorded.append(fields)
        return MessageSentiment(**fields, tenant_id=TENANT), True


class FakeUsage:
    """Remembers what was metered, without a session to stage it in."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def ai_request(self, **fields) -> None:
        self.requests.append(fields)


class FakeAnalytics:
    """Remembers the handoffs an escalation recorded."""

    def __init__(self) -> None:
        self.handoffs: list[dict] = []

    def handoff(self, **fields) -> None:
        self.handoffs.append(fields)


def _inbound(body: str | None = "this is unacceptable") -> Message:
    return Message(
        id=MESSAGE,
        direction=MessageDirection.INBOUND,
        status=MessageStatus.RECEIVED,
        kind=MessageKind.TEXT,
        body=body,
    )


def _voice_note(transcript: str | None, *, is_voice: bool = True) -> MessageMedia:
    return MessageMedia(
        tenant_id=TENANT,
        message_id=MESSAGE,
        conversation_id=CONVERSATION,
        status=MediaStatus.READY,
        byte_size=1,
        attempts=1,
        is_voice=is_voice,
        transcript=transcript,
    )


def _returns(instance):
    def build(*args: object, **kwargs: object):
        return instance

    return build


def _build(
    monkeypatch,
    *,
    analyzer=None,
    conversation=None,
    message=DEFAULT,
    media=None,
    stored=None,
    readings=None,
    usage=None,
    analytics=None,
) -> SentimentService:
    fakes = {
        "UsageRecorder": usage if usage is not None else FakeUsage(),
        "AnalyticsRecorder": analytics if analytics is not None else FakeAnalytics(),
        "ConversationRepository": FakeConversations(
            conversation
            if conversation is not None
            else Conversation(mode=ConversationMode.AI, priority=ConversationPriority.NORMAL)
        ),
        "MessageRepository": FakeMessages(_inbound() if message is DEFAULT else message),
        "MediaRepository": FakeMedia(media),
        "SentimentRepository": readings if readings is not None else FakeReadings(stored),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(service_module, name, _returns(fake))

    return SentimentService(session=None, tenant_id=TENANT, analyzer=analyzer)


async def test_an_angry_customer_is_handed_to_a_person(monkeypatch):
    conversation = Conversation(mode=ConversationMode.AI, priority=ConversationPriority.NORMAL)
    service = _build(monkeypatch, analyzer=StubAnalyzer(), conversation=conversation)

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.escalated is True
    assert outcome.blocks_reply is True
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.priority is ConversationPriority.URGENT
    assert conversation.handoff_reason is not None


async def test_the_reason_says_the_handoff_was_automatic(monkeypatch):
    """A colleague opens with different words depending on who decided."""
    conversation = Conversation(mode=ConversationMode.AI, priority=ConversationPriority.NORMAL)
    service = _build(monkeypatch, analyzer=StubAnalyzer(), conversation=conversation)

    await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert "automatically" in (conversation.handoff_reason or "")
    assert "angry" in (conversation.handoff_reason or "")


async def test_an_unhappy_customer_is_flagged_but_still_answered(monkeypatch):
    """The default threshold is anger, not disappointment."""
    conversation = Conversation(mode=ConversationMode.AI, priority=ConversationPriority.NORMAL)
    service = _build(
        monkeypatch,
        analyzer=StubAnalyzer(_reading(SentimentLabel.NEGATIVE, score=-0.4)),
        conversation=conversation,
    )

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.escalated is False
    assert conversation.mode is ConversationMode.AI
    assert conversation.priority is ConversationPriority.HIGH


async def test_a_low_confidence_reading_never_silences_the_agent(monkeypatch):
    """Silencing an agent on a guess leaves the customer waiting on nobody."""
    conversation = Conversation(mode=ConversationMode.AI, priority=ConversationPriority.NORMAL)
    service = _build(
        monkeypatch,
        analyzer=StubAnalyzer(_reading(confidence=MIN_ESCALATION_CONFIDENCE - 0.01)),
        conversation=conversation,
    )

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.escalated is False
    assert conversation.mode is ConversationMode.AI
    # The flag still goes up. A wrong flag on an inbox costs nothing.
    assert conversation.priority is ConversationPriority.URGENT


async def test_a_workspace_can_switch_automatic_handoff_off(monkeypatch):
    conversation = Conversation(mode=ConversationMode.AI, priority=ConversationPriority.NORMAL)
    service = _build(monkeypatch, analyzer=StubAnalyzer(), conversation=conversation)

    outcome = await service.assess(conversation_id=CONVERSATION, escalation_sentiment=None)

    assert outcome.escalated is False
    assert conversation.mode is ConversationMode.AI
    # Still read, still flagged. Switching off the handoff is not switching off
    # the signal.
    assert conversation.sentiment is SentimentLabel.ANGRY
    assert conversation.priority is ConversationPriority.URGENT


async def test_a_workspace_can_escalate_earlier(monkeypatch):
    conversation = Conversation(mode=ConversationMode.AI, priority=ConversationPriority.NORMAL)
    service = _build(
        monkeypatch,
        analyzer=StubAnalyzer(_reading(SentimentLabel.NEGATIVE)),
        conversation=conversation,
    )

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.NEGATIVE,
    )

    assert outcome.escalated is True
    assert conversation.mode is ConversationMode.HUMAN


async def test_the_reading_is_stored_with_what_decided_it(monkeypatch):
    readings = FakeReadings()
    service = _build(monkeypatch, analyzer=StubAnalyzer(), readings=readings)

    await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    stored = readings.recorded[0]
    assert stored["label"] is SentimentLabel.ANGRY
    assert stored["escalated"] is True
    assert stored["intent"] == "complaint"
    assert stored["model"] == "gpt-4.1-mini"


async def test_a_conversation_a_person_already_owns_is_not_analysed(monkeypatch):
    """Nothing left to escalate, so nothing worth paying a provider for."""
    analyzer = StubAnalyzer()
    service = _build(
        monkeypatch,
        analyzer=analyzer,
        conversation=Conversation(mode=ConversationMode.HUMAN),
    )

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.reading is None
    assert analyzer.seen == []


async def test_a_message_already_read_is_not_read_again(monkeypatch):
    """What stops a retried job paying for a second inference."""
    analyzer = StubAnalyzer()
    stored = MessageSentiment(
        tenant_id=TENANT,
        message_id=MESSAGE,
        conversation_id=CONVERSATION,
        label=SentimentLabel.ANGRY,
        score=-1.0,
        confidence=0.9,
        escalated=True,
    )
    service = _build(monkeypatch, analyzer=analyzer, stored=stored)

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert analyzer.seen == []
    assert outcome.analysed is False
    # The earlier decision still stands, so the agent still stays quiet.
    assert outcome.escalated is True


async def test_a_conversation_handed_back_is_not_re_escalated(monkeypatch):
    """The case the stored reading exists to prevent.

    A colleague looked at an escalated conversation, decided it was fine and
    handed it back to the AI. Nothing new has been said. Judging the same
    message again would undo their decision within seconds.
    """
    conversation = Conversation(mode=ConversationMode.AI, priority=ConversationPriority.URGENT)
    stored = MessageSentiment(
        tenant_id=TENANT,
        message_id=MESSAGE,
        conversation_id=CONVERSATION,
        label=SentimentLabel.ANGRY,
        score=-1.0,
        confidence=0.9,
        escalated=False,
    )
    service = _build(monkeypatch, analyzer=StubAnalyzer(), conversation=conversation, stored=stored)

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.escalated is False
    assert conversation.mode is ConversationMode.AI


async def test_a_conversation_with_nothing_said_is_not_analysed(monkeypatch):
    analyzer = StubAnalyzer()
    service = _build(monkeypatch, analyzer=analyzer, message=None)

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.reading is None
    assert analyzer.seen == []


async def test_a_deployment_without_a_provider_still_answers(monkeypatch):
    service = _build(monkeypatch, analyzer=None)

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.reading is None
    assert outcome.blocks_reply is False


@pytest.mark.parametrize("failure", [ExternalServiceError("down"), RateLimitedError("slow down")])
async def test_a_provider_failure_does_not_cost_the_customer_a_reply(monkeypatch, failure):
    """A reading is an enhancement. The reply is the product."""
    conversation = Conversation(mode=ConversationMode.AI, priority=ConversationPriority.NORMAL)
    service = _build(
        monkeypatch,
        analyzer=StubAnalyzer(raises=failure),
        conversation=conversation,
    )

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.blocks_reply is False
    assert conversation.mode is ConversationMode.AI


async def test_a_voice_note_is_judged_on_what_was_said(monkeypatch):
    """A transcript is the customer's own words, so it is read like any other."""
    analyzer = StubAnalyzer()
    service = _build(
        monkeypatch,
        analyzer=analyzer,
        message=_inbound(body=None),
        media=_voice_note("you have wasted my entire morning"),
    )

    await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert analyzer.seen == ["you have wasted my entire morning"]


async def test_a_caption_and_a_voice_note_are_read_together(monkeypatch):
    analyzer = StubAnalyzer()
    service = _build(
        monkeypatch,
        analyzer=analyzer,
        message=_inbound("look at this"),
        media=_voice_note("this is the third time"),
    )

    await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert analyzer.seen == ["look at this\nthis is the third time"]


async def test_a_photograph_is_not_judged_from_our_own_description(monkeypatch):
    """The description is this system's prose, not something the customer said.

    Reading a mood off it would be treating our own inference as evidence.
    """
    analyzer = StubAnalyzer()
    service = _build(
        monkeypatch,
        analyzer=analyzer,
        message=_inbound(body=None),
        media=_voice_note("a photograph of a cracked screen", is_voice=False),
    )

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert analyzer.seen == []
    assert outcome.reading is None


async def test_priority_can_be_given_back_by_hand(monkeypatch):
    """The only way it comes down."""
    conversation = Conversation(mode=ConversationMode.AI, priority=ConversationPriority.URGENT)
    service = _build(monkeypatch, conversation=conversation)

    updated = await service.set_priority(
        conversation_id=CONVERSATION,
        priority=ConversationPriority.NORMAL,
    )

    assert updated.priority is ConversationPriority.NORMAL


async def test_an_assessment_is_metered_as_a_provider_call_of_its_own(monkeypatch):
    """The caller never sees this call, so the caller cannot count it.

    Folding it into the agent turn's figures would hide a cost from the
    workspace paying for it, on a model that is often not the agent's.
    """
    usage = FakeUsage()
    service = _build(monkeypatch, analyzer=StubAnalyzer(), usage=usage)

    await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert usage.requests == [
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "model": "gpt-4.1-mini",
            "conversation_id": CONVERSATION,
        }
    ]


async def test_a_provider_failure_is_not_metered(monkeypatch):
    """Nothing was read, so nothing was consumed."""
    usage = FakeUsage()
    service = _build(
        monkeypatch,
        analyzer=StubAnalyzer(raises=ExternalServiceError("the classifier is down")),
        usage=usage,
    )

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.analysed is False
    assert usage.requests == []


async def test_a_reading_already_taken_is_not_metered_again(monkeypatch):
    """A retried job must not pay twice for a message already read."""
    usage = FakeUsage()
    service = _build(
        monkeypatch,
        analyzer=StubAnalyzer(),
        stored=MessageSentiment(
            tenant_id=TENANT,
            message_id=MESSAGE,
            conversation_id=CONVERSATION,
            label=SentimentLabel.ANGRY,
            score=-0.9,
            confidence=0.9,
            escalated=True,
        ),
        usage=usage,
    )

    await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert usage.requests == []


async def test_an_escalation_records_the_classifier_as_the_one_who_decided(monkeypatch):
    """Three things can hand a conversation over, and the conversation row
    cannot tell them apart afterwards. This is the one that says the product
    judged a customer angry."""
    analytics = FakeAnalytics()
    service = _build(monkeypatch, analyzer=StubAnalyzer(), analytics=analytics)

    await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert len(analytics.handoffs) == 1
    assert analytics.handoffs[0]["source"] is AnalyticsSource.SENTIMENT
    assert analytics.handoffs[0]["conversation_id"] == CONVERSATION


async def test_a_reading_that_does_not_escalate_records_no_handoff(monkeypatch):
    analytics = FakeAnalytics()
    service = _build(
        monkeypatch,
        analyzer=StubAnalyzer(_reading(SentimentLabel.NEGATIVE)),
        analytics=analytics,
    )

    outcome = await service.assess(
        conversation_id=CONVERSATION,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert outcome.escalated is False
    assert analytics.handoffs == []
