"""A late or retried delivery never moves a conversation backwards (OMNI-036).

`last_inbound_at` is what every channel's reply window is computed from, and
`last_message_at` is the inbox order. Both used to be assigned the provider's
timestamp unconditionally, so a message Meta delivered late - a retry of a
delivery refused during an outage, up to seven days later - moved the window
anchor back and could close a window the provider still held open: every
free-text reply, human or AI, was refused until the customer wrote again.

Each case is driven through `WhatsAppIngestionService`, and the race through two
real database sessions with the older delivery committing last.

Mutant this suite kills: M-O07 (`touch_inbound` assigns without GREATEST).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.conversation import Conversation, ConversationStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.integrations.whatsapp.policy import WhatsAppChannelPolicy
from app.repositories.conversation_repository import ConversationRepository
from app.services.whatsapp_service import WhatsAppIngestionService

pytestmark = pytest.mark.integration

CUSTOMER = "201000000636"


async def _number(session: AsyncSession) -> WhatsAppAccount:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Anchors {tag}", slug=f"anchors-{tag}")
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"pn-{tag}",
        waba_id=f"waba-{tag}",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()
    return account


def _text(account: WhatsAppAccount, at: datetime, *, phone: str = CUSTOMER) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": account.phone_number_id},
                            "contacts": [{"wa_id": phone, "profile": {"name": "Late"}}],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": str(int(at.timestamp())),
                                    "type": "text",
                                    "text": {"body": f"sent at {at.isoformat()}"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _conversation(session: AsyncSession, account: WhatsAppAccount) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(Conversation.tenant_id == account.tenant_id)
    )
    assert conversation is not None
    return conversation


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


async def test_a_late_older_message_leaves_the_anchors_at_the_newer_one(
    db_session: AsyncSession,
) -> None:
    account = await _number(db_session)
    newer = _now() - timedelta(minutes=5)
    older = newer - timedelta(hours=25)
    service = WhatsAppIngestionService(session=db_session)

    await service.ingest(_text(account, newer))
    await WhatsAppIngestionService(session=db_session).ingest(_text(account, older))
    await db_session.flush()

    conversation = await _conversation(db_session, account)
    await db_session.refresh(conversation)
    assert conversation.last_inbound_at == newer
    assert conversation.last_message_at == newer
    # The window the provider holds open is open here too.
    assert WhatsAppChannelPolicy().standard_window_open(conversation, now=_now())


async def test_a_newer_message_moves_the_anchors_forward(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    older = _now() - timedelta(hours=30)
    newer = _now() - timedelta(minutes=1)

    await WhatsAppIngestionService(session=db_session).ingest(_text(account, older))
    await WhatsAppIngestionService(session=db_session).ingest(_text(account, newer))
    await db_session.flush()

    conversation = await _conversation(db_session, account)
    await db_session.refresh(conversation)
    assert (conversation.last_inbound_at, conversation.last_message_at) == (newer, newer)


async def test_a_late_message_still_reopens_a_closed_conversation(
    db_session: AsyncSession,
) -> None:
    """The reopening is unchanged: a customer writing is a live conversation."""
    account = await _number(db_session)
    newer = _now() - timedelta(minutes=2)
    await WhatsAppIngestionService(session=db_session).ingest(_text(account, newer))
    await db_session.flush()
    conversation = await _conversation(db_session, account)
    conversation.status = ConversationStatus.CLOSED
    await db_session.flush()

    await WhatsAppIngestionService(session=db_session).ingest(
        _text(account, newer - timedelta(hours=3))
    )
    await db_session.flush()

    await db_session.refresh(conversation)
    assert conversation.status is ConversationStatus.OPEN
    assert conversation.last_inbound_at == newer


async def test_the_inbox_order_ignores_a_late_older_message(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    base = _now() - timedelta(hours=2)
    await WhatsAppIngestionService(session=db_session).ingest(
        _text(account, base, phone="201000000001")
    )
    await WhatsAppIngestionService(session=db_session).ingest(
        _text(account, base + timedelta(minutes=30), phone="201000000002")
    )
    # A retry of a day-old message from the first customer arrives last.
    await WhatsAppIngestionService(session=db_session).ingest(
        _text(account, base - timedelta(days=1), phone="201000000001")
    )
    await db_session.flush()

    inbox = await ConversationRepository(db_session, tenant_id=account.tenant_id).list_open()

    assert [conversation.last_message_at for conversation in inbox] == [
        base + timedelta(minutes=30),
        base,
    ]


async def test_a_send_never_moves_the_inbox_order_back(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    newer = _now() - timedelta(minutes=1)
    await WhatsAppIngestionService(session=db_session).ingest(_text(account, newer))
    await db_session.flush()
    conversation = await _conversation(db_session, account)
    repository = ConversationRepository(db_session, tenant_id=account.tenant_id)

    await repository.touch_outbound(conversation, at=newer - timedelta(hours=1))
    assert conversation.last_message_at == newer
    await repository.touch_outbound(conversation, at=newer + timedelta(seconds=5))
    assert conversation.last_message_at == newer + timedelta(seconds=5)
    # The window is the customer's alone: a send never touches it.
    assert conversation.last_inbound_at == newer


async def test_concurrent_deliveries_settle_on_the_newest(prepared_database: str) -> None:
    """The older delivery writes last, after waiting on the newer one's row lock."""
    database = Database(
        Settings(_env_file=None, environment="test", database_url=prepared_database)
    )
    newer = _now() - timedelta(minutes=3)
    older = newer - timedelta(days=2)
    try:
        async with database.session() as session:
            account = await _number(session)
            tenant_id = account.tenant_id
            await WhatsAppIngestionService(session=session).ingest(_text(account, older))
            conversation_id = (await _conversation(session, account)).id
            await session.commit()

        async with database.session() as first, database.session() as second:
            winner = await first.get(Conversation, conversation_id)
            loser = await second.get(Conversation, conversation_id)
            assert winner is not None and loser is not None
            await ConversationRepository(first, tenant_id=tenant_id).touch_inbound(winner, at=newer)
            # Blocks on the row the first session has updated and not committed.
            late = asyncio.create_task(
                ConversationRepository(second, tenant_id=tenant_id).touch_inbound(
                    loser, at=older - timedelta(hours=1)
                )
            )
            await asyncio.sleep(0.3)
            assert not late.done(), "the second writer should be waiting on the row lock"
            await first.commit()
            await late
            await second.commit()

        async with database.session() as session:
            settled = await session.get(Conversation, conversation_id)
            assert settled is not None
            assert settled.last_inbound_at == newer
            assert settled.last_message_at == newer
    finally:
        async with database.session() as session:
            tenant = await session.get(Tenant, tenant_id)
            if tenant is not None:
                await session.delete(tenant)
            await session.commit()
        await database.dispose()
