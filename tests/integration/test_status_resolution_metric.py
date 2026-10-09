"""Every status lookup is timed and observed once per delivery (OMNI-029).

`wasla_status_resolution_duration_seconds{channel}` is how a regression to the
sequential scan would show in production. Proved through
`ChannelIngestionService` on the synthetic channel: one observation per status
event, labelled by channel, and none for a delivery with no statuses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.tenant import Tenant
from app.services import channel_ingestion_service as ingestion_module
from app.services.channel_ingestion_service import ChannelIngestionService
from app.workers.queue import AgentQueue
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

pytestmark = pytest.mark.integration


class RecordingQueue:
    async def enqueue(self, job: Any) -> None:
        return None


async def test_each_status_lookup_is_observed_by_channel(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[str, list[float]]] = []

    async def record(channel: str, durations: list[float]) -> None:
        observed.append((channel, list(durations)))

    monkeypatch.setattr(ingestion_module, "record_status_resolutions", record)
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Metric {tag}", slug=f"metric-{tag}")
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
    service = ChannelIngestionService(
        session=db_session,
        adapter=cast(ChannelAdapter, adapter),
        queue=cast(AgentQueue, RecordingQueue()),
    )
    at = int(datetime.now(UTC).timestamp())
    statuses = [
        {"type": "status", "id": f"m.{tag}.{n}", "status": "delivered", "at": at} for n in range(3)
    ]

    await service.ingest(
        adapter.parse(synthetic_payload(connection.external_account_id, *statuses))
    )
    await service.ingest(
        adapter.parse(
            synthetic_payload(
                connection.external_account_id,
                {
                    "type": "message",
                    "id": f"m.{tag}.in",
                    "from": "igsid-0m",
                    "at": at,
                    "text": "hi",
                },
            )
        )
    )

    assert [channel for channel, _ in observed] == ["instagram", "instagram"]
    first, second = (durations for _, durations in observed)
    assert len(first) == 3 and all(seconds >= 0 for seconds in first)
    assert second == []
