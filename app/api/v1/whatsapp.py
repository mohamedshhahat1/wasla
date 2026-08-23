"""WhatsApp account connection endpoints.

Connecting, disabling and releasing are workspace-administration actions;
listing is open to any member. The workspace comes from the access token, so no
payload here carries a tenant field a caller could point elsewhere.

Connecting additionally requires proof: the credential in the request is checked
against Meta for the number being claimed before anything is written (ADR-037).
A request that cannot be verified is refused, and no row is created - so a
failed claim cannot be used to squat a number either.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.dependencies import (
    ActiveWorkspaceDep,
    NumberSlotDep,
    TenantAdminDep,
    WhatsAppAccountServiceDep,
)
from app.api.route import CommittingRoute
from app.db.models import WhatsAppAccountStatus
from app.schemas.whatsapp import (
    WhatsAppAccountConnectRequest,
    WhatsAppAccountListResponse,
    WhatsAppAccountResponse,
)

router = APIRouter(route_class=CommittingRoute, prefix="/whatsapp/accounts", tags=["WhatsApp"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Connect a WhatsApp Business number",
    responses={
        409: {"description": "Another workspace already holds this number."},
        422: {"description": "Control of the number could not be proven with this credential."},
    },
)
async def connect_account(
    payload: WhatsAppAccountConnectRequest,
    workspace: TenantAdminDep,
    service: WhatsAppAccountServiceDep,
    slot: NumberSlotDep,
) -> WhatsAppAccountResponse:
    account = await service.connect(
        # Named, so the trail says who claimed this number rather than only
        # that it appeared.
        actor=workspace.user,
        tenant_id=workspace.tenant.id,
        phone_number_id=payload.phone_number_id,
        # Checked against Meta, then encrypted by the service if this
        # deployment can store it. The plaintext goes no further than this
        # call, and no response model can return it.
        access_token=payload.access_token,
        waba_id=payload.waba_id,
        display_name=payload.display_name,
    )
    return WhatsAppAccountResponse.model_validate(account)


@router.get("", summary="List connected numbers")
async def list_accounts(
    workspace: ActiveWorkspaceDep,
    service: WhatsAppAccountServiceDep,
) -> WhatsAppAccountListResponse:
    accounts = await service.list_accounts(tenant_id=workspace.tenant.id)
    return WhatsAppAccountListResponse(
        accounts=[WhatsAppAccountResponse.model_validate(account) for account in accounts]
    )


# Disable and enable are named transitions rather than a general PATCH on
# status: status is the only field with an operational meaning, and naming the
# transition keeps the audit trail readable.
@router.post("/{account_id}/disable", summary="Stop accepting and sending traffic")
async def disable_account(
    account_id: uuid.UUID,
    workspace: TenantAdminDep,
    service: WhatsAppAccountServiceDep,
) -> WhatsAppAccountResponse:
    account = await service.set_status(
        actor=workspace.user,
        tenant_id=workspace.tenant.id,
        account_id=account_id,
        status=WhatsAppAccountStatus.DISABLED,
    )
    return WhatsAppAccountResponse.model_validate(account)


@router.post("/{account_id}/enable", summary="Resume traffic")
async def enable_account(
    account_id: uuid.UUID,
    workspace: TenantAdminDep,
    service: WhatsAppAccountServiceDep,
) -> WhatsAppAccountResponse:
    account = await service.set_status(
        actor=workspace.user,
        tenant_id=workspace.tenant.id,
        account_id=account_id,
        status=WhatsAppAccountStatus.ACTIVE,
    )
    return WhatsAppAccountResponse.model_validate(account)


@router.post(
    "/{account_id}/release",
    summary="Give the number up so it can be claimed elsewhere",
)
async def release_account(
    account_id: uuid.UUID,
    workspace: TenantAdminDep,
    service: WhatsAppAccountServiceDep,
) -> WhatsAppAccountResponse:
    """Hand a number back, keeping its history.

    Distinct from disabling, which pauses traffic while the workspace keeps the
    claim. Releasing frees the number for another workspace to prove and claim,
    and cannot be undone from here: taking it back means proving control of it
    again, at the same bar anybody else would have to clear.

    A POST rather than a DELETE, because nothing is deleted. The row - and every
    conversation and message hanging off it - stays exactly where it was.
    """
    account = await service.release(
        actor=workspace.user,
        tenant_id=workspace.tenant.id,
        account_id=account_id,
    )
    return WhatsAppAccountResponse.model_validate(account)
