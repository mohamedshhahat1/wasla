"""Channel capacity as a workspace sees it, and an owner's selection (ENT-14, ENT-15).

`GET /billing/channel-capacity` answers everything a capacity page needs in one
read: the slots in force and what takes them, the capacity a scheduled change
leaves, an open reduction and its grace, what the automatic fallback would do,
and the revision a selection must name. `POST /billing/channel-capacity/selection`
takes the connections to keep and nothing else.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.channel import Channel
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.schemas.billing import ChannelCapacityBreakdown, TypedSlotRead
from app.services.channel_fit import ChannelCapacity
from app.services.entitlement_terms import ordered

#: The most connections one selection may name. A request bound, not a product rule.
MAX_SELECTION = 500


class ScheduledCapacityRead(BaseModel):
    """The capacity a scheduled plan change leaves at its boundary (ENT-14)."""

    effective_at: datetime
    effective_limit: int | None
    general_limit: int | None
    typed_slots: list[TypedSlotRead]
    allowed_channel_types: list[Channel]
    # Whether today's active connections fit it. False means new connections
    # are refused now, and the boundary will open a reduction or apply the
    # owner's pre-selection.
    fits: bool


class CapacityReductionRead(BaseModel):
    """One reduction: why, by when, to what, and how it ended."""

    id: uuid.UUID
    cause: CapacityReductionCause
    status: CapacityReductionStatus
    effective_at: datetime
    grace_ends_at: datetime
    target_general_limit: int
    target_typed_slots: dict[str, int]
    target_allowed_channel_types: list[str]
    resolved_at: datetime | None
    kept_connection_ids: list[uuid.UUID] | None
    disabled_connection_ids: list[uuid.UUID] | None
    revision: int

    @classmethod
    def from_model(cls, reduction: ChannelCapacityReduction) -> Self:
        return cls(
            id=reduction.id,
            cause=reduction.cause,
            status=reduction.status,
            effective_at=reduction.effective_at,
            grace_ends_at=reduction.grace_ends_at,
            target_general_limit=reduction.target_general,
            target_typed_slots=dict(reduction.target_typed),
            target_allowed_channel_types=list(reduction.target_allowed_types),
            resolved_at=reduction.resolved_at,
            kept_connection_ids=reduction.kept_connection_ids,
            disabled_connection_ids=reduction.disabled_connection_ids,
            revision=reduction.revision,
        )


class FallbackPreviewRead(BaseModel):
    """What the automatic fallback would keep and disable if it ran now (ENT-14)."""

    keep: list[uuid.UUID]
    disable: list[uuid.UUID]


class ChannelCapacityRead(BaseModel):
    """This workspace's channel capacity and where it is heading.

    `effective_limit` and `remaining` are null when unlimited. `remaining` is
    the general slots left for a connection of a type with no typed slot of its
    own. `selection_revision` is what `POST .../selection` must send as
    `expected_revision`: the open reduction's while one is open, else the
    subscription's.
    """

    effective_limit: int | None
    base_limit: int | None
    topup_limit: int
    platform_grant_limit: int
    active: int
    remaining: int | None
    over_limit: bool
    breakdown: ChannelCapacityBreakdown
    scheduled: ScheduledCapacityRead | None
    reduction: CapacityReductionRead | None
    automatic_fallback: FallbackPreviewRead | None
    preselected: list[uuid.UUID]
    selection_revision: int | None


class ChannelCapacitySelection(BaseModel):
    """The connections an owner keeps. Everything else active is disabled - never released."""

    model_config = ConfigDict(extra="forbid")

    keep: list[uuid.UUID] = Field(max_length=MAX_SELECTION)
    expected_revision: int = Field(ge=1)


class ChannelCapacitySelectionResult(BaseModel):
    """What the selection did, and the capacity after it."""

    applied: bool
    kept: list[uuid.UUID]
    disabled: list[uuid.UUID]
    capacity: ChannelCapacityRead


def scheduled_read(
    capacity: ChannelCapacity, *, effective_at: datetime, fits: bool
) -> ScheduledCapacityRead:
    typed = capacity.typed
    return ScheduledCapacityRead(
        effective_at=effective_at,
        effective_limit=capacity.total,
        general_limit=capacity.general,
        typed_slots=[
            TypedSlotRead(
                channel=channel, capacity=typed[channel], used=capacity.typed_used(channel)
            )
            for channel in ordered(typed)
        ],
        allowed_channel_types=ordered(capacity.allowed),
        fits=fits,
    )


__all__ = [
    "MAX_SELECTION",
    "CapacityReductionRead",
    "ChannelCapacityRead",
    "ChannelCapacitySelection",
    "ChannelCapacitySelectionResult",
    "FallbackPreviewRead",
    "ScheduledCapacityRead",
    "scheduled_read",
]
