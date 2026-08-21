"""Follow-up endpoints.

Every route is workspace-scoped through the active workspace dependency, so a
follow-up id belonging to another workspace answers not-found.

These are operational: any member may see what is scheduled, add one and cancel
one. A follow-up is a message to a customer the member is already handling, and
requiring an administrator to cancel one would mean the person watching the
conversation cannot stop a nudge they can see is now wrong.

There is no route that edits a follow-up in place. Scheduling one for a
conversation that already has a pending nudge replaces it, which is the same
operation with fewer ways to get it wrong.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import ActiveWorkspaceDep, FollowUpServiceDep
from app.core.pagination import MAX_CURSOR_LENGTH
from app.db.models.follow_up import FollowUpStatus
from app.schemas.conversation import CursorPage
from app.schemas.follow_up import (
    FollowUpCancelRequest,
    FollowUpCreateRequest,
    FollowUpRead,
)

router = APIRouter(prefix="/follow-ups", tags=["follow-ups"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]
CursorQuery = Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)]


@router.get("", response_model=CursorPage[FollowUpRead])
async def list_follow_ups(
    follow_ups: FollowUpServiceDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
    status_filter: Annotated[list[FollowUpStatus] | None, Query(alias="status")] = None,
    conversation_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
) -> CursorPage[FollowUpRead]:
    """Scheduled follow-ups, soonest first."""
    page = await follow_ups.list_follow_ups(
        statuses=tuple(status_filter or ()),
        conversation_id=conversation_id,
        lead_id=lead_id,
        limit=limit,
        cursor=cursor,
    )
    return CursorPage[FollowUpRead](
        items=[FollowUpRead.from_model(follow_up) for follow_up in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("", response_model=FollowUpRead, status_code=status.HTTP_201_CREATED)
async def schedule_follow_up(
    payload: FollowUpCreateRequest,
    workspace: ActiveWorkspaceDep,
    follow_ups: FollowUpServiceDep,
) -> FollowUpRead:
    """Schedule a follow-up, or replace the one already waiting.

    Answers 201 either way. A conversation holds at most one pending follow-up,
    so scheduling a second reschedules the first rather than queueing another
    message at the same customer.
    """
    follow_up = await follow_ups.schedule(
        conversation_id=payload.conversation_id,
        delay=(
            timedelta(minutes=payload.delay_minutes) if payload.delay_minutes is not None else None
        ),
        scheduled_at=payload.scheduled_at,
        body=payload.body,
        template_name=payload.template_name,
        template_language=payload.template_language,
        template_components=payload.template_components,
        reason=payload.reason,
        lead_id=payload.lead_id,
        created_by_id=workspace.user.id,
    )
    return FollowUpRead.from_model(follow_up)


@router.get("/{follow_up_id}", response_model=FollowUpRead)
async def get_follow_up(
    follow_up_id: uuid.UUID,
    follow_ups: FollowUpServiceDep,
) -> FollowUpRead:
    return FollowUpRead.from_model(await follow_ups.get(follow_up_id))


@router.post("/{follow_up_id}/cancel", response_model=FollowUpRead)
async def cancel_follow_up(
    follow_up_id: uuid.UUID,
    payload: FollowUpCancelRequest,
    follow_ups: FollowUpServiceDep,
) -> FollowUpRead:
    """Cancel a scheduled follow-up.

    Cancelling one that has already been sent or cancelled succeeds and changes
    nothing: losing that race is not the caller's mistake, and their intent — no
    further nudge — already holds.
    """
    follow_up = await follow_ups.cancel(follow_up_id=follow_up_id, reason=payload.reason)
    return FollowUpRead.from_model(follow_up)
