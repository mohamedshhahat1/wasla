"""Entitlement reads on the platform billing control plane (PLAT-G2, -G3, -G5; ADR-132).

Same prefix, authority and contract as `platform_billing`: platform owner or
admin on every route, 403 for every tenant role, `limit` at most 100, and a
`platform_billing_read` access entry for every read - against the workspace
when the read is one workspace's.

* **The capacity-reduction queue** - every workspace coming down to a smaller
  channel capacity, the grace ending soonest first - and one workspace's
  history. Reads only: staff cannot resolve, extend, shorten or cancel a
  reduction here.
* **A workspace's channel capacity and connections**, exactly as the workspace
  reads them: the same functions compute both, and the same schemas carry
  them - no credential, token or secret, nothing the tenant route does not
  already show. Staff still cannot connect, enable, disable or release one.
* **The channel vocabulary**, and which channels this deployment can operate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import ChannelRegistryDep, PlatformAccessAuditDep, PlatformStaffDep
from app.api.route import CommittingRoute
from app.channels.policy import ChannelState
from app.core.dependencies import SessionDep, SettingsDep
from app.core.exceptions import NotFoundError
from app.db.models.channel import Channel
from app.db.models.channel_capacity import CapacityReductionCause, CapacityReductionStatus
from app.db.models.tenant import Tenant
from app.platform.capacity_reduction_queue import CapacityReductionQueue
from app.schemas.channel_capacity import ChannelCapacityRead
from app.schemas.channel_connection import ChannelConnectionRead
from app.schemas.platform_billing import MAX_PAGE, Page
from app.schemas.platform_entitlements import (
    ChannelTypeRead,
    PlatformCapacityReductionDetail,
    PlatformCapacityReductionRead,
)
from app.services import channel_capacity_view

router = APIRouter(route_class=CommittingRoute, prefix="/platform/billing", tags=["platform"])

LimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE)]
OffsetQuery = Annotated[int, Query(ge=0, le=1_000_000)]


def get_queue(session: SessionDep, settings: SettingsDep) -> CapacityReductionQueue:
    return CapacityReductionQueue(session, settings=settings)


QueueDep = Annotated[CapacityReductionQueue, Depends(get_queue)]


async def _require_workspace(session: SessionDep, tenant_id: uuid.UUID) -> None:
    if await session.get(Tenant, tenant_id) is None:
        raise NotFoundError("No such workspace.")


# ------------------------------------------------------ reduction queue


@router.get("/capacity-reductions", response_model=Page[PlatformCapacityReductionRead])
async def list_capacity_reductions(
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    queue: QueueDep,
    reduction_status: Annotated[list[CapacityReductionStatus] | None, Query(alias="status")] = None,
    cause: Annotated[list[CapacityReductionCause] | None, Query()] = None,
    tenant_id: uuid.UUID | None = None,
    grace_ends_before: datetime | None = None,
    grace_ends_after: datetime | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[PlatformCapacityReductionRead]:
    """Every workspace's capacity reductions, the grace ending soonest first (PLAT-G2).

    `status` and `cause` repeat (`?status=pending_selection&cause=downgrade&cause=...`).
    `grace_ends_before` is exclusive, `grace_ends_after` inclusive.
    """
    page = await queue.list_reductions(
        statuses=reduction_status,
        causes=cause,
        tenant_id=tenant_id,
        grace_ends_before=grace_ends_before,
        grace_ends_after=grace_ends_after,
        limit=limit,
        offset=offset,
    )
    access.billing_read(
        actor=staff.user,
        resource="capacity_reductions",
        tenant_id=tenant_id,
        returned=len(page.items),
    )
    return Page(items=page.items, total=page.total, limit=limit, offset=offset)


@router.get("/capacity-reductions/{reduction_id}", response_model=PlatformCapacityReductionDetail)
async def get_capacity_reduction(
    reduction_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    queue: QueueDep,
) -> PlatformCapacityReductionDetail:
    """One reduction: what it kept and disabled, and while open, the fallback's preview."""
    result = await queue.get(reduction_id)
    access.billing_read(actor=staff.user, resource="capacity_reduction", tenant_id=result.tenant_id)
    return result


@router.get(
    "/tenants/{tenant_id}/capacity-reductions",
    response_model=Page[PlatformCapacityReductionRead],
)
async def list_workspace_capacity_reductions(
    tenant_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    queue: QueueDep,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[PlatformCapacityReductionRead]:
    """One workspace's every reduction, newest first. 404 for no such workspace."""
    page = await queue.history(tenant_id, limit=limit, offset=offset)
    access.billing_read(
        actor=staff.user,
        resource="workspace_capacity_reductions",
        tenant_id=tenant_id,
        returned=len(page.items),
    )
    return Page(items=page.items, total=page.total, limit=limit, offset=offset)


# ---------------------------------------------------- workspace channels


@router.get("/tenants/{tenant_id}/channel-capacity", response_model=ChannelCapacityRead)
async def read_workspace_channel_capacity(
    tenant_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    session: SessionDep,
    settings: SettingsDep,
) -> ChannelCapacityRead:
    """The workspace's `GET /billing/channel-capacity`, as its members see it (PLAT-G3)."""
    await _require_workspace(session, tenant_id)
    result = await channel_capacity_view.read_channel_capacity(session, tenant_id, settings)
    access.billing_read(actor=staff.user, resource="channel_capacity", tenant_id=tenant_id)
    return result


@router.get("/tenants/{tenant_id}/channel-connections", response_model=list[ChannelConnectionRead])
async def list_workspace_channel_connections(
    tenant_id: uuid.UUID,
    staff: PlatformStaffDep,
    access: PlatformAccessAuditDep,
    session: SessionDep,
    settings: SettingsDep,
    channel: Annotated[Channel | None, Query(description="Only this channel's.")] = None,
) -> list[ChannelConnectionRead]:
    """The workspace's `GET /channel-connections`, as its members see it (PLAT-G3).

    Never a credential: the tenant's schema carries none.
    """
    await _require_workspace(session, tenant_id)
    result = await channel_capacity_view.list_channel_connections(
        session, tenant_id, settings, channel=channel
    )
    access.billing_read(
        actor=staff.user,
        resource="channel_connections",
        tenant_id=tenant_id,
        returned=len(result),
    )
    return result


# ----------------------------------------------------------- vocabulary


@router.get("/channel-types", response_model=list[ChannelTypeRead])
async def list_channel_types(
    staff: PlatformStaffDep, access: PlatformAccessAuditDep, channels: ChannelRegistryDep
) -> list[ChannelTypeRead]:
    """Every channel label a plan, a top-up or a grant may name, and which are operable (PLAT-G5).

    `operable`: this deployment registered an adapter, so a workspace can
    connect one. The rest can be written into a plan, a product or a grant
    today and become usable when their adapter ships.
    """
    result = [
        ChannelTypeRead(
            channel=channel,
            operable=channels.state_for(channel) is not ChannelState.UNAVAILABLE,
            state=channels.state_for(channel),
        )
        for channel in Channel
    ]
    access.billing_read(actor=staff.user, resource="channel_types", returned=len(result))
    return result
