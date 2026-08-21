"""Lead and CRM endpoints.

Every route is workspace-scoped through the active workspace dependency, so a
lead id belonging to another workspace answers not-found rather than revealing
that it exists elsewhere.

Most of these are operational: anyone staffing a pipeline may read leads, create
them, edit their details, move them through the stages and write notes.

Two are not. Assignment decides who owns a revenue opportunity, and the
statistics view reports across every rep's pipeline at once - both are
management actions, so both require an administrator. This is a different line
from the one drawn on conversations, where any member may assign: grabbing an
unanswered conversation is triage, while handing someone a deal is not.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import ActiveWorkspaceDep, LeadServiceDep, TenantAdminDep
from app.core.pagination import MAX_CURSOR_LENGTH
from app.db.models.lead import LeadSource, LeadStatus
from app.repositories.lead_repository import LeadFilters
from app.schemas.conversation import CursorPage
from app.schemas.lead import (
    MAX_TAG_LENGTH,
    LeadActivityRead,
    LeadAssignmentRequest,
    LeadCreateRequest,
    LeadNoteRead,
    LeadNoteRequest,
    LeadRead,
    LeadScoreRequest,
    LeadStatisticsRead,
    LeadStatusRequest,
    LeadUpdateRequest,
)

router = APIRouter(prefix="/leads", tags=["leads"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]
CursorQuery = Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)]
# Bounded so a long query string is rejected before it reaches a LIKE pattern.
SearchQuery = Annotated[str | None, Query(min_length=1, max_length=200)]
TagQuery = Annotated[list[str] | None, Query(max_length=10)]


def _filters(
    *,
    statuses: list[LeadStatus] | None,
    sources: list[LeadSource] | None,
    assigned_to_id: uuid.UUID | None,
    unassigned: bool,
    tags: list[str] | None,
    search: str | None,
    contact_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
) -> LeadFilters:
    return LeadFilters(
        statuses=tuple(statuses or ()),
        sources=tuple(sources or ()),
        assigned_to_id=assigned_to_id,
        unassigned_only=unassigned,
        tags=tuple(tag.strip().lower()[:MAX_TAG_LENGTH] for tag in (tags or []) if tag.strip()),
        search=search,
        contact_id=contact_id,
        conversation_id=conversation_id,
    )


@router.get("", response_model=CursorPage[LeadRead])
async def list_leads(
    leads: LeadServiceDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
    status_filter: Annotated[list[LeadStatus] | None, Query(alias="status")] = None,
    source: Annotated[list[LeadSource] | None, Query()] = None,
    assigned_to_id: uuid.UUID | None = None,
    unassigned: bool = False,
    tag: TagQuery = None,
    search: SearchQuery = None,
    contact_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> CursorPage[LeadRead]:
    """Leads in this workspace, newest first.

    Filters intersect: asking for `status=new&status=contacted&unassigned=true`
    returns leads that are both. Paged by cursor rather than offset, because a
    pipeline is written to while it is being read.
    """
    page = await leads.list_leads(
        filters=_filters(
            statuses=status_filter,
            sources=source,
            assigned_to_id=assigned_to_id,
            unassigned=unassigned,
            tags=tag,
            search=search,
            contact_id=contact_id,
            conversation_id=conversation_id,
        ),
        limit=limit,
        cursor=cursor,
    )
    return CursorPage[LeadRead](
        items=[LeadRead.from_model(lead) for lead in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/statistics", response_model=LeadStatisticsRead)
async def lead_statistics(
    workspace: TenantAdminDep,
    leads: LeadServiceDep,
    status_filter: Annotated[list[LeadStatus] | None, Query(alias="status")] = None,
    source: Annotated[list[LeadSource] | None, Query()] = None,
    assigned_to_id: uuid.UUID | None = None,
    unassigned: bool = False,
    tag: TagQuery = None,
    search: SearchQuery = None,
) -> LeadStatisticsRead:
    """Pipeline counts across the workspace. Administrators only.

    Declared before `/{lead_id}` so the literal path wins the match; otherwise
    "statistics" would be parsed as a lead id and answer 422.
    """
    statistics = await leads.statistics(
        filters=_filters(
            statuses=status_filter,
            sources=source,
            assigned_to_id=assigned_to_id,
            unassigned=unassigned,
            tags=tag,
            search=search,
            contact_id=None,
            conversation_id=None,
        )
    )
    return LeadStatisticsRead.from_statistics(statistics)


@router.post("", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreateRequest,
    workspace: ActiveWorkspaceDep,
    leads: LeadServiceDep,
) -> LeadRead:
    """Create a lead by hand.

    Every field supplied is marked human-verified, so a later AI extraction
    fills in what is missing without overwriting what was typed here.

    Answers 409 if the customer already has an open lead: a second one would
    split the history of a single opportunity across two records.
    """
    lead = await leads.create_lead(
        actor_id=workspace.user.id,
        source=payload.source,
        contact_id=payload.contact_id,
        conversation_id=payload.conversation_id,
        name=payload.name,
        phone=payload.phone,
        email=payload.email,
        interest=payload.interest,
        budget_amount=payload.budget_amount,
        budget_currency=payload.budget_currency,
        assigned_to_id=payload.assigned_to_id,
        tags=payload.tags,
        custom_fields=payload.custom_fields,
    )
    return LeadRead.from_model(lead)


@router.get("/{lead_id}", response_model=LeadRead)
async def get_lead(lead_id: uuid.UUID, leads: LeadServiceDep) -> LeadRead:
    return LeadRead.from_model(await leads.get_lead(lead_id))


@router.patch("/{lead_id}", response_model=LeadRead)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdateRequest,
    workspace: ActiveWorkspaceDep,
    leads: LeadServiceDep,
) -> LeadRead:
    """Edit a lead's details.

    Omitted fields are left alone; fields sent as null are cleared. Anything
    touched here becomes human-verified and is protected from later extraction.
    """
    lead = await leads.update_lead(
        lead_id=lead_id,
        actor_id=workspace.user.id,
        update=payload.to_update(),
    )
    return LeadRead.from_model(lead)


@router.post("/{lead_id}/status", response_model=LeadRead)
async def change_status(
    lead_id: uuid.UUID,
    payload: LeadStatusRequest,
    workspace: ActiveWorkspaceDep,
    leads: LeadServiceDep,
) -> LeadRead:
    """Move the lead through the pipeline.

    Answers 422 for a move the pipeline does not allow - a won lead reopening,
    for instance. Setting the status it already has succeeds and changes
    nothing, so a retried request is safe.
    """
    lead = await leads.change_status(
        lead_id=lead_id,
        status=payload.status,
        actor_id=workspace.user.id,
        reason=payload.reason,
    )
    return LeadRead.from_model(lead)


@router.post("/{lead_id}/assignment", response_model=LeadRead)
async def assign_lead(
    lead_id: uuid.UUID,
    payload: LeadAssignmentRequest,
    workspace: TenantAdminDep,
    leads: LeadServiceDep,
) -> LeadRead:
    """Assign to a member of this workspace, or clear it. Administrators only."""
    lead = await leads.assign(
        lead_id=lead_id,
        assigned_to_id=payload.assigned_to_id,
        actor_id=workspace.user.id,
    )
    return LeadRead.from_model(lead)


@router.post("/{lead_id}/score", response_model=LeadRead)
async def set_score(
    lead_id: uuid.UUID,
    payload: LeadScoreRequest,
    workspace: ActiveWorkspaceDep,
    leads: LeadServiceDep,
) -> LeadRead:
    lead = await leads.set_score(
        lead_id=lead_id,
        score=payload.score,
        actor_id=workspace.user.id,
    )
    return LeadRead.from_model(lead)


@router.get("/{lead_id}/notes", response_model=CursorPage[LeadNoteRead])
async def list_notes(
    lead_id: uuid.UUID,
    leads: LeadServiceDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
) -> CursorPage[LeadNoteRead]:
    """Internal notes, most recent first. Never shown to the customer."""
    page = await leads.list_notes(lead_id=lead_id, limit=limit, cursor=cursor)
    return CursorPage[LeadNoteRead](
        items=[LeadNoteRead.from_model(note) for note in page.items],
        next_cursor=page.next_cursor,
    )


@router.post(
    "/{lead_id}/notes",
    response_model=LeadNoteRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_note(
    lead_id: uuid.UUID,
    payload: LeadNoteRequest,
    workspace: ActiveWorkspaceDep,
    leads: LeadServiceDep,
) -> LeadNoteRead:
    note = await leads.add_note(
        lead_id=lead_id,
        body=payload.body,
        author_id=workspace.user.id,
    )
    return LeadNoteRead.from_model(note)


@router.get("/{lead_id}/activity", response_model=CursorPage[LeadActivityRead])
async def list_activity(
    lead_id: uuid.UUID,
    leads: LeadServiceDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
) -> CursorPage[LeadActivityRead]:
    """What happened to this lead, most recent first.

    Append-only: there is no route that edits or removes an entry, because an
    audit trail the application can rewrite does not answer the question it
    exists to answer.
    """
    page = await leads.list_activity(lead_id=lead_id, limit=limit, cursor=cursor)
    return CursorPage[LeadActivityRead](
        items=[LeadActivityRead.from_model(activity) for activity in page.items],
        next_cursor=page.next_cursor,
    )
