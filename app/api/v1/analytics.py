"""Analytics endpoints.

Open to every member of the workspace, unlike usage. These are the numbers that
tell somebody staffing an inbox how the inbox is doing - how many customers went
unanswered, how many were unhappy, how often the agent needed a person - and
restricting them to administrators would hide the team's own work from the team.

Nothing here is a per-customer view: every figure is an aggregate over a window,
and the one endpoint that returns rows returns them for a single conversation
the caller already has access to.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import ActiveWorkspaceDep, AnalyticsServiceDep, InboxServiceDep
from app.schemas.analytics import AnalyticsEventRead, TenantAnalyticsRead

router = APIRouter(prefix="/analytics", tags=["analytics"])

SinceQuery = Annotated[datetime | None, Query(description="Start of the window, inclusive (UTC)")]
UntilQuery = Annotated[datetime | None, Query(description="End of the window, exclusive (UTC)")]
LimitQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("", response_model=TenantAnalyticsRead)
async def workspace_analytics(
    workspace: ActiveWorkspaceDep,
    analytics: AnalyticsServiceDep,
    since: SinceQuery = None,
    until: UntilQuery = None,
) -> TenantAnalyticsRead:
    """Conversations, messages, leads, sentiment and campaigns for a window.

    Rates come back alongside the counts they were computed from, never instead
    of them: a rate on its own cannot be checked and hides the difference
    between nine of ten and nine hundred of a thousand.
    """
    report = await analytics.report(since=since, until=until)
    return TenantAnalyticsRead.from_report(report)


@router.get(
    "/conversations/{conversation_id}/events",
    response_model=list[AnalyticsEventRead],
)
async def conversation_events(
    conversation_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    analytics: AnalyticsServiceDep,
    inbox: InboxServiceDep,
    limit: LimitQuery = 50,
) -> list[AnalyticsEventRead]:
    """Why this conversation ended up where it did, newest first.

    The conversation is resolved first, through the inbox service, so another
    workspace's id answers not-found rather than an empty list - an empty list
    would confirm the id exists.
    """
    await inbox.get_conversation(conversation_id)
    events = await analytics.conversation_history(conversation_id, limit=limit)
    return [AnalyticsEventRead.from_model(event) for event in events]
