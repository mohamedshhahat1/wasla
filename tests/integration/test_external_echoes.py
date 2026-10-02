"""A person replying from the provider's own app stops the AI (OMNI-037).

Instagram and Messenger report every message the business sends as an echo
(`is_echo`), whether Wasla sent it or a colleague typed it in the Instagram app;
Coexistence does the same for the WhatsApp Business app (`smb_message_echoes`).
Every echo used to be stored as evidence and nothing else, so the transcript
showed half a conversation and the AI answered over the person.

The decision applied (brief section 8): an echo naming Wasla's own send is
confirmed and changes nothing; an unmatched echo is projected as an outbound
message with origin `external`, the conversation goes to a person (cancelling
the agent's nudges, and any queued or composing agent turn is suppressed by the
mode it reads), and no window is opened, extended or moved.

Driven through `ChannelIngestionService` on the synthetic channel - WhatsApp
Cloud API echoes of Business-app sends are Coexistence-only - and, for the race,
through a real `AgentWorker` turn.

Mutants this suite kills: M-O21 (an unmatched echo not projected, or the AI
turn not cancelled) and M-O22 (an echo of Wasla's own send projected as
external).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.db.models.agent_turn import TurnOutcome
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    Message,
    MessageDeliveryState,
    MessageDirection,
    MessageOrigin,
)
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind
from app.db.models.tenant import Tenant
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.services.channel_ingestion_service import (
    EXTERNAL_REPLY_REASON,
    ChannelIngestionService,
    IngestionOutcome,
)
from app.services.messaging_service import MessagingService
from app.workers.queue import AgentQueue
from tests.channel_fakes import SyntheticAdapter, synthetic_payload
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    _TakesTheHandoff,
    text_response,
)
from tests.integration.test_automation_disclosure import Page, _customer_writes, _page

pytestmark = pytest.mark.integration

CUSTOMER = "igsid-0echo"


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[Any] = []

    async def enqueue(self, job: Any) -> None:
        self.jobs.append(job)


async def _setup(
    session: AsyncSession, adapter: SyntheticAdapter, *, wrote: timedelta = timedelta(minutes=1)
) -> tuple[ChannelConnection, Conversation]:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Echoes {tag}", slug=f"echoes-{tag}")
    session.add(tenant)
    await session.flush()
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"ig-{tag}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=30),
    )
    session.add(connection)
    await session.flush()
    await _deliver(
        session,
        adapter,
        connection,
        {
            "type": "message",
            "id": f"m.{tag}",
            "from": CUSTOMER,
            "at": int((datetime.now(UTC) - wrote).timestamp()),
            "text": "price?",
        },
    )
    conversation = await session.scalar(
        select(Conversation).where(Conversation.account_id == connection.id)
    )
    assert conversation is not None
    return connection, conversation


async def _deliver(
    session: AsyncSession,
    adapter: SyntheticAdapter,
    connection: ChannelConnection,
    event: dict[str, Any],
    queue: RecordingQueue | None = None,
) -> IngestionOutcome:
    outcome = await ChannelIngestionService(
        session=session, adapter=cast(ChannelAdapter, adapter), queue=cast(AgentQueue, queue)
    ).ingest(adapter.parse(synthetic_payload(connection.external_account_id, event)))
    await session.flush()
    return outcome


def _echo(mid: str, text: str, at: datetime | None = None) -> dict[str, Any]:
    return {
        "type": "echo",
        "id": mid,
        "to": CUSTOMER,
        "at": int((at or datetime.now(UTC)).timestamp()),
        "text": text,
    }


async def _transcript(
    session: AsyncSession, conversation: Conversation
) -> list[tuple[str, str, str | None]]:
    rows = await session.scalars(
        select(Message).where(Message.conversation_id == conversation.id).order_by(Message.sequence)
    )
    return [(m.direction.value, m.origin.value, m.body) for m in rows]


async def test_an_echo_of_wasla_s_own_send_changes_nothing(
    db_session: AsyncSession, settings: Settings
) -> None:
    adapter = SyntheticAdapter()
    connection, conversation = await _setup(db_session, adapter)
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.INSTAGRAM: cast(ChannelAdapter, adapter),
        },
        unmetered=True,
    )
    sent = await MessagingService(
        session=db_session, settings=settings, tenant_id=conversation.tenant_id, channels=registry
    ).send_text(conversation_id=conversation.id, body="It is 500.", origin=MessageOrigin.AGENT)
    before = await _transcript(db_session, conversation)

    outcome = await _deliver(
        db_session, adapter, connection, _echo(str(sent.wa_message_id), "It is 500.")
    )

    assert (outcome.echoes, outcome.external_echoes) == (1, 0)
    assert await _transcript(db_session, conversation) == before
    await db_session.refresh(conversation)
    assert conversation.mode is ConversationMode.AI


async def test_an_echo_of_a_send_still_in_flight_is_wasla_s_too(
    db_session: AsyncSession,
) -> None:
    """The echo can arrive before the send's response names it."""
    adapter = SyntheticAdapter()
    connection, conversation = await _setup(db_session, adapter)
    db_session.add(
        Message(
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND,
            kind="text",
            status="pending",
            delivery_state=MessageDeliveryState.REQUESTED,
            body="It is 500.",
            origin=MessageOrigin.AGENT,
        )
    )
    await db_session.flush()

    outcome = await _deliver(
        db_session, adapter, connection, _echo("m.not-yet-named", "It is 500.")
    )

    assert outcome.external_echoes == 0
    await db_session.refresh(conversation)
    assert conversation.mode is ConversationMode.AI


async def test_a_reply_typed_in_the_providers_app_is_projected_and_hands_over(
    db_session: AsyncSession,
) -> None:
    adapter = SyntheticAdapter()
    queue = RecordingQueue()
    connection, conversation = await _setup(db_session, adapter)
    nudge = FollowUp(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        status=FollowUpStatus.PENDING,
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
        body="Any thoughts?",
        created_by_kind=ActorKind.AGENT,
    )
    db_session.add(nudge)
    await db_session.flush()

    outcome = await _deliver(
        db_session, adapter, connection, _echo("m.typed-in-the-app", "It is 500."), queue
    )

    assert (outcome.echoes, outcome.external_echoes) == (1, 1)
    assert await _transcript(db_session, conversation) == [
        ("inbound", "customer", "price?"),
        ("outbound", "external", "It is 500."),
    ]
    await db_session.refresh(conversation)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.handoff_reason == EXTERNAL_REPLY_REASON
    assert conversation.assigned_to_id is None
    await db_session.refresh(nudge)
    assert nudge.status is FollowUpStatus.CANCELLED
    assert queue.jobs == [], "an echo is never a turn to answer"


async def test_an_external_echo_never_opens_or_moves_the_window(db_session: AsyncSession) -> None:
    adapter = SyntheticAdapter()
    connection, conversation = await _setup(db_session, adapter, wrote=timedelta(days=9))
    await db_session.refresh(conversation)
    anchor = conversation.last_inbound_at

    await _deliver(db_session, adapter, connection, _echo("m.late-reply", "Sorry, just saw this"))

    await db_session.refresh(conversation)
    assert conversation.last_inbound_at == anchor
    assert not adapter.policy.standard_window_open(conversation, now=datetime.now(UTC))


async def test_a_replayed_echo_is_projected_once(db_session: AsyncSession) -> None:
    adapter = SyntheticAdapter()
    connection, conversation = await _setup(db_session, adapter)
    echo = _echo("m.replayed", "It is 500.")

    await _deliver(db_session, adapter, connection, echo)
    replay = await _deliver(db_session, adapter, connection, echo)

    assert replay.duplicates == 1
    external = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == conversation.id, Message.origin == MessageOrigin.EXTERNAL)
    )
    assert external == 1


async def test_an_echo_arriving_while_the_model_composes_stops_its_reply(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The engagement barrier: the turn engaged, then a person answered in the app."""
    page: Page = await _page(ai_turns)
    trigger = await _customer_writes(ai_turns, page, "price?")

    async def composing(_request: dict[str, Any]) -> dict[str, Any]:
        async with ai_turns.database.session() as session:
            await ChannelIngestionService(
                session=session,
                adapter=cast(ChannelAdapter, page.adapter),
                queue=cast(AgentQueue, _TakesTheHandoff()),
            ).ingest(
                page.adapter.parse(
                    synthetic_payload(
                        page.connection.external_account_id,
                        {
                            "type": "echo",
                            "id": f"m.app-{uuid.uuid4().hex}",
                            "to": "psid-0disclosure",
                            "at": int(datetime.now(UTC).timestamp()),
                            "text": "It is 500.",
                        },
                    )
                )
            )
            await session.commit()
        return text_response("Our price is 450.")

    ai_providers.agent = composing
    assert page.conversation_id is not None
    await ai_turns.enqueue(page.workspace, page.conversation_id, trigger)
    worker = ai_turns.worker()
    worker._channels = page.registry
    assert await worker.run_once(wait_seconds=1)

    assert ai_providers.inference == 1, "the turn really engaged the model"
    assert page.adapter.log.sent == [], "no reply over the person"
    assert await ai_turns.turn_outcomes(page.workspace.tenant_id) == [
        TurnOutcome.SUPPRESSED_HUMAN.value
    ]
    conversation = await ai_turns.conversation(page.conversation_id)
    assert conversation.mode is ConversationMode.HUMAN
