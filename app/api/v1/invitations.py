"""Workspace invitation endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from app.api.dependencies import InvitationServiceDep, SeatDep, TenantAdminDep
from app.api.rate_limits import auth_rate_limit, workspace_rate_limit
from app.api.route import CommittingRoute
from app.db.models import TenantInvitation
from app.schemas.auth import WorkspaceSummary
from app.schemas.invitation import (
    InvitationAcceptedResponse,
    InvitationAcceptRequest,
    InvitationCreateRequest,
    InvitationResponse,
)

router = APIRouter(route_class=CommittingRoute, prefix="/invitations", tags=["Invitations"])

# Declared per route rather than on the router, because `/accept` must stay
# reachable without credentials and the workspace guard resolves the whole
# authentication chain. See the note in `app/api/v1/__init__.py`.
_WORKSPACE_LIMIT = Depends(workspace_rate_limit)
_CLIENT_LIMIT = Depends(auth_rate_limit)


def _response(invitation: TenantInvitation) -> InvitationResponse:
    return InvitationResponse(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        status=invitation.status,
        expires_at=invitation.expires_at,
        accepted_at=invitation.accepted_at,
    )


@router.post(
    "",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite somebody to this workspace",
    dependencies=[_WORKSPACE_LIMIT],
)
async def issue_invitation(
    payload: InvitationCreateRequest,
    workspace: TenantAdminDep,
    service: InvitationServiceDep,
    # Checked when the invitation is issued rather than when it is accepted:
    # refusing somebody who was invited in good faith, at the moment they click,
    # is a worse experience than telling the inviter now.
    seat: SeatDep,
) -> InvitationResponse:
    """Create an invitation and mail it. The token is not returned (ADR-057).

    `issue` still hands the raw token back to this layer, because the service
    is what queues the email and the caller is what commits it. It stops here:
    the only place it travels is the outbox row addressed to the invited
    mailbox, which is the proof of ownership the whole flow rests on.
    """
    invitation, _token = await service.issue(
        tenant_id=workspace.tenant.id,
        inviter=workspace.user,
        inviter_role=workspace.role,
        email=payload.email,
        role=payload.role,
    )
    return _response(invitation)


@router.get(
    "",
    response_model=list[InvitationResponse],
    summary="List invitations still awaiting acceptance",
    dependencies=[_WORKSPACE_LIMIT],
)
async def list_invitations(
    workspace: TenantAdminDep,
    service: InvitationServiceDep,
) -> list[InvitationResponse]:
    invitations = await service.list_pending(tenant_id=workspace.tenant.id)
    return [_response(invitation) for invitation in invitations]


@router.delete(
    "/{invitation_id}",
    response_model=InvitationResponse,
    summary="Revoke an invitation",
    dependencies=[_WORKSPACE_LIMIT],
)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    workspace: TenantAdminDep,
    service: InvitationServiceDep,
) -> InvitationResponse:
    invitation = await service.revoke(
        tenant_id=workspace.tenant.id,
        invitation_id=invitation_id,
    )
    return _response(invitation)


@router.post(
    "/accept",
    response_model=InvitationAcceptedResponse,
    summary="Accept an invitation",
    # Unauthenticated and credential-bearing, so it is limited by client
    # address on the authentication budget: the token in the body is guessable
    # only by brute force, and this is what makes brute force expensive.
    dependencies=[_CLIENT_LIMIT],
)
async def accept_invitation(
    payload: InvitationAcceptRequest,
    service: InvitationServiceDep,
) -> InvitationAcceptedResponse:
    """Unauthenticated on purpose: the invited person may have no account yet.

    The token in the body is the authorization, and it is checked against a
    stored hash.
    """
    accepted = await service.accept(
        raw_token=payload.token,
        password=payload.password,
        full_name=payload.full_name,
    )
    return InvitationAcceptedResponse(
        email=accepted.user.email,
        workspace=WorkspaceSummary(
            id=accepted.tenant.id,
            name=accepted.tenant.name,
            slug=accepted.tenant.slug,
            role=accepted.membership.role,
        ),
    )
