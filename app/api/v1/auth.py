"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import AccountServiceDep, AuthServiceDep, CurrentUserDep
from app.api.rate_limits import AuthRateLimit
from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep, SettingsDep
from app.db.models import User
from app.schemas.auth import (
    AccessTokenResponse,
    AccountStateResponse,
    LoginRequest,
    LogoutRequest,
    PasswordChangeRequest,
    ProfileResponse,
    RefreshRequest,
    RegistrationRequest,
    SessionResponse,
    WorkspaceSummary,
    WorkspaceSwitchRequest,
)
from app.schemas.password_reset import (
    PasswordResetConfirmPayload,
    PasswordResetRequestedResponse,
    PasswordResetRequestPayload,
)
from app.services.auth_service import AuthenticatedSession, WorkspaceContext
from app.services.password_reset_service import (
    RESET_REQUESTED_MESSAGE,
    PasswordResetService,
)

router = APIRouter(route_class=CommittingRoute, prefix="/auth", tags=["Authentication"])


def _summarise(workspace: WorkspaceContext) -> WorkspaceSummary:
    return WorkspaceSummary(
        id=workspace.tenant.id,
        name=workspace.tenant.name,
        slug=workspace.tenant.slug,
        role=workspace.membership.role,
    )


def _session_response(result: AuthenticatedSession) -> SessionResponse:
    return SessionResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        active_workspace=_summarise(result.workspace) if result.workspace else None,
    )


@router.post(
    "/register",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and its first workspace",
)
async def register(
    payload: RegistrationRequest,
    service: AuthServiceDep,
    # Counted per client address: a caller creating an account has no
    # identity yet, so where the request came from is all there is.
    limit: AuthRateLimit,
) -> SessionResponse:
    result = await service.register(
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        workspace_name=payload.workspace_name,
        workspace_slug=payload.workspace_slug,
    )
    return _session_response(result)


@router.post(
    "/login",
    response_model=SessionResponse,
    summary="Exchange credentials for a token pair",
)
async def login(
    payload: LoginRequest,
    service: AuthServiceDep,
    limit: AuthRateLimit,
) -> SessionResponse:
    result = await service.login(
        email=payload.email,
        password=payload.password,
        workspace_slug=payload.workspace_slug,
    )
    return _session_response(result)


@router.post(
    "/refresh",
    response_model=SessionResponse,
    summary="Rotate a refresh token for a new pair",
)
async def refresh(
    payload: RefreshRequest,
    service: AuthServiceDep,
    # Limited too. A refresh token is a credential, and a script holding a
    # stolen one should not get unlimited attempts to find a live workspace.
    limit: AuthRateLimit,
) -> SessionResponse:
    result = await service.refresh(
        refresh_token=payload.refresh_token,
        workspace_slug=payload.workspace_slug,
    )
    return _session_response(result)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    # Stated rather than inferred. Under postponed annotations FastAPI resolves
    # a `-> None` return to NoneType, which it then treats as a real response
    # model and rejects against a 204.
    response_model=None,
    summary="Revoke a refresh token",
)
async def logout(
    payload: LogoutRequest,
    service: AuthServiceDep,
    # Limited by client address, and deliberately *not* authenticated.
    #
    # Requiring an access token would break logout exactly when people use it:
    # the access token has expired, which is why they are signing out rather
    # than continuing. And it would add nothing against the adversary it looks
    # like it guards - somebody holding a victim's refresh token can already
    # exchange it for a live session, which is strictly worse than revoking it.
    #
    # What was actually missing is a budget. The endpoint decodes a JWT for any
    # caller, so it was a free endpoint doing signature work, and the limit is
    # the proportionate answer (ADR-040).
    limit: AuthRateLimit,
) -> None:
    """Revoke a refresh token. No credential beyond the token itself.

    Presenting a token that is already spent, revoked or expired is not an
    error: logging out twice is a thing clients do, and answering differently
    would turn this into an oracle for whether a token is still live.
    """
    await service.logout(refresh_token=payload.refresh_token)


@router.post(
    "/workspace",
    response_model=AccessTokenResponse,
    summary="Switch the active workspace",
)
async def switch_workspace(
    payload: WorkspaceSwitchRequest,
    current_user: CurrentUserDep,
    service: AuthServiceDep,
) -> AccessTokenResponse:
    result = await service.select_workspace(
        user=current_user.user,
        workspace_slug=payload.workspace_slug,
    )
    return AccessTokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        active_workspace=_summarise(result.workspace),
    )


@router.get(
    "/me",
    response_model=ProfileResponse,
    summary="Describe the authenticated caller",
)
async def me(current_user: CurrentUserDep, service: AuthServiceDep) -> ProfileResponse:
    workspaces = await service.list_workspaces(user=current_user.user)
    user = current_user.user
    return ProfileResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        email_verified_at=user.email_verified_at,
        platform_role=user.platform_role,
        workspaces=[_summarise(workspace) for workspace in workspaces],
    )


def _account_state(user: User) -> AccountStateResponse:
    return AccountStateResponse(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        token_version=user.token_version,
    )


@router.post(
    "/logout-all",
    response_model=AccountStateResponse,
    summary="Sign out of every session",
)
async def logout_everywhere(
    current_user: CurrentUserDep,
    accounts: AccountServiceDep,
) -> AccountStateResponse:
    """End every session this account holds, including the one calling (ADR-036).

    The self-service half of revocation, and the reason it is not behind an
    administrator: somebody who thinks a token leaked needs to act now, and
    should not have to lose their account to do it. Signing in again immediately
    afterwards is expected - the account is untouched, only its sessions end.

    The token used to make this call is invalidated too. Exempting it would
    leave the one session an attacker is most likely to be holding.
    """
    user = await accounts.revoke_sessions(user=current_user.user)
    return _account_state(user)


@router.post(
    "/password",
    response_model=AccountStateResponse,
    summary="Change the password, ending every session",
)
async def change_password(
    payload: PasswordChangeRequest,
    current_user: CurrentUserDep,
    accounts: AccountServiceDep,
    # Limited by client address like the rest of the credential surface: the
    # current password is guessable in principle, and this route verifies one.
    limit: AuthRateLimit,
) -> AccountStateResponse:
    """Replace the password, proving the current one first.

    Not a reset. A reset serves somebody who *cannot* sign in and needs a token
    sent to an address they control, which this deployment has no way to send -
    see docs/SECURITY.md. This serves somebody already signed in, so the proof
    is the password itself.

    Every session ends on success, this one included, because the usual reason
    to change a password is that something may have been taken.
    """
    user = await accounts.change_password(
        user=current_user.user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return _account_state(user)


@router.post(
    "/password-reset/request",
    response_model=PasswordResetRequestedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a password reset link by email",
)
async def request_password_reset(
    payload: PasswordResetRequestPayload,
    session: SessionDep,
    settings: SettingsDep,
    # Counted per client address with the process-local Redis fallback, like
    # the rest of the credential surface (ADR-040): this route stands in
    # front of a credential and in front of somebody else's inbox.
    limit: AuthRateLimit,
) -> PasswordResetRequestedResponse:
    """One answer for every address, whatever the database says.

    202 with the same body whether the address is registered, unknown,
    suspended or passwordless - the response must not be an oracle for which
    addresses have accounts (docs/SECURITY.md). The token travels only in
    the email; nothing about it appears here.
    """
    service = PasswordResetService(session=session, settings=settings)
    await service.request(email=payload.email)
    return PasswordResetRequestedResponse(detail=RESET_REQUESTED_MESSAGE)


@router.post(
    "/password-reset/confirm",
    response_model=AccountStateResponse,
    summary="Redeem a reset token for a new password",
)
async def confirm_password_reset(
    payload: PasswordResetConfirmPayload,
    session: SessionDep,
    settings: SettingsDep,
    limit: AuthRateLimit,
) -> AccountStateResponse:
    """Set a new password, proving ownership with the emailed token.

    Single use, 30-minute expiry, and every session ends on success - a
    reset exists because something may have been taken. Unknown, expired,
    superseded and replayed tokens all receive the same refusal.
    """
    service = PasswordResetService(session=session, settings=settings)
    user = await service.confirm(
        raw_token=payload.token,
        new_password=payload.new_password,
    )
    return _account_state(user)
