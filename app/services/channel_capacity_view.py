"""A workspace's channel capacity and connections, as one computation for every reader.

`GET /billing/channel-capacity` and `GET /channel-connections` show a workspace
its own channel picture; `GET /platform/billing/tenants/{id}/channel-capacity`
and `.../channel-connections` show platform staff the same picture (PLAT-G3,
ADR-132). Both call these functions, so support staff can never see a
different number from the one the customer sees - not a second
implementation that could forget the typed slots or a pending reduction.

Nothing here exposes a credential: a connection is read through
`ChannelConnectionRead`, which carries its channel, its external account id,
its state and how it was disabled, and nothing a provider issued.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.billing import LimitKey
from app.db.models.channel import Channel
from app.repositories.billing_repository import SubscriptionRepository
from app.schemas.billing import ChannelCapacityBreakdown
from app.schemas.channel_capacity import (
    CapacityReductionRead,
    ChannelCapacityRead,
    FallbackPreviewRead,
    scheduled_read,
)
from app.schemas.channel_connection import ChannelConnectionRead
from app.services.capacity_reduction import ChannelCapacityReductions, needs_reduction
from app.services.channel_connection_service import ChannelConnectionService
from app.services.entitlement_service import EntitlementService


def capacity_reductions(
    session: AsyncSession, tenant_id: uuid.UUID, settings: Settings, now: datetime
) -> ChannelCapacityReductions:
    """The reduction lifecycle of one workspace, on this deployment's grace and clock."""
    return ChannelCapacityReductions(
        session,
        tenant_id=tenant_id,
        default_plan_code=settings.default_plan_code,
        grace=timedelta(days=settings.channel_capacity_grace_days),
        clock=lambda: now,
    )


async def read_channel_capacity(
    session: AsyncSession, tenant_id: uuid.UUID, settings: Settings
) -> ChannelCapacityRead:
    """The capacity view, read fresh - after a selection, its disables included."""
    now = datetime.now(UTC)
    entitlements = EntitlementService(
        session,
        tenant_id=tenant_id,
        default_plan_code=settings.default_plan_code,
        clock=lambda: now,
    )
    state = await entitlements.check(LimitKey.CHANNEL_CONNECTIONS, additional=0)
    capacity = await entitlements.channel_capacity(at=now)
    reductions = capacity_reductions(session, tenant_id, settings, now)
    open_reduction = await reductions.open_reduction()
    subscription = await SubscriptionRepository(session, tenant_id=tenant_id).get()
    ahead = await entitlements.scheduled_channel_capacity()
    preview = await reductions.fallback_preview(now=now)
    return ChannelCapacityRead(
        effective_limit=state.limit,
        base_limit=state.base_limit,
        topup_limit=state.topup_limit,
        platform_grant_limit=state.grant_limit,
        active=capacity.active_total,
        remaining=state.remaining,
        over_limit=state.over_limit,
        breakdown=ChannelCapacityBreakdown.from_capacity(capacity),
        scheduled=(
            scheduled_read(
                ahead,
                effective_at=subscription.current_period_end,
                fits=not needs_reduction(ahead, capacity.active),
            )
            if ahead is not None and subscription is not None
            else None
        ),
        reduction=(
            CapacityReductionRead.from_model(open_reduction) if open_reduction is not None else None
        ),
        automatic_fallback=(
            FallbackPreviewRead(
                keep=[connection.id for connection in preview.keep],
                disable=[connection.id for connection in preview.disable],
            )
            if preview is not None
            else None
        ),
        preselected=await reductions.preselected(),
        selection_revision=(
            open_reduction.revision
            if open_reduction is not None
            else subscription.revision if subscription is not None else None
        ),
    )


async def list_channel_connections(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    settings: Settings,
    *,
    channel: Channel | None = None,
) -> list[ChannelConnectionRead]:
    """Every connection the workspace holds - active and disabled - newest first."""
    connections = await ChannelConnectionService(
        session, tenant_id=tenant_id, default_plan_code=settings.default_plan_code
    ).list_connections(channel=channel)
    return [ChannelConnectionRead.from_model(row) for row in connections]


__all__ = ["capacity_reductions", "list_channel_connections", "read_channel_capacity"]
