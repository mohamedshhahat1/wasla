"""The connection allowance seam, without a database (OMNI-017, ADR-123).

The row arithmetic and its concurrency are proved against PostgreSQL in
`tests/integration/test_connection_throughput.py`. What is pinned here is the
decision around it: off unless configured, bulk refused, replies only counted,
and a refusal that tells a client when to come back.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.throughput import (
    CONNECTION_THROTTLED,
    THROTTLED_ORIGINS,
    ConnectionThrottledError,
)
from app.core.config import Settings
from app.core.exceptions import RateLimitedError
from app.db.models.conversation import MessageOrigin
from app.services.messaging_service import MessagingService

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
CONNECTION = uuid.uuid4()
# What `_take_allowance` reads of the connection row: its id and its own allowance.
CONNECTION_ROW: Any = SimpleNamespace(id=CONNECTION, sends_per_minute=None)


class RecordingConnections:
    """Stands in for the connection repository's allowance, recording each ask."""

    def __init__(self, *, refuse_until: datetime | None = None) -> None:
        self.asks: list[dict[str, Any]] = []
        self._refuse_until = refuse_until

    async def take_send_allowance(
        self,
        connection_id: uuid.UUID,
        *,
        per_window: int,
        now: datetime,
        may_refuse: bool = True,
    ) -> datetime | None:
        self.asks.append(
            {"connection_id": connection_id, "per_window": per_window, "may_refuse": may_refuse}
        )
        return self._refuse_until if may_refuse else None


def _service(connections: RecordingConnections, **settings: Any) -> MessagingService:
    configured = Settings(_env_file=None, environment="test", **settings)
    service = MessagingService(
        session=cast(AsyncSession, object()), settings=configured, tenant_id=uuid.uuid4()
    )
    service._connections = connections  # type: ignore[assignment]
    return service


def test_only_bulk_senders_are_ever_held_back() -> None:
    assert frozenset({MessageOrigin.CAMPAIGN, MessageOrigin.FOLLOW_UP}) == THROTTLED_ORIGINS
    for origin in (MessageOrigin.AGENT, MessageOrigin.HUMAN, MessageOrigin.SYSTEM):
        assert origin not in THROTTLED_ORIGINS


async def test_nothing_is_counted_unless_an_allowance_is_configured() -> None:
    """Default off: ADR-026's per-campaign rate stays the only rule."""
    connections = RecordingConnections(refuse_until=NOW)
    service = _service(connections)

    await service._take_allowance(CONNECTION_ROW, origin=MessageOrigin.CAMPAIGN)

    assert connections.asks == []


async def test_a_campaign_over_the_allowance_is_refused_before_anything_is_staged() -> None:
    reopens = datetime.now(UTC) + timedelta(seconds=41)
    connections = RecordingConnections(refuse_until=reopens)
    service = _service(connections, connection_sends_per_minute=20)

    with pytest.raises(ConnectionThrottledError) as refused:
        await service._take_allowance(CONNECTION_ROW, origin=MessageOrigin.CAMPAIGN)

    assert connections.asks == [{"connection_id": CONNECTION, "per_window": 20, "may_refuse": True}]
    error = refused.value
    assert error.retry_at == reopens
    assert error.connection_id == CONNECTION
    assert error.error_code == CONNECTION_THROTTLED
    assert isinstance(error, RateLimitedError)
    assert error.headers is not None and 1 <= int(error.headers["Retry-After"]) <= 42


@pytest.mark.parametrize("origin", [MessageOrigin.AGENT, MessageOrigin.HUMAN])
async def test_a_reply_is_counted_and_never_refused(origin: MessageOrigin) -> None:
    """An agent's reply is sent after its inference is paid for; refusing it
    would lose an answer a customer is waiting for."""
    connections = RecordingConnections(refuse_until=NOW + timedelta(minutes=1))
    service = _service(connections, connection_sends_per_minute=20)

    await service._take_allowance(CONNECTION_ROW, origin=origin)

    assert connections.asks == [
        {"connection_id": CONNECTION, "per_window": 20, "may_refuse": False}
    ]


def test_retry_after_is_whole_seconds_and_never_zero() -> None:
    soon = ConnectionThrottledError(
        connection_id=CONNECTION, retry_at=NOW + timedelta(milliseconds=200), now=NOW
    )
    later = ConnectionThrottledError(
        connection_id=CONNECTION, retry_at=NOW + timedelta(seconds=30, milliseconds=1), now=NOW
    )
    past = ConnectionThrottledError(connection_id=CONNECTION, retry_at=NOW, now=NOW)

    assert soon.headers == {"Retry-After": "1"}
    assert later.headers == {"Retry-After": "31"}
    assert past.headers == {"Retry-After": "1"}


def test_a_blank_allowance_setting_means_none() -> None:
    assert Settings(_env_file=None, environment="test").connection_sends_per_minute is None
    assert (
        Settings(
            _env_file=None, environment="test", connection_sends_per_minute=" "
        ).connection_sends_per_minute
        is None
    )
    with pytest.raises(ValueError):
        Settings(_env_file=None, environment="test", connection_sends_per_minute=0)
