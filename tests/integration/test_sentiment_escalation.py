"""Sentiment and escalation against a real database.

The rules are covered against fakes elsewhere. What only PostgreSQL can prove is
here: that a reading is written once per message and the unique constraint says
so, that an escalation survives as committed state rather than as an in-memory
attribute, and that one workspace's readings are invisible to another.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import TenantIsolationError
from app.db.models.agent import Agent, AgentStatus
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.sentiment import (
    ConversationPriority,
    MessageSentiment,
    SentimentLabel,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.openai.types import TokenUsage
from app.repositories.sentiment_repository import SentimentRepository
from app.services.sentiment_reader import SentimentAnalyzer, SentimentReading
from app.services.sentiment_service import SentimentService
from tests.fakes import as_analyzer

pytestmark = pytest.mark.integration

EARLIER = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 22, 9, 5, tzinfo=UTC)


class StubAnalyzer:
    """Returns a fixed reading without touching a provider."""

    def __init__(
        self,
        label: SentimentLabel = SentimentLabel.ANGRY,
        *,
        confidence: float = 0.95,
        intent: str | None = "complaint",
    ) -> None:
        self._reading = SentimentReading(
            label=label,
            score=-0.95 if label is SentimentLabel.ANGRY else 0.0,
            intent=intent,
            confidence=confidence,
            model="gpt-4.1-mini",
            usage=TokenUsage(input_tokens=20, output_tokens=8, total_tokens=28),
        )
        self.reads = 0

    async def read(self, text: str) -> SentimentReading:
        self.reads += 1
        return self._reading


async def _conversation(session: AsyncSession, *, slug: str = "acme") -> tuple[Any, ...]:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id="201234567890")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
    )
    session.add(conversation)
    await session.flush()
    return tenant, conversation


async def _said(
    session: AsyncSession,
    *,
    tenant: Tenant,
    conversation: Conversation,
    body: str,
    wa_message_id: str = "wamid.1",
    at: datetime | None = None,
) -> Message:
    """Store one inbound message.

    `at` is set explicitly wherever the order of two messages is the point.
    PostgreSQL's `now()` is the transaction's start time, so rows inserted by
    one transaction share a `created_at` and the ordering falls back to the
    random UUID tie-break - which passes half the time and fails the other half.
    """
    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        wa_message_id=wa_message_id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.TEXT,
        status=MessageStatus.RECEIVED,
        body=body,
    )
    if at is not None:
        message.created_at = at
    session.add(message)
    await session.flush()
    return message


def _service(
    session: AsyncSession,
    tenant: Tenant,
    analyzer: SentimentAnalyzer | None = None,
) -> SentimentService:
    return SentimentService(session=session, tenant_id=tenant.id, analyzer=analyzer)


async def test_an_angry_message_escalates_and_is_recorded(db_session: AsyncSession) -> None:
    tenant, conversation = await _conversation(db_session)
    message = await _said(
        db_session,
        tenant=tenant,
        conversation=conversation,
        body="This is the third time I have asked and nobody has answered me.",
    )

    outcome = await _service(db_session, tenant, as_analyzer(StubAnalyzer())).assess(
        conversation_id=conversation.id,
        escalation_sentiment=SentimentLabel.ANGRY,
    )
    await db_session.flush()

    assert outcome.escalated is True
    await db_session.refresh(conversation)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.priority is ConversationPriority.URGENT
    assert conversation.sentiment is SentimentLabel.ANGRY
    assert conversation.intent == "complaint"

    reading = (
        await db_session.execute(
            select(MessageSentiment).where(MessageSentiment.message_id == message.id)
        )
    ).scalar_one()
    assert reading.label is SentimentLabel.ANGRY
    assert reading.escalated is True
    assert reading.model == "gpt-4.1-mini"


async def test_a_second_reading_on_one_message_is_refused_by_the_database(
    db_session: AsyncSession,
) -> None:
    """The idempotency key, enforced where it cannot be skipped."""
    tenant, conversation = await _conversation(db_session)
    message = await _said(db_session, tenant=tenant, conversation=conversation, body="hello")

    for _ in range(2):
        db_session.add(
            MessageSentiment(
                tenant_id=tenant.id,
                message_id=message.id,
                conversation_id=conversation.id,
                label=SentimentLabel.NEUTRAL,
                score=0.0,
                confidence=0.5,
                escalated=False,
            )
        )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_retried_assessment_does_not_pay_for_a_second_reading(
    db_session: AsyncSession,
) -> None:
    tenant, conversation = await _conversation(db_session)
    await _said(db_session, tenant=tenant, conversation=conversation, body="unacceptable")
    analyzer = StubAnalyzer()

    first = await _service(db_session, tenant, as_analyzer(analyzer)).assess(
        conversation_id=conversation.id,
        escalation_sentiment=SentimentLabel.ANGRY,
    )
    await db_session.flush()
    # The conversation is in human mode now, so the retry is put back into the
    # state the first attempt found - which is what a rolled-back turn leaves.
    conversation.mode = ConversationMode.AI
    await db_session.flush()

    second = await _service(db_session, tenant, as_analyzer(analyzer)).assess(
        conversation_id=conversation.id,
        escalation_sentiment=SentimentLabel.ANGRY,
    )

    assert first.analysed is True
    assert second.analysed is False
    assert analyzer.reads == 1
    # The earlier decision still stands.
    assert second.escalated is True


async def test_the_newest_message_is_the_one_judged(db_session: AsyncSession) -> None:
    tenant, conversation = await _conversation(db_session)
    await _said(
        db_session,
        tenant=tenant,
        conversation=conversation,
        body="hello",
        wa_message_id="wamid.1",
        at=EARLIER,
    )
    newest = await _said(
        db_session,
        tenant=tenant,
        conversation=conversation,
        body="I want to speak to a manager",
        wa_message_id="wamid.2",
        at=LATER,
    )

    await _service(db_session, tenant, as_analyzer(StubAnalyzer())).assess(
        conversation_id=conversation.id,
        escalation_sentiment=SentimentLabel.ANGRY,
    )
    await db_session.flush()

    readings = (await db_session.execute(select(MessageSentiment))).scalars().all()
    assert [reading.message_id for reading in readings] == [newest.id]


async def test_a_calm_message_leaves_the_conversation_with_the_agent(
    db_session: AsyncSession,
) -> None:
    tenant, conversation = await _conversation(db_session)
    await _said(db_session, tenant=tenant, conversation=conversation, body="what are your hours?")

    outcome = await _service(
        db_session,
        tenant,
        as_analyzer(StubAnalyzer(SentimentLabel.NEUTRAL, intent="question")),
    ).assess(conversation_id=conversation.id, escalation_sentiment=SentimentLabel.ANGRY)
    await db_session.flush()
    await db_session.refresh(conversation)

    assert outcome.escalated is False
    assert conversation.mode is ConversationMode.AI
    assert conversation.priority is ConversationPriority.NORMAL
    assert conversation.intent == "question"


async def test_priority_set_by_hand_is_not_raised_back_by_a_calm_message(
    db_session: AsyncSession,
) -> None:
    """Down is a person's decision; a later good reading must not undo it either way."""
    tenant, conversation = await _conversation(db_session)
    await _said(db_session, tenant=tenant, conversation=conversation, body="thanks, all sorted")
    conversation.priority = ConversationPriority.HIGH
    await db_session.flush()

    await _service(
        db_session,
        tenant,
        as_analyzer(StubAnalyzer(SentimentLabel.POSITIVE, intent="praise")),
    ).assess(conversation_id=conversation.id, escalation_sentiment=SentimentLabel.ANGRY)
    await db_session.flush()
    await db_session.refresh(conversation)

    assert conversation.priority is ConversationPriority.HIGH

    updated = await _service(db_session, tenant).set_priority(
        conversation_id=conversation.id,
        priority=ConversationPriority.NORMAL,
    )
    assert updated.priority is ConversationPriority.NORMAL


async def test_one_workspace_cannot_see_another_workspace_readings(
    db_session: AsyncSession,
) -> None:
    tenant, conversation = await _conversation(db_session, slug="acme")
    other, _ = await _conversation(db_session, slug="rival")
    message = await _said(db_session, tenant=tenant, conversation=conversation, body="furious")

    await _service(db_session, tenant, as_analyzer(StubAnalyzer())).assess(
        conversation_id=conversation.id,
        escalation_sentiment=SentimentLabel.ANGRY,
    )
    await db_session.flush()

    intruder = SentimentRepository(db_session, tenant_id=other.id)
    assert await intruder.get_for_message(message.id) is None
    assert await intruder.list_for_conversation(conversation.id) == []


async def test_one_workspace_cannot_assess_another_workspace_conversation(
    db_session: AsyncSession,
) -> None:
    tenant, conversation = await _conversation(db_session, slug="acme")
    other, _ = await _conversation(db_session, slug="rival")
    await _said(db_session, tenant=tenant, conversation=conversation, body="furious")

    with pytest.raises(TenantIsolationError):
        await _service(db_session, other, as_analyzer(StubAnalyzer())).assess(
            conversation_id=conversation.id,
            escalation_sentiment=SentimentLabel.ANGRY,
        )


async def test_an_existing_agent_escalates_by_default(db_session: AsyncSession) -> None:
    """Agents created before this phase are not silently opted out.

    The column carries a server default rather than a null, so a workspace that
    never touches its configuration still gets an angry customer in front of a
    person.
    """
    tenant, _ = await _conversation(db_session)
    agent = Agent(
        tenant_id=tenant.id,
        name="Sales",
        status=AgentStatus.ACTIVE,
        model="gpt-4.1-mini",
        system_prompt="Be helpful.",
    )
    db_session.add(agent)
    await db_session.flush()
    await db_session.refresh(agent)

    assert agent.escalation_sentiment is SentimentLabel.ANGRY
