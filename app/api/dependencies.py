"""Authentication and authorization dependencies.

The tenant a request acts on is read from the signed access token, never from a
path, query or body parameter. There is therefore no request field a caller
could forge to aim a route at another workspace's data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.dependencies import RedisDep, SessionDep, SettingsDep
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.security import TokenClaims, TokenType, decode_token
from app.core.token_store import RefreshTokenStore
from app.db.models import Membership, PlatformRole, Tenant, TenantRole, User
from app.repositories import MembershipRepository, TenantRepository, UserRepository
from app.services.auth_service import AuthService
from app.services.invitation_service import InvitationService

# auto_error is off so a missing header raises the same domain error as a bad
# one, and every failure leaves through the same response envelope.
bearer_scheme = HTTPBearer(auto_error=False, description="Access token")

BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def get_auth_service(
    settings: SettingsDep,
    session: SessionDep,
    redis: RedisDep,
) -> AuthService:
    return AuthService(
        session=session,
        settings=settings,
        token_store=RefreshTokenStore(redis),
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_invitation_service(session: SessionDep) -> InvitationService:
    return InvitationService(session=session)


InvitationServiceDep = Annotated[InvitationService, Depends(get_invitation_service)]


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated caller and the token they presented."""

    user: User
    claims: TokenClaims


async def get_current_user(
    settings: SettingsDep,
    session: SessionDep,
    credentials: BearerDep,
) -> CurrentUser:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authentication is required.")

    claims = decode_token(
        credentials.credentials,
        settings=settings,
        expected_type=TokenType.ACCESS,
    )
    user = await UserRepository(session).get_by_id(claims.subject)
    if user is None or not user.is_active:
        # The token is still signed and unexpired, but the account behind it is
        # gone or disabled, so it stops working now rather than at expiry.
        raise AuthenticationError("The credentials are not valid.")
    return CurrentUser(user=user, claims=claims)


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


@dataclass(frozen=True, slots=True)
class ActiveWorkspace:
    """The workspace a request is scoped to, and the caller's standing in it."""

    user: User
    membership: Membership
    tenant: Tenant

    @property
    def role(self) -> TenantRole:
        return self.membership.role


async def get_active_workspace(
    current_user: CurrentUserDep,
    session: SessionDep,
) -> ActiveWorkspace:
    """Resolve the workspace named by the token, re-checking membership.

    The membership is loaded on every request rather than trusted from the
    token, so withdrawing someone's access takes effect at once instead of
    whenever their access token happens to expire.
    """
    tenant_id = current_user.claims.tenant_id
    if tenant_id is None:
        raise PermissionDeniedError("No workspace is selected for this session.")

    memberships = MembershipRepository(session, tenant_id=tenant_id)
    membership = await memberships.require_for_user(current_user.user.id)

    tenant = await TenantRepository(session).get_by_id(tenant_id)
    if tenant is None or not tenant.is_active:
        raise PermissionDeniedError("This workspace is not available.")
    return ActiveWorkspace(
        user=current_user.user,
        membership=membership,
        tenant=tenant,
    )


ActiveWorkspaceDep = Annotated[ActiveWorkspace, Depends(get_active_workspace)]


def require_tenant_roles(*roles: TenantRole) -> Callable[[ActiveWorkspace], ActiveWorkspace]:
    """Build a dependency that admits only these workspace roles.

    Authorization is a dependency rather than a check inside the handler, so a
    route cannot be written that forgets it.
    """
    allowed = frozenset(roles)

    def guard(workspace: ActiveWorkspaceDep) -> ActiveWorkspace:
        if workspace.role not in allowed:
            raise PermissionDeniedError("This action requires a different role in this workspace.")
        return workspace

    return guard


def require_platform_roles(*roles: PlatformRole) -> Callable[[CurrentUser], CurrentUser]:
    """Build a dependency for platform administration.

    Platform authority is deliberately separate from workspace roles: owning a
    workspace grants nothing across the platform.
    """
    allowed = frozenset(roles)

    def guard(current_user: CurrentUserDep) -> CurrentUser:
        if current_user.user.platform_role not in allowed:
            raise PermissionDeniedError("This action requires platform administration rights.")
        return current_user

    return guard


TenantOwnerDep = Annotated[
    ActiveWorkspace,
    Depends(require_tenant_roles(TenantRole.TENANT_OWNER)),
]
TenantAdminDep = Annotated[
    ActiveWorkspace,
    Depends(require_tenant_roles(TenantRole.TENANT_OWNER, TenantRole.TENANT_ADMIN)),
]
PlatformStaffDep = Annotated[
    CurrentUser,
    Depends(require_platform_roles(PlatformRole.PLATFORM_OWNER, PlatformRole.PLATFORM_ADMIN)),
]
PlatformOwnerDep = Annotated[
    CurrentUser,
    Depends(require_platform_roles(PlatformRole.PLATFORM_OWNER)),
]
