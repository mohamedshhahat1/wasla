"""Whether Wasla still sends for a workspace: one definition, every automated sender.

An automated message - an agent's follow-up, a campaign copy - is a WhatsApp
message to a real customer that nobody is watching leave. The platform stops
serving a workspace by suspending it or by its owner deleting it, and from that
moment nothing automated may go out on its behalf (TOOL-04, PD-TOOLS-06, CMP-02).

The predicate used to live inside `FollowUpService`, which is why campaigns
never asked it: a suspended or soft-deleted workspace went on broadcasting
(CAMPAIGNS_OUTBOUND_MESSAGING_AUDIT.md, CMP-02). It lives here so both senders
ask the same question and cannot drift into two definitions of "served".

Columns rather than the mapped row, for the reason `app.agents.lifecycle`
gives: the tenant may already be in the session's identity map with the
attributes it was loaded with, and what this needs is the row as it is now.
Retention decides when a deleted workspace's data is erased, not whether it is
served in the meantime.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import TenantStatus
from app.db.models.tenant import Tenant


class WorkspaceServing(StrEnum):
    """Why a workspace is, or is not, one Wasla sends for."""

    SERVED = "served"
    SUSPENDED = "suspended"
    DELETED = "deleted"
    #: The row is gone: purged, or never existed.
    GONE = "gone"


async def workspace_serving(session: AsyncSession, tenant_id: uuid.UUID) -> WorkspaceServing:
    """The workspace's lifecycle as it is committed now."""
    row = (
        await session.execute(
            select(Tenant.status, Tenant.deleted_at)
            .where(Tenant.id == tenant_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return WorkspaceServing.GONE
    status, deleted_at = row
    if deleted_at is not None:
        return WorkspaceServing.DELETED
    if status is not TenantStatus.ACTIVE:
        return WorkspaceServing.SUSPENDED
    return WorkspaceServing.SERVED


async def workspace_is_served(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """Whether this workspace is still one Wasla sends for.

    Suspension and soft deletion both stop service. The one predicate
    follow-ups (`FollowUpService.dispatch`) and campaigns
    (`CampaignService.dispatch_batch`) are gated on.
    """
    return await workspace_serving(session, tenant_id) is WorkspaceServing.SERVED


__all__ = ["WorkspaceServing", "workspace_is_served", "workspace_serving"]
