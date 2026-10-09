"""A read watermark is compared on the provider's clock (OMNI-042).

Messenger reports reads as `message_reads.read.watermark`: "All messages that were
sent before or at this timestamp were read" (Messenger Platform webhook reference,
read 2026-10-02; the final audit's F2) - Meta's time. Wasla compared it with its
own `sent_at`, taken after the Send API answered, so the newest message was never
marked read (probe P5: a watermark 1 s before the local `sent_at` advanced 0).

**Synthetic evidence.** The synthetic channel's provider stamps each send on a
clock it controls; proving this against real Messenger payloads is an external
verification listed in the report.

Mutant this suite kills: M-O25 (the watermark compares the local `sent_at` only).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import Conversation, Message, MessageOrigin, MessageStatus
from app.db.models.tenant import Tenant
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.messaging_service import MessagingService
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

pytestmark = pytest.mark.integration

READER = "psid-0watermark"


async def _sent(
    session: AsyncSession, settings: Settings, *, lag: timedelta | None
) -> tuple[SyntheticAdapter, ChannelConnection, Message]:
    adapter = SyntheticAdapter(Channel.MESSENGER, tagged=True)
    adapter.log.provider_clock_lag = lag
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Reads {tag}", slug=f"reads-{tag}")
    session.add(tenant)
    await session.flush()
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.MESSENGER,
        external_account_id=f"page-{tag}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
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
            "from": READER,
            "at": int(datetime.now(UTC).timestamp()) - 30,
            "text": "hi",
        },
    )
    conversation = await session.scalar(
        select(Conversation).where(Conversation.account_id == connection.id)
    )
    assert conversation is not None
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.MESSENGER: cast(ChannelAdapter, adapter),
        },
    )
    message = await MessagingService(
        session=session, settings=settings, tenant_id=tenant.id, channels=registry
    ).send_text(conversation_id=conversation.id, body="Here you go", origin=MessageOrigin.AGENT)
    return adapter, connection, message


async def _deliver(
    session: AsyncSession,
    adapter: SyntheticAdapter,
    connection: ChannelConnection,
    event: dict[str, Any],
    *,
    tolerance: timedelta = timedelta(seconds=5),
) -> None:
    await ChannelIngestionService(
        session=session, adapter=cast(ChannelAdapter, adapter), watermark_tolerance=tolerance
    ).ingest(adapter.parse(synthetic_payload(connection.external_account_id, event)))
    await session.flush()


async def _status(session: AsyncSession, message: Message) -> MessageStatus:
    await session.refresh(message)
    return message.status


def _read(at: datetime) -> dict[str, Any]:
    stamp = int(at.timestamp())
    return {"type": "read", "from": READER, "watermark": stamp, "at": stamp}


async def test_a_watermark_at_the_providers_time_reads_the_newest_message(
    db_session: AsyncSession, settings: Settings
) -> None:
    adapter, connection, message = await _sent(db_session, settings, lag=timedelta(seconds=3))
    assert message.provider_sent_at is not None and message.sent_at is not None
    assert message.provider_sent_at < message.sent_at

    # One second before Wasla's own clock - and at the provider's.
    await _deliver(db_session, adapter, connection, _read(message.sent_at - timedelta(seconds=1)))

    await db_session.refresh(message)
    assert message.status is MessageStatus.READ


async def test_a_watermark_before_the_providers_time_reads_nothing(
    db_session: AsyncSession, settings: Settings
) -> None:
    adapter, connection, message = await _sent(db_session, settings, lag=timedelta(seconds=3))
    assert message.provider_sent_at is not None

    await _deliver(
        db_session, adapter, connection, _read(message.provider_sent_at - timedelta(seconds=2))
    )

    await db_session.refresh(message)
    assert message.status is MessageStatus.SENT


async def test_without_a_provider_time_the_tolerance_is_bounded(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A provider whose answer carries no time: Wasla's clock, give or take five seconds."""
    adapter, connection, message = await _sent(db_session, settings, lag=None)
    assert message.provider_sent_at is None and message.sent_at is not None

    await _deliver(db_session, adapter, connection, _read(message.sent_at - timedelta(seconds=30)))
    assert await _status(db_session, message) is MessageStatus.SENT

    await _deliver(db_session, adapter, connection, _read(message.sent_at - timedelta(seconds=2)))
    assert await _status(db_session, message) is MessageStatus.READ


async def test_an_echo_of_the_send_supplies_the_providers_time(
    db_session: AsyncSession, settings: Settings
) -> None:
    adapter, connection, message = await _sent(db_session, settings, lag=None)
    provider_time = (datetime.now(UTC) - timedelta(seconds=40)).replace(microsecond=0)

    await _deliver(
        db_session,
        adapter,
        connection,
        {
            "type": "echo",
            "id": message.wa_message_id,
            "to": READER,
            "at": int(provider_time.timestamp()),
            "text": "Here you go",
        },
    )

    await db_session.refresh(message)
    assert message.provider_sent_at == provider_time
