"""Handoffs, recorded where the domain forgets them.

`conversations.mode` answers "who has this now". These tests are about the
questions it cannot answer - when did it move, how often, and who decided - and
about the distinction the whole table exists for: an agent giving up, a
classifier judging a customer angry, and a colleague taking over are three
different facts that land in the same column.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.analytics import AnalyticsEventType, AnalyticsSource
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.sentiment import SentimentLabel
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.openai.types import TokenUsage
from app.repositories.analytics_repository import AnalyticsEventRepository
from app.services.inbox_service import InboxService
from app.services.sentiment_reader import SentimentReading
from app.services.sentiment_service import SentimentService
from tests.fakes import as_analyzer

pytestmark = pytest.mark.integration


class StubAnalyzer:
    """Reads every message as furious, without a provider."""

    def __init__(self, label: SentimentLabel) -> None:
        self._label = label

    async def read(self, text: str) -> SentimentReading:
        return SentimentReading(
            label=self._label,
            score=-0.95,
            intent="complaint",
            confidence=0.95,
            model="gpt-4.1-mini",
            usage=TokenUsage(input_tokens=20, output_tokens=8, total_tokens=28),
        )


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
    contact = Contact(tenant_id=tenant.id, wa_id=f"2010000{slug}")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        mode=ConversationMode.AI,
    )
    session.add(conversation)
    await session.flush()
    return tenant, conversation


async def _events(session: AsyncSession, tenant: Tenant) -> Sequence[Any]:
    return await AnalyticsEventRepository(session, tenant_id=tenant.id).counts()


async def test_a_colleague_taking_over_is_recorded_with_their_name(
    db_session: AsyncSession,
) -> None:
    tenant, conversation = await _conversation(db_session)
    user = User(email="rep@acme.test", hashed_password="x", full_name="Rep")
    db_session.add(user)
    await db_session.flush()

    inbox = InboxService(session=db_session, tenant_id=tenant.id)
    await inbox.set_mode(
        conversation_id=conversation.id,
        mode=ConversationMode.HUMAN,
        handoff_reason="Customer asked for a person.",
        actor_id=user.id,
    )
    await db_session.flush()

    events = await AnalyticsEventRepository(db_session, tenant_id=tenant.id).list_for_conversation(
        conversation.id
    )
    assert len(events) == 1
    assert events[0].event_type is AnalyticsEventType.HANDOFF
    assert events[0].source is AnalyticsSource.USER
    assert events[0].actor_id == user.id
    assert events[0].meta == {"reason": "Customer asked for a person."}


async def test_an_agent_giving_up_is_a_different_source(db_session: AsyncSession) -> None:
    tenant, conversation = await _conversation(db_session)
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    await inbox.set_mode(
        conversation_id=conversation.id,
        mode=ConversationMode.HUMAN,
        handoff_reason="I cannot help with refunds.",
        source=AnalyticsSource.AGENT,
    )
    await db_session.flush()

    counts = await _events(db_session, tenant)
    assert [(row.event_type, row.source, row.count) for row in counts] == [
        (AnalyticsEventType.HANDOFF, AnalyticsSource.AGENT, 1)
    ]


async def test_an_escalation_is_recorded_as_the_classifier_deciding(
    db_session: AsyncSession,
) -> None:
    """The count that says how often the product judged a customer angry."""
    tenant, conversation = await _conversation(db_session)
    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.TEXT,
        status=MessageStatus.RECEIVED,
        body="This is the third time I have asked.",
    )
    db_session.add(message)
    await db_session.flush()

    service = SentimentService(
        session=db_session,
        tenant_id=tenant.id,
        analyzer=as_analyzer(StubAnalyzer(SentimentLabel.ANGRY)),
    )
    outcome = await service.assess(
        conversation_id=conversation.id,
        escalation_sentiment=SentimentLabel.ANGRY,
    )
    await db_session.flush()

    assert outcome.escalated is True
    counts = await _events(db_session, tenant)
    assert [(row.event_type, row.source, row.count) for row in counts] == [
        (AnalyticsEventType.HANDOFF, AnalyticsSource.SENTIMENT, 1)
    ]


async def test_setting_a_mode_it_already_has_is_not_a_second_handoff(
    db_session: AsyncSession,
) -> None:
    """Editing the reason on a conversation a colleague already owns is not a
    handoff, and counting it would inflate the one number this table reports."""
    tenant, conversation = await _conversation(db_session)
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    await inbox.set_mode(conversation_id=conversation.id, mode=ConversationMode.HUMAN)
    await inbox.set_mode(
        conversation_id=conversation.id,
        mode=ConversationMode.HUMAN,
        handoff_reason="Actually, a billing question.",
    )
    await db_session.flush()

    counts = await _events(db_session, tenant)
    assert [row.count for row in counts] == [1]


async def test_giving_a_conversation_back_is_its_own_event(db_session: AsyncSession) -> None:
    tenant, conversation = await _conversation(db_session)
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    await inbox.set_mode(conversation_id=conversation.id, mode=ConversationMode.HUMAN)
    await inbox.set_mode(conversation_id=conversation.id, mode=ConversationMode.AI)
    await db_session.flush()

    counts = {row.event_type: row.count for row in await _events(db_session, tenant)}
    assert counts == {
        AnalyticsEventType.HANDOFF: 1,
        AnalyticsEventType.HANDOFF_RESUMED: 1,
    }


async def test_a_conversation_handed_over_twice_is_one_conversation(
    db_session: AsyncSession,
) -> None:
    """Events and conversations are different counts, and both are wanted: a
    resolution rate built on events would punish the same conversation twice."""
    tenant, conversation = await _conversation(db_session)
    inbox = InboxService(session=db_session, tenant_id=tenant.id)

    for _ in range(2):
        await inbox.set_mode(conversation_id=conversation.id, mode=ConversationMode.HUMAN)
        await inbox.set_mode(conversation_id=conversation.id, mode=ConversationMode.AI)
    await db_session.flush()

    repository = AnalyticsEventRepository(db_session, tenant_id=tenant.id)
    touched = await repository.conversations_touched(event_type=AnalyticsEventType.HANDOFF)
    counts = {row.event_type: row.count for row in await repository.counts()}
    assert touched == 1
    assert counts[AnalyticsEventType.HANDOFF] == 2


async def test_one_workspace_cannot_see_anothers_handoffs(db_session: AsyncSession) -> None:
    acme, acme_conversation = await _conversation(db_session, slug="acme")
    rival, _ = await _conversation(db_session, slug="rival")

    await InboxService(session=db_session, tenant_id=acme.id).set_mode(
        conversation_id=acme_conversation.id,
        mode=ConversationMode.HUMAN,
    )
    await db_session.flush()

    assert await _events(db_session, rival) == []
    assert [row.count for row in await _events(db_session, acme)] == [1]


async def test_another_workspaces_conversation_cannot_be_handed_over(
    db_session: AsyncSession,
) -> None:
    """The isolation that matters most here: the id arrives in a request."""
    from app.core.exceptions import TenantIsolationError

    _, conversation = await _conversation(db_session, slug="acme")
    rival, _ = await _conversation(db_session, slug="rival")

    with pytest.raises(TenantIsolationError):
        await InboxService(session=db_session, tenant_id=rival.id).set_mode(
            conversation_id=conversation.id,
            mode=ConversationMode.HUMAN,
        )


async def test_an_event_belongs_to_the_transaction_that_caused_it(db_session: AsyncSession) -> None:
    """A handoff that rolled back did not happen."""
    tenant, conversation = await _conversation(db_session)
    # Held as values: a rollback expires the instances, and reading an
    # attribute off one afterwards would try to reload it outside the loop.
    tenant_id, conversation_id = tenant.id, conversation.id
    await db_session.commit()

    await InboxService(session=db_session, tenant_id=tenant_id).set_mode(
        conversation_id=conversation_id,
        mode=ConversationMode.HUMAN,
    )
    await db_session.flush()
    await db_session.rollback()

    assert await AnalyticsEventRepository(db_session, tenant_id=tenant_id).counts() == []


async def test_an_unknown_conversation_records_nothing(db_session: AsyncSession) -> None:
    tenant, _ = await _conversation(db_session)
    from app.core.exceptions import TenantIsolationError

    with pytest.raises(TenantIsolationError):
        await InboxService(session=db_session, tenant_id=tenant.id).set_mode(
            conversation_id=uuid.uuid4(),
            mode=ConversationMode.HUMAN,
        )
    assert await _events(db_session, tenant) == []
