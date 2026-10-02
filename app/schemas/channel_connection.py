"""A workspace's channel connections, whatever their channel (ENT-07, section 30).

What a capacity page lists beside the slots: each connection, whether it takes
a slot now, and - for a disabled one - why, so an owner can tell a connection
they paused from one a capacity reduction disabled, and enable it again when a
slot is free.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel

from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionDisabledReason,
    ConnectionHealth,
    ConnectionStatus,
)


class ChannelConnectionRead(BaseModel):
    """One connection. `counts_toward_capacity` is true for active, unreleased ones."""

    id: uuid.UUID
    channel: Channel
    external_account_id: str
    status: ConnectionStatus
    health: ConnectionHealth
    counts_toward_capacity: bool
    disabled_reason: ConnectionDisabledReason | None
    disabled_at: datetime | None
    ownership_started_at: datetime

    @classmethod
    def from_model(cls, connection: ChannelConnection) -> Self:
        return cls(
            id=connection.id,
            channel=connection.channel,
            external_account_id=connection.external_account_id,
            status=connection.status,
            health=connection.health,
            counts_toward_capacity=connection.counts_toward_capacity,
            disabled_reason=connection.disabled_reason,
            disabled_at=connection.disabled_at,
            ownership_started_at=connection.ownership_started_at,
        )


__all__ = ["ChannelConnectionRead"]
