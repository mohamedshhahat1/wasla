"""What platform staff read about entitlements across workspaces (PLAT-G2, PLAT-G5).

The capacity-reduction queue - every workspace in a grace, and when each ends -
and the channel vocabulary a staff form offers. A workspace's own channel
capacity and connections are read with the tenant's schemas
(`ChannelCapacityRead`, `ChannelConnectionRead`), never a platform copy of them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.channels.policy import ChannelState
from app.db.models.channel import Channel
from app.db.models.channel_capacity import CapacityReductionCause, CapacityReductionStatus
from app.schemas.channel_capacity import FallbackPreviewRead


class PlatformCapacityReductionRead(BaseModel):
    """One capacity reduction, as the queue lists it.

    `active_now` is the workspace's connections taking a slot at the moment of
    the read. `would_be_disabled_count` is, for the open reduction, how many
    the automatic fallback would disable if the grace ended now (what the
    owner's capacity page shows), and for a closed one how many it disabled.
    `topup_purchase_id` names the grant whose withdrawal opened it
    (`cause: grant_withdrawn`), else null.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str
    cause: CapacityReductionCause
    status: CapacityReductionStatus
    effective_at: datetime
    grace_ends_at: datetime
    target_general_limit: int
    target_typed_slots: dict[str, int]
    target_allowed_channel_types: list[str]
    active_now: int
    would_be_disabled_count: int
    notified_at: datetime | None
    warned_at: datetime | None
    resolved_at: datetime | None
    topup_purchase_id: uuid.UUID | None
    revision: int


class PlatformCapacityReductionDetail(PlatformCapacityReductionRead):
    """One reduction with what it kept and disabled, and what is still to come.

    `preselected` and `automatic_fallback` describe the workspace now and are
    filled only while this is its open reduction - the same values its
    `GET /billing/channel-capacity` shows; a closed reduction reads them null
    and empty.
    """

    kept_connection_ids: list[uuid.UUID]
    disabled_connection_ids: list[uuid.UUID]
    preselected: list[uuid.UUID]
    automatic_fallback: FallbackPreviewRead | None


class ChannelTypeRead(BaseModel):
    """One label of the channel vocabulary (PLAT-G5).

    `operable` is whether this deployment registered an adapter for it: only
    then can a workspace connect one. `state` says more - an operable channel
    may be paused. A plan, a top-up or a grant may still name a channel that
    is not operable yet; nothing can be connected on it until it is.
    """

    channel: Channel
    operable: bool
    state: ChannelState


__all__ = [
    "ChannelTypeRead",
    "PlatformCapacityReductionDetail",
    "PlatformCapacityReductionRead",
]
