"""Workspace membership endpoints.

The removal route is guarded by `ActiveWorkspaceDep` rather than by
`TenantAdminDep`, and that is not an oversight. Leaving a workspace needs no
permission, so a member must be able to call it - on themselves. The role rules
for removing *somebody else* live in the service, where they can see who the
target is, which a dependency evaluated before the path parameter is bound
cannot (ADR-038).

Everything else here is administration and is guarded the ordinary way.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query

from app.api.dependencies import (
    ActiveWorkspaceDep,
    MembershipServiceDep,
    SeatDep,
    TenantAdminDep,
)
from app.api.route import CommittingRoute
from app.schemas.membership import (
    MemberListResponse,
    MemberReinstateRequest,
    MemberResponse,
)
from app.services.membership_service import MemberView

router = APIRouter(route_class=CommittingRoute, prefix="/workspace/members", tags=["Team"])


def _response(view: MemberView) -> MemberResponse:
    return MemberResponse(
        id=view.membership.id,
        user_id=view.user.id,
        email=view.user.email,
        full_name=view.user.full_name,
        role=view.membership.role,
        status=view.membership.status,
        joined_at=view.membership.created_at,
        revoked_at=view.membership.revoked_at,
    )


@router.get("", summary="List the people in this workspace")
async def list_members(
    workspace: ActiveWorkspaceDep,
    service: MembershipServiceDep,
    include_revoked: bool = Query(
        default=False,
        description="Include people whose access has been withdrawn.",
    ),
) -> MemberListResponse:
    """Open to any member: knowing who your colleagues are is not privileged.

    Removed people are excluded unless asked for, because a member list that
    silently counts them is how somebody ends up believing a former colleague
    still has access.
    """
    members = await service.list_members(include_revoked=include_revoked)
    return MemberListResponse(members=[_response(view) for view in members])


@router.delete(
    "/{user_id}",
    summary="Withdraw somebody's access to this workspace",
    responses={
        403: {"description": "The caller may not remove this person."},
        409: {"description": "Already removed, or this would leave no owner."},
    },
)
async def remove_member(
    user_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    service: MembershipServiceDep,
) -> MemberResponse:
    """Remove a member, or leave the workspace by naming yourself.

    Takes effect on the caller's next request: authorization loads the
    membership every time rather than trusting the token, so nothing has to
    expire. It does not touch the person's account or their access to any other
    workspace - being removed from one company is not a reason to be signed out
    of another.
    """
    membership = await service.revoke(
        actor=workspace.user,
        actor_role=workspace.role,
        user_id=user_id,
    )
    members = await service.list_members(include_revoked=True)
    view = next(entry for entry in members if entry.membership.id == membership.id)
    return _response(view)


@router.post(
    "/{user_id}/reinstate",
    summary="Readmit somebody who was removed",
    responses={
        403: {"description": "The caller may not grant this role."},
        409: {"description": "That person is already a member."},
    },
)
async def reinstate_member(
    user_id: uuid.UUID,
    payload: MemberReinstateRequest,
    workspace: TenantAdminDep,
    service: MembershipServiceDep,
    # A readmission fills a seat exactly as an invitation does, so it is
    # counted the same way. Otherwise removing and reinstating would be a way
    # around the plan's team-size limit.
    seat: SeatDep,
) -> MemberResponse:
    """Give access back without an invitation round trip.

    Useful precisely when an invitation is not: somebody removed by mistake, or
    somebody returning whose email no longer reaches them.
    """
    membership = await service.reinstate(
        actor=workspace.user,
        actor_role=workspace.role,
        user_id=user_id,
        role=payload.role,
    )
    members = await service.list_members(include_revoked=True)
    view = next(entry for entry in members if entry.membership.id == membership.id)
    return _response(view)
