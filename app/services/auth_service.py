"""Authentication and session issuance."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    AuthenticationError,
    PermissionDeniedError,
    TenantIsolationError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    spend_verification_time,
    validate_password_strength,
    verify_password,
)
from app.core.token_store import RefreshTokenStore
from app.db.models import Membership, Tenant, TenantRole, User
from app.repositories import (
    MembershipRepository,
    TenantRepository,
    UserMembershipRepository,
    UserRepository,
)
from app.services.subscription_service import SubscriptionService

logger = get_logger(__name__)

# One answer for every credential failure. Which half was wrong is not
# something an unauthenticated caller may learn.
INVALID_CREDENTIALS: Final = "The email address or password is incorrect."


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """A workspace and the caller's standing in it."""

    membership: Membership
    tenant: Tenant


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """Result of a successful registration, login or refresh."""

    user: User
    access_token: str
    refresh_token: str
    expires_in: int
    workspace: WorkspaceContext | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceAccess:
    """A fresh access token scoped to another workspace."""

    access_token: str
    expires_in: int
    workspace: WorkspaceContext


class AuthService:
    """Registration, login, refresh, logout and workspace selection.

    The service owns no transaction. The request-scoped session commits when the
    request succeeds, so a half-finished registration cannot be left behind.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        token_store: RefreshTokenStore,
    ) -> None:
        self._session = session
        self._settings = settings
        self._token_store = token_store
        self._users = UserRepository(session)
        self._tenants = TenantRepository(session)
        self._memberships = UserMembershipRepository(session)

    async def register(
        self,
        *,
        email: str,
        password: str,
        workspace_name: str,
        workspace_slug: str,
        full_name: str | None = None,
    ) -> AuthenticatedSession:
        """Create an account, its first workspace, and an owner membership."""
        validate_password_strength(password)
        user = await self._users.create(
            email=email,
            full_name=full_name,
            hashed_password=hash_password(password),
        )
        tenant = await self._tenants.create(name=workspace_name, slug=workspace_slug)
        # Identifiers are assigned on flush, and the membership needs both.
        await self._session.flush()

        memberships = MembershipRepository(self._session, tenant_id=tenant.id)
        membership = await memberships.add_member(
            user_id=user.id,
            role=TenantRole.TENANT_OWNER,
        )
        await self._session.flush()

        await self._start_subscription(tenant_id=tenant.id)

        logger.info(
            "auth.registered",
            extra={
                "event": "auth.registered",
                "tenant_id": str(tenant.id),
                "user_id": str(user.id),
            },
        )
        workspace = WorkspaceContext(membership=membership, tenant=tenant)
        return self._issue(user=user, workspace=workspace)

    async def _start_subscription(self, *, tenant_id: uuid.UUID) -> None:
        """Put the new workspace on the default plan, if there is one.

        Registration must not fail because a catalogue row is missing. A
        workspace without a subscription is still entitled to the default plan
        by the same code (ADR-029), so the worst case is a missing row rather
        than a customer who cannot sign up - and a signup that 500s over
        billing configuration is the least forgivable failure in the product.
        """
        code = self._settings.default_plan_code
        if not code:
            return
        try:
            await SubscriptionService(self._session, tenant_id=tenant_id).start(plan_code=code)
        except ValidationError:
            logger.warning(
                "billing.default_plan_missing",
                extra={
                    "event": "billing.default_plan_missing",
                    "tenant_id": str(tenant_id),
                    "plan_code": code,
                },
            )

    async def login(
        self,
        *,
        email: str,
        password: str,
        workspace_slug: str | None = None,
    ) -> AuthenticatedSession:
        user = await self._users.get_by_email(email)
        if user is None or user.hashed_password is None:
            # Spend the time a real verification would: response time discloses
            # whether an address is registered just as a message would.
            spend_verification_time(password)
            raise AuthenticationError(INVALID_CREDENTIALS)

        if not verify_password(password=password, password_hash=user.hashed_password):
            raise AuthenticationError(INVALID_CREDENTIALS)

        # Only after the password is proven: telling this caller that their own
        # account is disabled reveals nothing they did not already know.
        if not user.is_active:
            raise PermissionDeniedError("This account has been disabled.")

        if password_needs_rehash(user.hashed_password):
            # Cost parameters have been raised since this hash was made.
            user.hashed_password = hash_password(password)

        workspace = await self._resolve_workspace(user=user, workspace_slug=workspace_slug)
        logger.info(
            "auth.logged_in",
            extra={"event": "auth.logged_in", "user_id": str(user.id)},
        )
        return self._issue(user=user, workspace=workspace)

    async def refresh(
        self,
        *,
        refresh_token: str,
        workspace_slug: str | None = None,
    ) -> AuthenticatedSession:
        """Exchange a refresh token for a new pair, rotating the old one out."""
        claims = decode_token(
            refresh_token,
            settings=self._settings,
            expected_type=TokenType.REFRESH,
        )
        if await self._token_store.is_revoked(claims.token_id):
            # A spent token being presented means it leaked, or someone is
            # replaying it. Either way nothing is issued.
            logger.warning(
                "auth.spent_refresh_token_presented",
                extra={
                    "event": "auth.spent_refresh_token_presented",
                    "user_id": str(claims.subject),
                },
            )
            raise AuthenticationError(INVALID_CREDENTIALS)

        user = await self._users.get_by_id(claims.subject)
        if user is None or not user.is_active:
            raise AuthenticationError(INVALID_CREDENTIALS)

        # Rotation: the presented token is spent immediately, so a stolen copy
        # stops working as soon as the real holder refreshes once.
        await self._token_store.revoke(
            claims.token_id,
            ttl_seconds=claims.seconds_until_expiry,
        )

        workspace = await self._resolve_workspace(user=user, workspace_slug=workspace_slug)
        return self._issue(user=user, workspace=workspace)

    async def logout(self, *, refresh_token: str) -> None:
        """Revoke a refresh token.

        Access tokens are left to expire on their own. Logging out twice, or
        with a token that is already unusable, is not an error.
        """
        try:
            claims = decode_token(
                refresh_token,
                settings=self._settings,
                expected_type=TokenType.REFRESH,
            )
        except AuthenticationError:
            return
        await self._token_store.revoke(
            claims.token_id,
            ttl_seconds=claims.seconds_until_expiry,
        )

    async def select_workspace(self, *, user: User, workspace_slug: str) -> WorkspaceAccess:
        """Mint an access token for another workspace this user belongs to.

        The refresh token is untouched, so moving between workspaces never
        disturbs the long-lived credential.
        """
        workspace = await self._resolve_workspace(user=user, workspace_slug=workspace_slug)
        if workspace is None:
            raise TenantIsolationError()
        access_token, _ = create_access_token(
            settings=self._settings,
            subject=user.id,
            tenant_id=workspace.tenant.id,
        )
        return WorkspaceAccess(
            access_token=access_token,
            expires_in=self._settings.access_token_ttl_seconds,
            workspace=workspace,
        )

    async def list_workspaces(self, *, user: User) -> list[WorkspaceContext]:
        """Every workspace this user may open.

        One lookup per membership. People belong to a handful of workspaces, so
        this stays cheap; if that ever stops being true it becomes a join.
        """
        contexts: list[WorkspaceContext] = []
        for membership in await self._memberships.list_for_user(user.id):
            tenant = await self._tenants.get_by_id(membership.tenant_id)
            if tenant is not None and tenant.is_active:
                contexts.append(WorkspaceContext(membership=membership, tenant=tenant))
        return contexts

    async def _resolve_workspace(
        self,
        *,
        user: User,
        workspace_slug: str | None,
    ) -> WorkspaceContext | None:
        """Decide which workspace a new session opens.

        A user with no membership still gets a session: platform staff and
        newly invited people exist before they belong anywhere.
        """
        memberships = await self._memberships.list_for_user(user.id)
        if not memberships:
            return None

        if workspace_slug is None:
            # Oldest membership first: the workspace they started with.
            for membership in memberships:
                tenant = await self._tenants.get_by_id(membership.tenant_id)
                if tenant is not None and tenant.is_active:
                    return WorkspaceContext(membership=membership, tenant=tenant)
            return None

        tenant = await self._tenants.get_by_slug(workspace_slug)
        # Named apart from the loop variable above: reusing that name would fix
        # this one's type as non-optional and hide the None the search can give.
        requested = next(
            (entry for entry in memberships if tenant and entry.tenant_id == tenant.id),
            None,
        )
        if tenant is None or requested is None:
            # One answer for "no such workspace" and "you are not in it".
            # Distinguishing them would let anyone map which workspaces exist.
            raise TenantIsolationError()
        if not tenant.is_active:
            raise PermissionDeniedError("This workspace is suspended.")
        return WorkspaceContext(membership=requested, tenant=tenant)

    def _issue(self, *, user: User, workspace: WorkspaceContext | None) -> AuthenticatedSession:
        access_token, _ = create_access_token(
            settings=self._settings,
            subject=user.id,
            tenant_id=workspace.tenant.id if workspace else None,
        )
        refresh_token, _ = create_refresh_token(settings=self._settings, subject=user.id)
        return AuthenticatedSession(
            user=user,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._settings.access_token_ttl_seconds,
            workspace=workspace,
        )
