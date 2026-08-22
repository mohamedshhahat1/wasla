"""WhatsApp account connection endpoints.

Connecting and disabling are workspace-administration actions; listing is open
to any member. The workspace comes from the access token, so no payload here
carries a tenant field a caller could point elsewhere.
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
from app.db.models import WhatsAppAccountStatus
from app.schemas.whatsapp import (
    WhatsAppAccountConnectRequest,
    WhatsAppAccountListResponse,
    WhatsAppAccountResponse,
)

router = APIRouter(prefix="/whatsapp/accounts", tags=["WhatsApp"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Connect a WhatsApp Business number",
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
        waba_id=payload.waba_id,
        display_phone_number=payload.display_phone_number,
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
