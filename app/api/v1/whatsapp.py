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
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app.api.dependencies import (
    ActiveWorkspaceDep,
    TenantAdminDep,
    WhatsAppAccountServiceDep,
    WhatsAppOnboardingServiceDep,
)
from app.api.route import CommittingRoute
from app.core.config import Settings
from app.core.dependencies import SessionDep, SettingsDep
from app.core.exceptions import ValidationError
from app.core.timezones import local
from app.db.models import WhatsAppAccountStatus
from app.db.models.whatsapp import OnboardingMode, WhatsAppAccount
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.schemas.whatsapp import (
    EmbeddedSignupConfigRead,
    EmbeddedSignupRequestBody,
    EmbeddedSignupResponse,
    MessagingLimitRead,
    OnboardingContactsRead,
    OnboardingHistoryRead,
    OnboardingStatusRead,
    ProviderDisconnectRead,
    WhatsAppAccountConnectRequest,
    WhatsAppAccountListResponse,
    WhatsAppAccountResponse,
    WhatsAppAccountVerifyRequest,
)
from app.services.business_initiated_ledger import limit_view
from app.services.whatsapp_onboarding_service import EmbeddedSignupRequest, ReleaseFollowUp

router = APIRouter(route_class=CommittingRoute, prefix="/whatsapp/accounts", tags=["WhatsApp"])
# Embedded Signup (ADR-204): the second connect adapter beside the form above.
signup_router = APIRouter(
    route_class=CommittingRoute, prefix="/whatsapp/embedded-signup", tags=["WhatsApp"]
)

MAX_ONBOARDING_KEY_LENGTH = 255
RequiredIdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=MAX_ONBOARDING_KEY_LENGTH,
        description=(
            "One key per Embedded Signup flow. Repeating the request with the same key "
            "returns the first answer and calls Meta not at all."
        ),
    ),
]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Connect a WhatsApp Business number",
    responses={
        409: {
            "description": (
                "Another workspace already holds this number; or no channel slot is free "
                "(`channel_capacity_exceeded`); or the plan does not include WhatsApp "
                "(`channel_type_not_allowed`)."
            )
        },
        422: {"description": "Control of the number could not be proven with this credential."},
    },
)
async def connect_account(
    payload: WhatsAppAccountConnectRequest,
    workspace: TenantAdminDep,
    service: WhatsAppAccountServiceDep,
) -> WhatsAppAccountResponse:
    # The channel capacity guard runs in the service: before Meta is asked,
    # then authoritatively under the workspace's lock before the insert (ENT-08).
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


@router.post(
    "/{account_id}/enable",
    summary="Resume traffic",
    responses={
        409: {
            "description": (
                "The number freed its channel slot when it was disabled and none is free "
                "now (`channel_capacity_exceeded`), or the plan does not include WhatsApp "
                "(`channel_type_not_allowed`)."
            )
        }
    },
)
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
    "/{account_id}/verify",
    summary="Prove control of a number this workspace already holds",
    responses={
        422: {"description": "Control of the number could not be proven with this credential."},
    },
)
async def verify_account(
    account_id: uuid.UUID,
    payload: WhatsAppAccountVerifyRequest,
    workspace: TenantAdminDep,
    service: WhatsAppAccountServiceDep,
) -> WhatsAppAccountResponse:
    """Attach proof to a number claimed before proof was required (ADR-041).

    Numbers connected before ADR-037 have no `ownership_verified_at`. Without
    this route the only way to establish one is to release the number and claim
    it again - which frees it to the entire platform in between, and hands an
    attacker a race worth running. The safe-looking route was the dangerous one.

    The number itself is not a parameter: it comes from the row. This proves
    control of something the workspace already holds, and cannot move a claim.
    """
    account = await service.reverify(
        actor=workspace.user,
        tenant_id=workspace.tenant.id,
        account_id=account_id,
        access_token=payload.access_token,
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
    onboarding: WhatsAppOnboardingServiceDep,
    session: SessionDep,
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
    released = WhatsAppAccountResponse.model_validate(account)
    # An Embedded Signup number also stops its webhooks at Meta, best-effort and
    # after the release has committed (RL-02); the service read the token before
    # the release dropped it. Never the Deregister API (RL-04).
    owed = getattr(service, "release_follow_up", None)
    if owed is not None:
        await session.commit()
        token, messaging_account = owed
        await onboarding.unsubscribe(
            ReleaseFollowUp(token=token, messaging_account_id=messaging_account)
        )
    return released


@router.get(
    "/{account_id}/messaging-limit",
    response_model=MessagingLimitRead,
    summary="Meta's messaging limit for this number's business portfolio, and its use",
)
async def read_messaging_limit(
    account_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> MessagingLimitRead:
    """Any member. The portfolio's limit, the campaigns' share and what is used now (LIM-07).

    Counted across every number and workspace on the portfolio - Meta's limit
    is the portfolio's - and returned only as aggregates: nothing names
    another workspace, its numbers, its campaigns or how its sends split.
    Another workspace's number is not found.
    """
    account = await WhatsAppAccountRepository(session, tenant_id=workspace.tenant.id).require_by_id(
        account_id
    )
    view = await limit_view(
        session,
        account,
        share=settings.campaign_portfolio_limit_share,
        at=datetime.now(UTC),
    )
    return MessagingLimitRead(
        tier=view.tier,
        known=view.known,
        messaging_limit=view.messaging_limit,
        checked_at=view.checked_at,
        source=view.source,
        campaign_share=view.campaign_share,
        campaign_budget=view.campaign_budget,
        used_last_24h=view.used_last_24h,
        campaign_available_now=view.campaign_available_now,
        next_capacity_at=view.next_capacity_at,
        next_capacity_at_local=(
            local(view.next_capacity_at, workspace.tenant.timezone)
            if view.next_capacity_at is not None
            else None
        ),
        shared_with_other_numbers=view.shared_with_other_numbers,
        shared_with_other_workspaces=view.shared_with_other_workspaces,
    )


# ------------------------------------------------------------- Embedded Signup


def onboarding_view(account: WhatsAppAccount, settings: Settings) -> OnboardingStatusRead:
    """The onboarding read (OS-02): steps and times, never a secret (OS-03)."""
    disconnect = (
        ProviderDisconnectRead(
            reason=account.provider_disconnect_reason,
            initiated_by=account.provider_disconnect_initiated_by,
            at=account.provider_disconnected_at,
        )
        if account.provider_disconnected_at is not None
        else None
    )
    coexistence = account.onboarding_mode == OnboardingMode.COEXISTENCE.value
    return OnboardingStatusRead(
        connection_id=account.id,
        method=account.onboarding_method,
        mode=account.onboarding_mode,
        onboarded_at=account.onboarded_at,
        subscribed_at=account.subscribed_at,
        registered_at=account.registered_at,
        registration_needed=account.is_embedded_signup and not coexistence,
        sync_deadline_at=account.sync_deadline_at,
        contacts=OnboardingContactsRead(requested_at=account.contacts_sync_requested_at),
        history=OnboardingHistoryRead(
            requested_at=account.history_sync_requested_at,
            progress=account.history_progress,
            phase_max=account.history_phase_max,
            completed_at=account.history_completed_at,
            declined_at=account.history_declined_at,
        ),
        provider_disconnect=disconnect,
        reminder_days=settings.coexistence_reminder_days if coexistence else None,
    )


@signup_router.get(
    "/config",
    summary="What the browser needs to open Meta's Embedded Signup popup",
)
async def embedded_signup_config(
    _workspace: TenantAdminDep,
    settings: SettingsDep,
    onboarding: WhatsAppOnboardingServiceDep,
    response: Response,
) -> EmbeddedSignupConfigRead:
    """Owner and administrator (CF-03). Never the app secret, a token or the verify token.

    `enabled` is true only when this deployment can finish an onboarding: the
    app id and secret, the configuration id, a credential key and the verify
    token are all set (CF-04). Otherwise every other field is null.
    """
    # Nothing here is secret, but it is one workspace's view (CF-05).
    response.headers["Cache-Control"] = "private"
    if not onboarding.enabled:
        return EmbeddedSignupConfigRead(enabled=False)
    return EmbeddedSignupConfigRead(
        enabled=True,
        app_id=settings.meta_app_id,
        config_id=settings.meta_embedded_signup_config_id,
        es_version=settings.meta_embedded_signup_version,
        graph_version=settings.meta_onboarding_api_version,
        coexistence=settings.meta_embedded_signup_coexistence,
    )


@signup_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Connect a WhatsApp number with Meta's Embedded Signup",
    responses={
        409: {
            "description": (
                "Embedded Signup is not available here (`embedded_signup_disabled`); Meta's "
                "weekly onboarding limit is reached (`embedded_signup_onboarding_limit`); no "
                "channel slot is free (`channel_capacity_exceeded`) or the plan does not "
                "include WhatsApp (`channel_type_not_allowed`) - asked before Meta is called; "
                "another workspace holds the number; or the number is on the WhatsApp "
                "Business app and this deployment does not connect those "
                "(`embedded_signup_coexistence_unavailable`)."
            )
        },
        422: {
            "description": (
                "Meta refused the code (`embedded_signup_code_invalid`) or it expired "
                "(`embedded_signup_code_expired`); or control of the number could not be "
                "proven with the exchanged token (`whatsapp_ownership_unverified`)."
            )
        },
        502: {"description": "Meta could not be reached."},
    },
)
async def embedded_signup(
    payload: EmbeddedSignupRequestBody,
    workspace: TenantAdminDep,
    onboarding: WhatsAppOnboardingServiceDep,
    settings: SettingsDep,
    session: SessionDep,
    idempotency_key: RequiredIdempotencyKey,
    response: Response,
) -> EmbeddedSignupResponse:
    """Exchange the popup's code, prove the number, connect it (EX-01 .. EX-12).

    The code is exchanged in this request, server to server, after the plan,
    the channel slot and Meta's weekly limit have been asked. The webhook
    subscription - and a new number's registration - follow the commit; their
    failure leaves the connection written, its health saying so (SB-03).
    """
    messaging_account = payload.messaging_account_id or payload.waba_id
    if messaging_account is None:
        raise ValidationError("The Messaging account id from Meta's session is missing.")
    result = await onboarding.onboard(
        tenant_id=workspace.tenant.id,
        actor=workspace.user,
        idempotency_key=idempotency_key,
        request=EmbeddedSignupRequest(
            code=payload.code,
            phone_number_id=payload.phone_number_id,
            messaging_account_id=messaging_account,
            business_id=payload.business_id,
            is_on_biz_app_hint=payload.is_on_biz_app,
        ),
    )
    account = result.account
    if result.complete is not None:
        # The connection is durable before Meta is told to send to it.
        await session.commit()
        await result.complete()
        await session.refresh(account)
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return EmbeddedSignupResponse(
        account=WhatsAppAccountResponse.model_validate(account),
        onboarding=onboarding_view(account, settings),
    )


@router.get(
    "/{account_id}/onboarding",
    summary="How this number was connected and how far its onboarding got",
)
async def read_onboarding(
    account_id: uuid.UUID,
    workspace: ActiveWorkspaceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> OnboardingStatusRead:
    """Any member (OS-01). Another workspace's number is not found."""
    account = await WhatsAppAccountRepository(session, tenant_id=workspace.tenant.id).require_by_id(
        account_id
    )
    return onboarding_view(account, settings)


@router.post(
    "/{account_id}/subscription",
    summary="Retry the webhook subscription and registration of an Embedded Signup number",
    responses={409: {"description": "The number was not connected with Meta, or is disabled."}},
)
async def retry_onboarding_setup(
    account_id: uuid.UUID,
    workspace: TenantAdminDep,
    onboarding: WhatsAppOnboardingServiceDep,
    settings: SettingsDep,
) -> OnboardingStatusRead:
    """Owner and administrator (SB-04). Retries whichever of subscribe and register is missing."""
    account = await onboarding.retry_setup(tenant_id=workspace.tenant.id, account_id=account_id)
    return onboarding_view(account, settings)
