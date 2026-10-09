"""A Messenger/Instagram postback is a message carrying an action (OMNI-053, ADR-125).

`postback {mid, title, payload}` reuses OMNI-030's reply action rather than a
kind of its own: the transcript shows the title, the payload is kept for
routing, the model reads it as a tap and the customer is answered. Driven
through `ChannelIngestionService` on the synthetic channel; real Meta payloads
are an adapter-stage concern.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.memory import build_window
from app.channels.adapter import ChannelAdapter
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import Message, MessageKind, ReplyActionSource
from app.db.models.tenant import Tenant
from app.services.channel_ingestion_service import ChannelIngestionService
from app.workers.queue import AgentQueue
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

pytestmark = pytest.mark.integration


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[Any] = []

    async def enqueue(self, job: Any) -> None:
        self.jobs.append(job)


async def test_a_postback_is_stored_with_its_action_and_answered(
    db_session: AsyncSession,
) -> None:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Postback {tag}", slug=f"postback-{tag}")
    db_session.add(tenant)
    await db_session.flush()
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"ig-{tag}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=30),
    )
    db_session.add(connection)
    await db_session.flush()
    adapter = SyntheticAdapter()
    queue = RecordingQueue()
    event = {
        "type": "postback",
        "id": f"m.{tag}",
        "from": "igsid-0postback",
        "at": int(datetime.now(UTC).timestamp()),
        "title": "Get started",
        "payload": "GET_STARTED",
    }

    outcome = await ChannelIngestionService(
        session=db_session, adapter=cast(ChannelAdapter, adapter), queue=cast(AgentQueue, queue)
    ).ingest(adapter.parse(synthetic_payload(connection.external_account_id, event)))
    await db_session.flush()

    message = await db_session.scalar(select(Message).where(Message.connection_id == connection.id))
    assert message is not None
    assert outcome.stored == 1
    assert message.kind is MessageKind.INTERACTIVE
    assert message.body == "Get started"
    assert (message.action_source, message.action_payload, message.action_title) == (
        ReplyActionSource.POSTBACK,
        "GET_STARTED",
        "Get started",
    )
    window = build_window([message], message_limit=10, token_budget=4_000)
    assert window.turns[0].text == "[tapped: Get started]"
    assert len(queue.jobs) == 1
