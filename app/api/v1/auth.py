"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import (
    AccountServiceDep,
    AuthServiceDep,
    CurrentUserDep,
    VerifiedUserDep,
)
from app.api.rate_limits import AuthRateLimit, RateLimiterDep
from app.api.route import CommittingRoute
from app.core.dependencies import SessionDep, SettingsDep
from app.db.models import User
from app.schemas.auth import (
    AccessTokenResponse,
    AccountDeleteRequest,
    AccountStateResponse,
    LoginRequest,
    LogoutRequest,
    PasswordChangeRequest,
    PasswordSetRequest,
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
    current_user: VerifiedUserDep,
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
        avatar_url=user.avatar_url,
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
    sent to an address they control, which `/auth/password-reset/request` does.
    This serves somebody already signed in, so the proof is the password itself
    - and `/auth/password/set` serves an account that has never had one.

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
    "/password/set",
    response_model=AccountStateResponse,
    summary="Choose a first password for an account that has none",
)
async def set_password(
    payload: PasswordSetRequest,
    current_user: CurrentUserDep,
    accounts: AccountServiceDep,
    # Limited by client address like the rest of the credential surface. This
    # one writes a credential rather than verifying one, so the budget is about
    # bounding automation rather than guessing.
    limit: AuthRateLimit,
) -> AccountStateResponse:
    """Set the first password on this account. Authenticated, and self only.

    Who this is for: somebody who signed up with Google. That account is
    created without a password hash on purpose, and every other route refuses
    to give it one - `/auth/password` needs a current password to prove, and a
    reset declines passwordless accounts rather than becoming an oracle for
    which addresses have Google accounts. Disconnecting Google is refused while
    no password exists, with a message telling the person to set one; this is
    that route (ADR-057).

    The session is the proof, so no current password is asked for and none
    exists to ask for. An account that already has one is refused: this is not
    a second way to replace a password without knowing it.

    Every session ends on success, this one included, exactly as
    `/auth/password` behaves - acquiring a credential is a credential change,
    and the response carries the new token version so a client knows to sign in
    again.
    """
    user = await accounts.set_password(
        user=current_user.user,
        new_password=payload.new_password,
    )
    return _account_state(user)


@router.delete(
    "/me",
    response_model=AccountStateResponse,
    summary="Close this account permanently",
    responses={
        401: {"description": "The current password is incorrect."},
        409: {"description": ("The account has no password to prove, or still owns workspaces.")},
    },
)
async def delete_account(
    payload: AccountDeleteRequest,
    current_user: CurrentUserDep,
    accounts: AccountServiceDep,
    # Counted per client address like the rest of the credential surface: this
    # route verifies a password, so it is guessable in principle, and what it
    # does on success cannot be undone.
    limit: AuthRateLimit,
) -> AccountStateResponse:
    """Close your own account. The caller is always the target.

    `DELETE` on the same path `GET /auth/me` describes, because it is the same
    resource: the authenticated account. There is no `user_id` anywhere in the
    request, which is what distinguishes this from `DELETE /platform/users/{id}`
    - that one is platform staff acting on somebody else and requires a platform
    role. **A workspace owner or administrator has no route to another person's
    Wasla identity at all**, and this is not one: removing somebody from a
    workspace is `DELETE /workspace/members/{user_id}` and reaches only their
    membership.

    **Proof beyond the session is required, and either kind will do**: the
    current password, or a single-use `reauthentication_token` from a completed
    Google re-authentication (`POST /auth/google/reauth/callback`). A session is
    not proof - a stolen access token is a session - and this is the one action
    that cannot be undone.

    Either, never both. Requiring both from an account that has both would be
    step-up MFA, which is a product decision nobody has made, and it would make
    the more securely configured account the harder one to close. An account
    with no password and no Google identity cannot reach this route at all,
    which is not a state the product can currently produce.

    Neither supplied is `409 reauthentication_required`, naming the thing to go
    and do rather than the thing that is absent.

    **Workspaces are resolved first.** If the account is the last active owner of
    any live workspace, the response is 409 `account_owns_workspaces` carrying
    those workspaces, and the person must transfer ownership or delete each one
    before this succeeds. Deleting the account anyway would leave those
    workspaces with nobody able to invite an owner or close them.

    On success every session ends immediately, the identity is tombstoned, every
    membership is withdrawn, and neither a password nor Google will ever open
    the account again. The address is not released: it stays on the tombstoned
    row, so it cannot be re-registered and cannot be used to claim the deleted
    account's history. Nobody else's account is touched.
    """
    user = await accounts.delete_self(
        user=current_user.user,
        current_password=payload.current_password,
        reauthentication_token=payload.reauthentication_token,
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
    limiter: RateLimiterDep,
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
    service = PasswordResetService(session=session, settings=settings, limiter=limiter)
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
