"""Usage endpoints.

Reading usage is an administrator's action, unlike reading conversations or
leads. It is the input to a bill and the thing a plan limit is enforced against,
so it sits with billing rather than with the inbox: everyone who staffs a
pipeline can see the pipeline, but what the workspace is spending is the
business owner's to see.

Both endpoints take an optional window. Given neither bound they report the last
thirty days, and the window they actually applied comes back in the response -
a figure without its period is not quotable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import TenantAdminDep, UsageServiceDep
from app.db.models.usage import UsageEventType
from app.schemas.usage import UsageSeriesRead, UsageSummaryRead

router = APIRouter(prefix="/usage", tags=["usage"])

SinceQuery = Annotated[datetime | None, Query(description="Start of the window, inclusive (UTC)")]
UntilQuery = Annotated[datetime | None, Query(description="End of the window, exclusive (UTC)")]
MeterQuery = Annotated[list[UsageEventType] | None, Query(alias="event_type")]


@router.get("", response_model=UsageSummaryRead)
async def usage_summary(
    workspace: TenantAdminDep,
    usage: UsageServiceDep,
    since: SinceQuery = None,
    until: UntilQuery = None,
) -> UsageSummaryRead:
    """Everything this workspace consumed in the window. Administrators only."""
    summary = await usage.summary(since=since, until=until)
    return UsageSummaryRead.from_summary(summary)


@router.get("/daily", response_model=UsageSeriesRead)
async def usage_series(
    workspace: TenantAdminDep,
    usage: UsageServiceDep,
    since: SinceQuery = None,
    until: UntilQuery = None,
    event_type: MeterQuery = None,
) -> UsageSeriesRead:
    """A daily point per meter, for drawing. Administrators only.

    Sparse: a day on which nothing happened has no point, because only the
    caller knows whether it is drawing bars or a cumulative line.
    """
    window, points = await usage.series(since=since, until=until, event_types=event_type)
    return UsageSeriesRead.from_points(window, points)
