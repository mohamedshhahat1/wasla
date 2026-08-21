"""Workspace invitation endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.dependencies import InvitationServiceDep, TenantAdminDep
from app.db.models import TenantInvitation
from app.schemas.auth import WorkspaceSummary
from app.schemas.invitation import (
    InvitationAcceptedResponse,
    InvitationAcceptRequest,
    InvitationCreatedResponse,
    InvitationCreateRequest,
    InvitationResponse,
)

router = APIRouter(prefix="/invitations", tags=["Invitations"])


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
    response_model=InvitationCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite somebody to this workspace",
)
async def issue_invitation(
    payload: InvitationCreateRequest,
    workspace: TenantAdminDep,
    service: InvitationServiceDep,
) -> InvitationCreatedResponse:
    invitation, token = await service.issue(
        tenant_id=workspace.tenant.id,
        inviter=workspace.user,
        inviter_role=workspace.role,
        email=payload.email,
        role=payload.role,
    )
    return InvitationCreatedResponse(
        **_response(invitation).model_dump(),
        token=token,
    )


@router.get(
    "",
    response_model=list[InvitationResponse],
    summary="List invitations still awaiting acceptance",
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
