"""Platform administration endpoints.

Behind `PlatformStaffDep`, which is a different authority from every other route
in this API: it is a property of the user, not of a membership. Owning a
workspace grants nothing here, and holding a platform role grants nothing
*inside* a workspace - a platform administrator reading these figures still
cannot open a customer's inbox.

Read-only, deliberately. Suspending or deleting a workspace is exactly the kind
of privileged action that must be audit-logged, and there is no audit log until
Phase 14; shipping the action first would mean a period during which the most
consequential operations in the product left no trace.

What is absent is as considered as what is here. There are no revenue figures,
because revenue is a question about subscriptions and there are none until Phase
13. A plausible zero on a dashboard is worse than an absent field.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import PlatformAnalyticsServiceDep, PlatformStaffDep
from app.db.models.enums import TenantStatus
from app.platform.platform_analytics import DEFAULT_PAGE, MAX_PAGE
from app.schemas.platform import PlatformOverviewRead, WorkspacePageRead

router = APIRouter(prefix="/platform", tags=["platform"])

SinceQuery = Annotated[datetime | None, Query(description="Start of the window, inclusive (UTC)")]
UntilQuery = Annotated[datetime | None, Query(description="End of the window, exclusive (UTC)")]
SearchQuery = Annotated[str | None, Query(min_length=1, max_length=200)]
LimitQuery = Annotated[int, Query(ge=1, le=MAX_PAGE)]
OffsetQuery = Annotated[int, Query(ge=0)]


@router.get("/overview", response_model=PlatformOverviewRead)
async def platform_overview(
    staff: PlatformStaffDep,
    analytics: PlatformAnalyticsServiceDep,
    since: SinceQuery = None,
    until: UntilQuery = None,
) -> PlatformOverviewRead:
    """Workspaces, connected numbers and platform-wide consumption for a window."""
    overview = await analytics.overview(since=since, until=until)
    return PlatformOverviewRead.from_overview(overview)


@router.get("/tenants", response_model=WorkspacePageRead)
async def platform_tenants(
    staff: PlatformStaffDep,
    analytics: PlatformAnalyticsServiceDep,
    since: SinceQuery = None,
    until: UntilQuery = None,
    search: SearchQuery = None,
    status: TenantStatus | None = None,
    limit: LimitQuery = DEFAULT_PAGE,
    offset: OffsetQuery = 0,
) -> WorkspacePageRead:
    """Workspaces and what each consumed, searchable by name or address.

    Offset paging rather than the cursors the tenant API uses: this list is
    sorted by name and searched by hand, and an operator wants page three of
    forty results rather than a stable feed. `total` is the number matching the
    filter, so paging does not have to guess where the results end.
    """
    page = await analytics.workspaces(
        since=since,
        until=until,
        search=search,
        status=status,
        limit=limit,
        offset=offset,
    )
    return WorkspacePageRead.from_page(page)
