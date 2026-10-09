"""The policy's decision reaches the adapter; an agent never borrows a person's tag (OMNI-033).

Messenger needs `messaging_type` on every send and lets a *person* answer for seven
days after the 24-hour window under `MESSAGE_TAG` + `HUMAN_AGENT` - "required for
Instagram Messaging API" (Meta's Messenger Platform send API and Instagram
Messaging, read 2026-10-02; the final audit's F3/I5). The send seam used to carry
only a recipient and content, so an adapter could neither tag a person's late
reply nor refuse to tag an agent's.

Driven through `MessagingService._dispatch` against PostgreSQL on the synthetic
channel configured with Messenger's rules (`tests.channel_fakes.TaggedPolicy`).
The context asserted is the one the synthetic provider received.

Mutant this suite kills: M-O16 (an AI origin allowed `human_agent_tag`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.channels.policy import SendMechanism
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import Conversation, Message, MessageOrigin
from app.db.models.tenant import Tenant
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.schemas.conversation import ReplyPolicyRead
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.messaging_service import MessagingService
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

pytestmark = pytest.mark.integration


async def _conversation(
    session: AsyncSession, adapter: SyntheticAdapter, *, customer_wrote: timedelta
) -> Conversation:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Context {tag}", slug=f"context-{tag}")
    session.add(tenant)
    await session.flush()
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.MESSENGER,
        external_account_id=f"page-{tag}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=30),
    )
    session.add(connection)
    await session.flush()
    at = int((datetime.now(UTC) - customer_wrote).timestamp())
    await ChannelIngestionService(session=session, adapter=cast(ChannelAdapter, adapter)).ingest(
        adapter.parse(
            synthetic_payload(
                connection.external_account_id,
                {"type": "message", "id": f"m.{tag}", "from": "psid-ctx", "at": at, "text": "hi"},
            )
        )
    )
    await session.flush()
    conversation = await session.scalar(
        select(Conversation).where(Conversation.account_id == connection.id)
    )
    assert conversation is not None
    return conversation


@pytest.fixture
def adapter() -> SyntheticAdapter:
    return SyntheticAdapter(Channel.MESSENGER, tagged=True)


def _messaging(
    session: AsyncSession, settings: Settings, adapter: SyntheticAdapter, tenant_id: uuid.UUID
) -> MessagingService:
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.MESSENGER: cast(ChannelAdapter, adapter),
        },
        unmetered=True,
    )
    return MessagingService(
        session=session, settings=settings, tenant_id=tenant_id, channels=registry
    )


async def _outbound(session: AsyncSession, conversation: Conversation) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation.id, Message.direction == "outbound")
        )
        or 0
    )


async def test_inside_the_window_every_sender_uses_the_standard_mechanism(
    db_session: AsyncSession, settings: Settings, adapter: SyntheticAdapter
) -> None:
    conversation = await _conversation(db_session, adapter, customer_wrote=timedelta(hours=1))
    messaging = _messaging(db_session, settings, adapter, conversation.tenant_id)

    await messaging.send_text(
        conversation_id=conversation.id, body="hi", origin=MessageOrigin.AGENT
    )
    await messaging.send_text(
        conversation_id=conversation.id, body="hi", origin=MessageOrigin.HUMAN
    )

    assert [context.mechanism for context in adapter.log.contexts] == [
        SendMechanism.STANDARD_WINDOW,
        SendMechanism.STANDARD_WINDOW,
    ]
    assert [context.origin for context in adapter.log.contexts] == [
        MessageOrigin.AGENT,
        MessageOrigin.HUMAN,
    ]


async def test_on_day_three_a_person_replies_under_the_human_agent_tag(
    db_session: AsyncSession, settings: Settings, adapter: SyntheticAdapter
) -> None:
    conversation = await _conversation(db_session, adapter, customer_wrote=timedelta(days=3))
    messaging = _messaging(db_session, settings, adapter, conversation.tenant_id)

    await messaging.send_text(
        conversation_id=conversation.id, body="Sorry for the wait", origin=MessageOrigin.HUMAN
    )

    (context,) = adapter.log.contexts
    assert (context.origin, context.mechanism) == (
        MessageOrigin.HUMAN,
        SendMechanism.HUMAN_AGENT_TAG,
    )


@pytest.mark.parametrize(
    "origin", [MessageOrigin.AGENT, MessageOrigin.FOLLOW_UP, MessageOrigin.CAMPAIGN]
)
async def test_on_day_three_no_automated_sender_is_ever_tagged(
    db_session: AsyncSession, settings: Settings, adapter: SyntheticAdapter, origin: MessageOrigin
) -> None:
    conversation = await _conversation(db_session, adapter, customer_wrote=timedelta(days=3))
    messaging = _messaging(db_session, settings, adapter, conversation.tenant_id)

    with pytest.raises(ValidationError):
        await messaging.send_text(
            conversation_id=conversation.id, body="Still there?", origin=origin
        )

    assert adapter.log.contexts == []
    assert await _outbound(db_session, conversation) == 0


async def test_on_day_eight_even_a_person_is_refused(
    db_session: AsyncSession, settings: Settings, adapter: SyntheticAdapter
) -> None:
    conversation = await _conversation(db_session, adapter, customer_wrote=timedelta(days=8))
    messaging = _messaging(db_session, settings, adapter, conversation.tenant_id)

    with pytest.raises(ValidationError, match="tagged reply window has closed"):
        await messaging.send_text(
            conversation_id=conversation.id, body="hello", origin=MessageOrigin.HUMAN
        )
    assert adapter.log.contexts == []


async def test_the_reply_policy_answers_per_origin(
    db_session: AsyncSession, settings: Settings, adapter: SyntheticAdapter
) -> None:
    day_three = await _conversation(db_session, adapter, customer_wrote=timedelta(days=3))
    messaging = _messaging(db_session, settings, adapter, day_three.tenant_id)

    read = ReplyPolicyRead.from_policy(messaging.reply_policy(day_three))

    assert read.free_text_allowed is True
    assert read.free_text_mechanism is SendMechanism.HUMAN_AGENT_TAG
    assert read.agent_free_text_allowed is False
    assert read.out_of_window.value == "tag"
