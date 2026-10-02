"""A delivery the workspace-wide key refuses is kept, not discarded (OMNI-043).

Until the compatibility cleanup (O8) the legacy `(tenant_id, event_id)` key stands
beside the per-connection one and decides first: the same provider id arriving on
a second connection of one workspace was refused by it and dropped - counted as a
collision, payload gone. Harmless for Meta's globally unique ids, and silent
message loss for any provider whose ids are unique only per connection.

Driven through the neutral ingestion with the synthetic channel, whose ids are
connection-scoped like Instagram's and Messenger's.

Mutant this suite kills: M-O15 (colliding events discarded again).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.channel_event import ChannelEvent, ChannelEventState
from app.db.models.conversation import Conversation, Message
from app.db.models.tenant import Tenant
from app.repositories.channel_event_repository import (
    COLLISION_EVIDENCE_PREFIX,
    EVENT_ID_COLLISION,
    collision_evidence_key,
)
from app.services.channel_ingestion_service import ChannelIngestionService, IngestionOutcome
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

pytestmark = pytest.mark.integration


async def _workspace(session: AsyncSession) -> Tenant:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Collisions {tag}", slug=f"collisions-{tag}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _connection(session: AsyncSession, tenant: Tenant) -> ChannelConnection:
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"syn-{uuid.uuid4().hex[:12]}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(connection)
    await session.flush()
    return connection


async def _deliver(
    session: AsyncSession, connection: ChannelConnection, mid: str, *, text: str = "hello"
) -> IngestionOutcome:
    adapter = SyntheticAdapter(Channel.INSTAGRAM)
    event: dict[str, Any] = {
        "type": "message",
        "id": mid,
        "from": "igsid-0collision",
        "at": int(datetime.now(UTC).timestamp()),
        "text": text,
    }
    outcome = await ChannelIngestionService(
        session=session, adapter=cast(ChannelAdapter, adapter)
    ).ingest(adapter.parse(synthetic_payload(connection.external_account_id, event)))
    await session.flush()
    return outcome


async def _events(session: AsyncSession, connection: ChannelConnection) -> list[ChannelEvent]:
    return list(
        (
            await session.scalars(
                select(ChannelEvent).where(ChannelEvent.account_id == connection.id)
            )
        ).all()
    )


async def test_a_collided_delivery_is_kept_as_failed_evidence_on_its_own_connection(
    db_session: AsyncSession,
) -> None:
    tenant = await _workspace(db_session)
    first, second = await _connection(db_session, tenant), await _connection(db_session, tenant)
    mid = f"mid.{uuid.uuid4().hex}"
    await _deliver(db_session, first, mid)

    outcome = await _deliver(db_session, second, mid, text="a different customer")

    assert (outcome.stored, outcome.collisions) == (0, 1)
    (evidence,) = await _events(db_session, second)
    assert evidence.event_id == collision_evidence_key(second.id, mid)
    assert evidence.event_id.startswith(COLLISION_EVIDENCE_PREFIX)
    assert evidence.state is ChannelEventState.FAILED
    assert evidence.error == EVENT_ID_COLLISION
    assert evidence.payload is not None
    assert evidence.payload["text"] == "a different customer"
    # Evidence only: nothing projected on the second connection.
    conversations = await db_session.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.account_id == second.id)
    )
    assert conversations == 0


async def test_a_replay_of_the_collided_delivery_keeps_one_piece_of_evidence(
    db_session: AsyncSession,
) -> None:
    tenant = await _workspace(db_session)
    first, second = await _connection(db_session, tenant), await _connection(db_session, tenant)
    mid = f"mid.{uuid.uuid4().hex}"
    await _deliver(db_session, first, mid)
    await _deliver(db_session, second, mid)

    replay = await _deliver(db_session, second, mid)

    assert replay.collisions == 1
    assert len(await _events(db_session, second)) == 1


async def test_the_same_id_in_another_workspace_is_simply_stored(
    db_session: AsyncSession,
) -> None:
    mid = f"mid.{uuid.uuid4().hex}"
    ours = await _connection(db_session, await _workspace(db_session))
    theirs = await _connection(db_session, await _workspace(db_session))
    await _deliver(db_session, ours, mid)

    outcome = await _deliver(db_session, theirs, mid)

    assert (outcome.stored, outcome.collisions) == (1, 0)
    (event,) = await _events(db_session, theirs)
    assert event.event_id == mid
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Message).where(Message.connection_id == theirs.id)
        )
        == 1
    )


def test_an_id_too_long_to_prefix_is_kept_by_its_digest() -> None:
    connection_id = uuid.uuid4()
    key = collision_evidence_key(connection_id, "m" * 250)

    assert len(key) <= 255
    assert key.startswith(f"{COLLISION_EVIDENCE_PREFIX}{connection_id.hex}:sha256:")
    assert collision_evidence_key(connection_id, "short") == (
        f"{COLLISION_EVIDENCE_PREFIX}{connection_id.hex}:short"
    )
