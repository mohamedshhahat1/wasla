"""An AI on Messenger or Instagram says it is automated, when it must (OMNI-041).

Meta's Messenger Platform and Instagram Messaging policy: "Automated chat
experiences must disclose that a person is interacting with an automated
service" - at the start of a conversation, after significant lapses of time, and
when moving from a person back to automation (read 2026-10-02; the final audit's
F4).

Every case runs a real agent turn through `AgentWorker` - the orchestrator, the
fake model, the reply bounding, `MessagingService` - on the synthetic channel
configured with Messenger's rules, and reads the disclosure from **the bytes the
provider was handed**, not from a helper's return value.

Mutants this suite kills: M-O17 (no disclosure after a human-to-AI transition)
and M-O18 (`automation_disclosed_at` written at staging rather than on SENT).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import select, update

from app.agents.disclosure import DEFAULT_DISCLOSURES
from app.channels.adapter import ChannelAdapter, TextContent
from app.channels.policy import TextUnit, text_length
from app.channels.registry import ChannelRegistry
from app.db.models.agent import Agent, AgentStatus
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import Conversation, ConversationMode, Message, MessageOrigin
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.inbox_service import InboxService
from app.services.messaging_service import MessagingService
from app.workers.queue import AgentQueue
from tests.channel_fakes import BYTE_LIMIT, SyntheticAdapter, synthetic_payload
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    Workspace,
    _TakesTheHandoff,
    scripted,
    text_response,
)

pytestmark = pytest.mark.integration

ENGLISH = DEFAULT_DISCLOSURES["en"]
ARABIC = DEFAULT_DISCLOSURES["ar"]


@dataclass
class Page:
    workspace: Workspace
    connection: ChannelConnection
    adapter: SyntheticAdapter
    registry: ChannelRegistry
    conversation_id: uuid.UUID | None = None


async def _page(ai_turns: TurnRunner) -> Page:
    adapter = SyntheticAdapter(Channel.MESSENGER, tagged=True)
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.MESSENGER: cast(ChannelAdapter, adapter),
        },
        unmetered=True,
    )
    async with ai_turns.database.session() as session:
        tenant = Tenant(name="Disclosure", slug=f"disclosure-{uuid.uuid4().hex[:10]}")
        session.add(tenant)
        await session.flush()
        connection = ChannelConnection(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            channel=Channel.MESSENGER,
            external_account_id=f"page-{uuid.uuid4().hex[:10]}",
            status=ConnectionStatus.ACTIVE,
            ownership_started_at=datetime.now(UTC) - timedelta(days=2),
        )
        agent = Agent(
            tenant_id=tenant.id,
            name="Helper",
            is_default=True,
            status=AgentStatus.ACTIVE,
            model="gpt-4o-mini",
            system_prompt="Answer briefly.",
        )
        session.add_all([connection, agent])
        await session.flush()
        workspace = Workspace(
            tenant_id=tenant.id,
            account_id=connection.id,
            agent_id=agent.id,
            phone_number_id=connection.external_account_id,
        )
    ai_turns.tenants.append(workspace.tenant_id)
    return Page(workspace=workspace, connection=connection, adapter=adapter, registry=registry)


async def _customer_writes(ai_turns: TurnRunner, page: Page, text: str) -> uuid.UUID:
    """One customer message, through the neutral ingestion; returns its id."""
    mid = f"m.{uuid.uuid4().hex}"
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
                        "type": "message",
                        "id": mid,
                        "from": "psid-0disclosure",
                        "at": int(datetime.now(UTC).timestamp()),
                        "text": text,
                    },
                )
            )
        )
        message = await session.scalar(select(Message).where(Message.wa_message_id == mid))
        assert message is not None
        page.conversation_id = message.conversation_id
        return message.id


async def _turn(ai_turns: TurnRunner, page: Page, text: str = "Do you open on Fridays?") -> str:
    """The customer writes, the agent answers; returns the text the provider received."""
    trigger = await _customer_writes(ai_turns, page, text)
    assert page.conversation_id is not None
    await ai_turns.enqueue(page.workspace, page.conversation_id, trigger)
    worker = ai_turns.worker()
    worker._channels = page.registry
    before = len(page.adapter.log.sent)
    assert await worker.run_once(wait_seconds=1)
    assert len(page.adapter.log.sent) == before + 1, "the agent did not reply"
    _, content = page.adapter.log.sent[-1]
    assert isinstance(content, TextContent)
    return content.body


async def _disclosed_at(ai_turns: TurnRunner, page: Page) -> datetime | None:
    assert page.conversation_id is not None
    return (await ai_turns.conversation(page.conversation_id)).automation_disclosed_at


async def test_the_first_ai_reply_discloses_and_the_next_one_does_not(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    page = await _page(ai_turns)
    ai_providers.agent = scripted(
        text_response("Yes, from noon."), text_response("Until ten at night.")
    )

    first = await _turn(ai_turns, page)
    recorded = await _disclosed_at(ai_turns, page)
    second = await _turn(ai_turns, page, "And until when?")

    assert first == f"{ENGLISH}\n\nYes, from noon."
    assert recorded is not None
    assert second == "Until ten at night."
    assert await _disclosed_at(ai_turns, page) == recorded


async def test_a_reply_after_the_gap_discloses_again(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    page = await _page(ai_turns)
    ai_providers.agent = scripted(text_response("Hello."), text_response("Welcome back."))
    await _turn(ai_turns, page)
    await ai_turns.execute(
        update(Conversation)
        .where(Conversation.id == page.conversation_id)
        .values(automation_disclosed_at=datetime.now(UTC) - timedelta(hours=25))
    )

    later = await _turn(ai_turns, page, "Hi again")

    assert later.startswith(ENGLISH)


async def test_a_hand_back_from_a_person_makes_the_next_reply_disclose(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    page = await _page(ai_turns)
    ai_providers.agent = scripted(text_response("Hello."), text_response("Happy to help."))
    await _turn(ai_turns, page)
    async with ai_turns.database.session() as session:
        colleague = User(
            email=f"{uuid.uuid4().hex[:8]}@disclosure.example",
            hashed_password="x",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(colleague)
        await session.flush()
        ai_turns.users.append(colleague.id)
        assert page.conversation_id is not None
        await session.execute(
            update(Conversation)
            .where(Conversation.id == page.conversation_id)
            .values(mode=ConversationMode.HUMAN)
        )
        inbox = InboxService(session=session, tenant_id=page.workspace.tenant_id)
        await inbox.release_to_ai(conversation_id=page.conversation_id, actor=colleague)
        await session.commit()

    after = await _turn(ai_turns, page, "Are you still there?")

    assert after.startswith(ENGLISH)


async def test_a_person_never_carries_the_disclosure(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    page = await _page(ai_turns)
    ai_providers.agent = scripted(text_response("Hello."))
    await _turn(ai_turns, page)
    async with ai_turns.database.session() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == page.conversation_id)
            .values(automation_disclosed_at=None)
        )
        messaging = MessagingService(
            session=session,
            settings=ai_turns.settings,
            tenant_id=page.workspace.tenant_id,
            channels=page.registry,
        )
        assert page.conversation_id is not None
        await messaging.send_text(
            conversation_id=page.conversation_id, body="Hi, Sara here.", origin=MessageOrigin.HUMAN
        )
        await session.commit()

    _, content = page.adapter.log.sent[-1]
    assert isinstance(content, TextContent)
    assert content.body == "Hi, Sara here."


async def test_whatsapp_replies_are_unchanged(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    ai_providers.agent = scripted(text_response("Yes, from noon."))
    conversation_id, (trigger,) = await ai_turns.write(workspace, ["Open on Fridays?"])

    await ai_turns.answer(workspace, conversation_id, trigger)

    (send,) = ai_providers.sends
    assert send["text"]["body"] == "Yes, from noon."
    assert (await ai_turns.conversation(conversation_id)).automation_disclosed_at is None


async def test_an_arabic_disclosure_and_a_long_arabic_reply_fit_the_byte_limit(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    page = await _page(ai_turns)
    long_reply = "شكرا لتواصلك معنا. " * 60  # about 2,000 bytes
    ai_providers.agent = scripted(text_response(long_reply))

    sent = await _turn(ai_turns, page, "مرحبا")

    assert sent.startswith(ARABIC)
    assert text_length(sent, TextUnit.UTF8_BYTES) <= BYTE_LIMIT
    # Whole characters only: the text round-trips through UTF-8 unchanged.
    assert sent.encode("utf-8").decode("utf-8") == sent


async def test_an_undelivered_reply_records_no_disclosure(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    page = await _page(ai_turns)
    ai_providers.agent = scripted(text_response("Hello."), text_response("Hello again."))
    page.adapter.log.refuse = True

    refused = await _turn(ai_turns, page)

    assert refused.startswith(ENGLISH)
    assert await _disclosed_at(ai_turns, page) is None
    # So the next reply, delivered, still owes the disclosure.
    page.adapter.log.refuse = False
    delivered = await _turn(ai_turns, page, "Hello?")
    assert delivered.startswith(ENGLISH)
    assert await _disclosed_at(ai_turns, page) is not None


async def test_a_workspaces_own_wording_replaces_wasla_s(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    page = await _page(ai_turns)
    async with ai_turns.database.session() as session:
        await session.execute(
            update(Tenant)
            .where(Tenant.id == page.workspace.tenant_id)
            .values(automation_disclosure={"en": "Hi! I'm Nour's assistant bot."})
        )
        await session.commit()
    ai_providers.agent = scripted(text_response("We open at noon."))

    sent = await _turn(ai_turns, page)

    assert sent == "Hi! I'm Nour's assistant bot.\n\nWe open at noon."
