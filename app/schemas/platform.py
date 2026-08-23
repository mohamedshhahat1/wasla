"""Platform administration contracts.

Deliberately built from the same `UsageCounters` a workspace sees, so the number
an operator quotes to a customer and the number that customer sees on their own
dashboard are the same number, computed the same way.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel

from app.db.models.enums import TenantStatus
from app.db.models.tenant import Tenant
from app.platform.platform_analytics import PlatformOverview, WorkspacePage, WorkspaceRow
from app.schemas.usage import UsageCounters, UsageSummaryRead, WindowRead
from app.services.usage_service import UsageSummary


def _counters(summary: UsageSummary) -> UsageCounters:
    return UsageSummaryRead.from_summary(summary).counters


class TenantSummaryRead(BaseModel):
    """One workspace as the platform lists it."""

    id: str
    name: str
    slug: str
    status: TenantStatus
    created_at: datetime

    @classmethod
    def from_model(cls, tenant: Tenant) -> Self:
        return cls(
            id=str(tenant.id),
            name=tenant.name,
            slug=tenant.slug,
            status=tenant.status,
            created_at=tenant.created_at,
        )


class WorkspaceUsageRead(BaseModel):
    """A workspace and what it consumed in the window."""

    tenant: TenantSummaryRead
    counters: UsageCounters

    @classmethod
    def from_row(cls, row: WorkspaceRow) -> Self:
        return cls(
            tenant=TenantSummaryRead.from_model(row.tenant),
            counters=_counters(row.usage),
        )


class WorkspacePageRead(BaseModel):
    """A page of workspaces.

    `total` is the number matching the filter, not the number returned, so an
    operator can page through a search without guessing where it ends.
    """

    window: WindowRead
    total: int
    items: list[WorkspaceUsageRead]

    @classmethod
    def from_page(cls, page: WorkspacePage) -> Self:
        return cls(
            window=WindowRead.from_window(page.window),
            total=page.total,
            items=[WorkspaceUsageRead.from_row(row) for row in page.rows],
        )


class PlatformOverviewRead(BaseModel):
    """The estate at a glance.

    No revenue figures, and none are coming until there are subscriptions to
    compute them from (Phase 13). A plausible zero on a dashboard is worse than
    an absent field, because somebody eventually believes it.
    """

    window: WindowRead
    tenants_total: int
    tenants_active: int
    tenants_suspended: int
    whatsapp_numbers: int
    whatsapp_numbers_active: int
    usage: UsageCounters

    @classmethod
    def from_overview(cls, overview: PlatformOverview) -> Self:
        return cls(
            window=WindowRead.from_window(overview.window),
            tenants_total=overview.tenants_total,
            tenants_active=overview.tenants_active,
            tenants_suspended=overview.tenants_suspended,
            whatsapp_numbers=overview.whatsapp_numbers,
            whatsapp_numbers_active=overview.whatsapp_numbers_active,
            usage=_counters(overview.usage),
        )


__all__ = [
    "PlatformOverviewRead",
    "TenantSummaryRead",
    "WorkspacePageRead",
    "WorkspaceUsageRead",
]
