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
from app.core.rate_limit import (
    LOGIN_ACCOUNT_POLICY,
    RateLimiter,
    RateLimitPolicy,
    account_identity,
)
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
from app.db.models.audit import AuditAction, AuditActorKind
from app.repositories import (
    MembershipRepository,
    TenantRepository,
    UserMembershipRepository,
    UserRepository,
)
from app.services.audit_service import AuditTrail
from app.services.email_verification_service import EmailVerificationService
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
        limiter: RateLimiter | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._token_store = token_store
        # Optional so a unit test can construct this service without Redis. When
        # absent the per-account login limit simply does not run - the route
        # always supplies one, and `tests/integration/test_auth_hardening.py`
        # asserts that it does.
        self._limiter = limiter
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
        await self._request_email_verification(user=user)

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

    async def _request_email_verification(self, *, user: User) -> None:
        """Issue the new account's first verification code, in this transaction.

        The challenge and its outbox row are written on the same session as the
        user, the workspace and the membership, so registration is one unit:
        a signup that rolls back leaves no challenge and mails nobody, and a
        signup that succeeds has its code already on the way. That is the whole
        transactional-outbox point (ADR-042), applied to the one message a new
        account is certain to want.

        The account is created **unverified** and stays that way until the code
        comes back. Nothing about the session issued below depends on that: an
        unverified account signs in and uses every route normally
        (docs/EMAIL_VERIFICATION.md). Registration does not, and must not,
        become a flow that returns a code or blocks on one.

        Rate limiting is deliberately not applied. `EmailVerificationService`
        limits the *endpoint*, where an account can ask repeatedly; this path
        runs exactly once per account, and counting it would spend part of the
        budget a person needs for the resend they are most likely to want
        seconds later. `POST /auth/register` carries its own client-address
        limit, which is what bounds signup-driven mail.

        Failures are contained for `_start_subscription`'s reason, and the same
        judgement: a signup that 500s because a verification code could not be
        queued is a worse outcome than an account whose first code has to be
        requested from the endpoint that exists for it. `ValidationError` is
        what a deployment with an out-of-range lifetime raises, and that is a
        configuration fault to log rather than a reason to refuse customers.
        """
        try:
            service = EmailVerificationService(session=self._session, settings=self._settings)
            await service.request(user=user)
        except ValidationError:
            logger.warning(
                "email_verification.registration_skipped",
                extra={
                    "event": "email_verification.registration_skipped",
                    "user_id": str(user.id),
                },
            )

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
            # `self_service=False`: this is the platform putting a new workspace
            # on the plan an operator configured as the default, not a customer
            # choosing from the catalogue. A deployment whose default plan is
            # private is making a deliberate choice, and registration should not
            # start failing because of it.
            await SubscriptionService(self._session, tenant_id=tenant_id).start(
                plan_code=code,
                self_service=False,
            )
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
        # Counted before anything is looked up, so the budget is spent whether
        # or not the address exists - which is what keeps this from becoming an
        # enumeration oracle of its own - and so a password sprayer cannot buy
        # extra attempts by rotating source addresses. This is the limit that
        # survives a botnet; the per-address one counts the attacker's machines.
        await self._limit_by_account(email)

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

    async def _limit_by_account(self, email: str) -> None:
        """Refuse a login that has already used up this account's attempts.

        Raises `RateLimitedError` (429) rather than an authentication error, and
        that distinction is deliberate even though it tells a caller the limit
        exists: the alternative is answering 401 to somebody whose own account
        is under attack, which would send a real user to reset a password that
        was never compromised.
        """
        if self._limiter is None or not self._settings.rate_limit_enabled:
            return
        policy = RateLimitPolicy(
            name=LOGIN_ACCOUNT_POLICY,
            limit=self._settings.rate_limit_login_per_account_per_minute,
            window_seconds=60,
            # In front of a credential, so a Redis outage must not mean
            # unlimited attempts (ADR-040).
            local_fallback=True,
        )
        await self._limiter.enforce(policy, account_identity(email))

    async def refresh(
        self,
        *,
        refresh_token: str,
        workspace_slug: str | None = None,
    ) -> AuthenticatedSession:
        """Exchange a refresh token for a new pair, rotating the old one out.

        The order here is the security property, not an implementation detail
        (ADR-039).

        **The token is spent before anything else happens.** `spend` is a single
        atomic write that reports whether this caller was the first to present
        it. Checking a denylist and then writing to it is a race: two requests
        carrying the same token both read "unspent" and both get a fresh pair,
        which is exactly what a stolen token used alongside the real one looks
        like. Spending first means one of the two loses whatever the
        interleaving, and losing *is* the detection.

        **Losing tears the estate down.** A refresh token presented twice is
        either a leak being replayed or a client bug, and there is no way to
        tell which from here. Rotation alone would only spend whichever copy
        arrived - usually the victim's, since the thief is the one racing - so
        the response is to raise `token_version`, invalidating every access and
        refresh token the account holds. Both parties are signed out; the real
        person signs in again, and the thief has nothing left.
        """
        claims = decode_token(
            refresh_token,
            settings=self._settings,
            expected_type=TokenType.REFRESH,
        )

        # Spend first. Everything below this line is reached only by a caller
        # that was the first to present this token.
        first = await self._token_store.spend(
            claims.token_id,
            ttl_seconds=claims.seconds_until_expiry,
        )
        if not first:
            await self._tear_down_after_reuse(claims.subject)
            raise AuthenticationError(INVALID_CREDENTIALS)

        user = await self._users.get_by_id(claims.subject)
        if user is None or not user.is_active:
            raise AuthenticationError(INVALID_CREDENTIALS)

        if claims.token_version != user.token_version:
            # Revocation (ADR-036). A version-stale token is not a replay: it is
            # a token that was valid and has since been invalidated wholesale -
            # by a password change, a sign-out-everywhere, or an earlier
            # teardown. Refusing is enough, and bumping again would punish the
            # holder of a merely old session for something already handled.
            logger.warning(
                "auth.revoked_refresh_token_presented",
                extra={
                    "event": "auth.revoked_refresh_token_presented",
                    "user_id": str(user.id),
                },
            )
            raise AuthenticationError(INVALID_CREDENTIALS)

        workspace = await self._resolve_workspace(user=user, workspace_slug=workspace_slug)
        return self._issue(user=user, workspace=workspace)

    async def _tear_down_after_reuse(self, user_id: uuid.UUID) -> None:
        """End every session this account holds, and record why.

        Committed here rather than left to the request, and that is the whole
        reason this method exists. The caller raises immediately afterwards, and
        an exception rolls the request's transaction back - so a teardown staged
        the ordinary way would be undone by the very refusal it accompanies. The
        revocation has to outlive the failed request, because the failed request
        is what triggered it.

        The increment is a single ``UPDATE ... RETURNING``. Two replays arriving
        together therefore raise the version twice rather than racing to write
        the same number, and neither can leave the account on a version that an
        outstanding token still matches.

        Nothing here touches token material. The audit entry names the account
        and the new version; the token that was replayed is identified nowhere,
        because an audit log is read by people and a log of credentials is a
        second copy of them.
        """
        version = await self._users.bump_token_version(user_id)
        if version is None:
            # No such account. The token was signed by us, so this is a deleted
            # user rather than a forgery - there is nothing left to revoke, and
            # nothing to record it against.
            logger.warning(
                "auth.refresh_token_reused_for_unknown_account",
                extra={"event": "auth.refresh_token_reused_for_unknown_account"},
            )
            return

        # Loaded after the update so the entry carries the address a person
        # reading the trail will recognise, and so the in-memory row does not
        # disagree with the column that was just written.
        user = await self._users.get_by_id(user_id)
        if user is not None:
            await self._session.refresh(user)
        AuditTrail(self._session).record(
            AuditAction.REFRESH_TOKEN_REUSED,
            actor=user,
            # The account did not do this; something holding its credential did.
            # Recorded as a system observation so the trail does not read as an
            # action the person took.
            actor_kind=AuditActorKind.SYSTEM,
            target_type="user",
            target_id=user_id,
            target_label=user.email if user is not None else None,
            meta={"token_version": version},
        )
        await self._session.commit()

        logger.warning(
            "auth.refresh_token_reused",
            extra={
                "event": "auth.refresh_token_reused",
                "user_id": str(user_id),
                "token_version": version,
            },
        )

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
            token_version=user.token_version,
        )
        refresh_token, _ = create_refresh_token(
            settings=self._settings,
            subject=user.id,
            token_version=user.token_version,
        )
        return AuthenticatedSession(
            user=user,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._settings.access_token_ttl_seconds,
            workspace=workspace,
        )
