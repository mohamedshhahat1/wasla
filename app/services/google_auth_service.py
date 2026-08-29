"""Google sign-in: resolving an identity, and connecting one.

The order of operations in this module is the security design, not an
implementation detail. Two orderings in particular.

**The state is spent before the code is exchanged.** A replayed or invented
state is refused without a single outbound request, so a stranger cannot make
this process talk to Google on their schedule.

**`email_verified` is checked before any account is looked up.** This is what
lets the collision response name the collision without becoming an enumeration
oracle. Anybody who gets as far as the lookup has proven, cryptographically,
that Google considers them the owner of that mailbox - so telling them an
account exists under it tells them nothing they could not have found by trying
to reset its password. Reverse the two and the endpoint becomes a directory:
register a Google account claiming an unverified address, submit a callback,
read the answer.

And one thing this module never does: log anybody in because an address matched.
Addresses are reassigned, corporate domains change hands, and control of a
mailbox today is not evidence of who opened an account under it last year. The
Google subject is the only identity key read here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.logging import get_logger
from app.core.oauth_flow import (
    FLOW_TTL_SECONDS,
    FlowKind,
    OAuthFlow,
    OAuthFlowStore,
    code_challenge,
)
from app.db.models import FederatedIdentity, User
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.identity import IdentityProvider
from app.integrations.google.client import GoogleExchangeError, GoogleOAuthClient
from app.integrations.google.oidc import (
    GoogleIdentityClaims,
    GoogleIdTokenVerifier,
    GoogleKeysUnavailableError,
    GoogleTokenInvalidError,
)
from app.repositories.identity_repository import FederatedIdentityRepository
from app.repositories.user_repository import UserRepository, normalise_email
from app.services.audit_service import AuditTrail
from app.services.auth_service import AuthenticatedSession, AuthService

logger = get_logger(__name__)

# One answer for every way a Google authorization can fail: a bad state, a
# replayed state, a refused code, a forged token, a wrong nonce, an unknown key.
# They are one message because the differences between them are only useful to
# somebody trying to find which check to defeat next.
GOOGLE_FAILED: Final = "Google sign-in could not be completed."

# Said to somebody who has proven they own the mailbox, and only to them. It
# names the collision because that is the only way they can act on it, and
# withholding it would mean answering "sign-in failed" to a person whose account
# is sitting right there.
ADDRESS_IN_USE: Final = (
    "An account already exists for this email address. Sign in with your password, "
    "then connect Google from your account settings."
)

_PROVIDER: Final = IdentityProvider.GOOGLE


class GoogleAuthService:
    """The five things this feature does.

    Owns no transaction. `CommittingRoute` commits when the request succeeds, so
    a link that fails halfway leaves nothing behind - which is what section 19's
    "linking must be transactional" amounts to here.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        flows: OAuthFlowStore,
        client: GoogleOAuthClient,
        verifier: GoogleIdTokenVerifier,
        auth: AuthService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._flows = flows
        self._client = client
        self._verifier = verifier
        self._auth = auth
        self._users = UserRepository(session)
        self._identities = FederatedIdentityRepository(session)
        self._audit = AuditTrail(session)

    async def start_login(self) -> tuple[str, int]:
        """Begin an unauthenticated sign-in attempt."""
        return await self._start(kind=FlowKind.LOGIN, user=None)

    async def start_link(self, *, user: User) -> tuple[str, int]:
        """Begin a linking attempt on behalf of somebody already signed in.

        The account is recorded in the flow, not carried through the browser.
        That is what binds the eventual callback to this person: the identity is
        attached to whoever started the flow, and there is no request field the
        caller could set to change that.
        """
        return await self._start(kind=FlowKind.LINK, user=user)

    async def _start(self, *, kind: FlowKind, user: User | None) -> tuple[str, int]:
        started = await self._flows.start(kind=kind, user_id=user.id if user else None)
        url = self._client.authorization_url(
            state=started.state,
            nonce=started.flow.nonce,
            challenge=code_challenge(started.flow.code_verifier),
        )
        # No audit row. Nobody is identified yet for a login, so there is no
        # actor to attribute it to, and a row an anonymous stranger can create
        # on demand is a way to flood a trail colleagues have to read. This is a
        # log line instead - see the audit vocabulary note in ADR-048.
        logger.info(
            "google.authorization_started",
            extra={
                "event": "google.authorization_started",
                "kind": kind.value,
                "user_id": str(user.id) if user else None,
            },
        )
        return url, FLOW_TTL_SECONDS

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
        workspace_slug: str | None = None,
    ) -> AuthenticatedSession:
        """Finish a sign-in, creating the account if this is a first login."""
        _, claims = await self._redeem(code=code, state=state, expected=FlowKind.LOGIN)

        identity = await self._identities.get_by_subject(
            provider=_PROVIDER,
            subject=claims.subject,
        )
        if identity is not None:
            return await self._resume(
                identity=identity,
                claims=claims,
                workspace_slug=workspace_slug,
            )

        # Before any lookup. See the module docstring: this ordering is what
        # keeps the collision answer below from being an enumeration oracle.
        if not claims.email_verified:
            logger.warning(
                "google.unverified_address_refused",
                extra={"event": "google.unverified_address_refused"},
            )
            raise AuthenticationError(GOOGLE_FAILED)

        email = normalise_email(claims.email)
        existing = await self._users.get_by_email(email)
        if existing is not None:
            return await self._refuse_collision(user=existing)

        return await self._enrol(claims=claims, email=email, workspace_slug=workspace_slug)

    async def _resume(
        self,
        *,
        identity: FederatedIdentity,
        claims: GoogleIdentityClaims,
        workspace_slug: str | None,
    ) -> AuthenticatedSession:
        """Open a session for an account this Google subject already owns.

        Once an identity row exists the email claim is never consulted again. A
        Google account whose address changes keeps working; a Google account
        that acquires somebody else's address gains nothing.

        The *display* claims are consulted every time, through
        :meth:`_refresh_profile`. Name and picture are decoration and follow
        Google; the address is identity and does not.
        """
        user = await self._users.get_by_id(identity.user_id)
        if user is None:
            # The foreign key cascades, so this should be unreachable. If it
            # happens, something deleted a user without the constraint firing.
            logger.error(
                "google.identity_without_user",
                extra={
                    "event": "google.identity_without_user",
                    "identity_id": str(identity.id),
                },
            )
            raise AuthenticationError(GOOGLE_FAILED)

        self._refresh_profile(user=user, claims=claims)

        try:
            session = await self._auth.authenticate_federated(
                user=user,
                workspace_slug=workspace_slug,
            )
        except PermissionDeniedError:
            # A disabled account. Recorded, because this is one of the two
            # refusals that can name a real account - and therefore one worth
            # having in the trail when somebody asks why they cannot get in.
            self._record(
                AuditAction.GOOGLE_LOGIN_FAILED,
                user=user,
                actor_kind=AuditActorKind.SYSTEM,
                reason="account_disabled",
            )
            raise

        await self._identities.stamp_login(
            identity_id=identity.id,
            now=datetime.now(UTC),
        )
        self._record(AuditAction.GOOGLE_LOGIN_SUCCEEDED, user=user, reason="existing_identity")
        return session

    @staticmethod
    def _refresh_profile(*, user: User, claims: GoogleIdentityClaims) -> None:
        """Follow Google's copy of the name and picture, and only those.

        Called on every login and on every link, so a renamed or
        re-photographed Google account is reflected here rather than frozen at
        whatever it said the first time. Both fields are decoration: nothing is
        authorized by either, so following the issuer costs nothing and keeps
        the interface from showing a five-year-old photograph.

        **The email address is deliberately not refreshed, and this is a
        security property rather than an omission.** `_resume` never consults
        the email claim once an identity row exists, precisely so that a Google
        account which later acquires somebody else's address gains nothing by
        it. Writing `claims.email` to `user.email` here would hand back exactly
        that: control of a Google account would become the power to move a
        Wasla account onto any address Google would attest to, and every
        password reset thereafter would go to the new one. The address is
        captured once, at enrolment, where it *is* the account.

        A `None` claim leaves the stored value alone. Google omitting a field
        is not the same statement as somebody clearing it, and treating the two
        alike would blank a perfectly good avatar every time a token arrived
        without one.
        """
        if claims.full_name is not None:
            user.full_name = claims.full_name
        if claims.picture is not None:
            user.avatar_url = claims.picture

    async def _refuse_collision(self, *, user: User) -> AuthenticatedSession:
        """Refuse a first login onto an address that already has an account.

        Never signs them in, never attaches the identity, never touches the
        password. The caller has proven control of a *mailbox*; that is not
        proof of anything about an account registered under it, which may have
        been opened by somebody who held the address before them.
        """
        self._record(
            AuditAction.GOOGLE_LOGIN_FAILED,
            user=user,
            actor_kind=AuditActorKind.SYSTEM,
            reason="address_already_registered",
        )
        logger.info(
            "google.login_refused_address_in_use",
            extra={
                "event": "google.login_refused_address_in_use",
                "user_id": str(user.id),
            },
        )
        raise ConflictError(ADDRESS_IN_USE)

    async def _enrol(
        self,
        *,
        claims: GoogleIdentityClaims,
        email: str,
        workspace_slug: str | None,
    ) -> AuthenticatedSession:
        """Create an account for a Google subject nobody has seen before.

        No password hash. `AuthService.login` already refuses an account whose
        hash is `None`, in the same branch and after the same delay as an
        address that does not exist, so this account is unreachable by password
        without a line of code being added anywhere.

        `email_verified_at` is stamped here because the address arrived inside a
        signature we checked (ADR-050). It grants nothing - `users.py` records
        that no route reads the column and no permission depends on it - so this
        writes down a fact rather than handing out access.

        No workspace is created. `register` needs a name and a slug that Google
        does not supply, and inventing one from a display name is a trap:
        `SLUG_PATTERN` is strict ASCII and a great many real names are not. The
        honest consequence is disclosed in ADR-047 - such an account holds a
        valid session but cannot open any workspace-scoped endpoint until it is
        invited somewhere.
        """
        try:
            user = await self._users.create(
                email=email,
                full_name=claims.full_name,
                hashed_password=None,
                avatar_url=claims.picture,
            )
            await self._session.flush()
            self._identities.create(
                user_id=user.id,
                provider=_PROVIDER,
                subject=claims.subject,
                now=datetime.now(UTC),
            )
            await self._session.flush()
        except IntegrityError as exc:
            # The uniqueness constraint is the backstop, exactly as section 7
            # asks. Two simultaneous first logins for one Google subject both
            # reach here; one inserts and one is refused by the database, so a
            # duplicate account cannot exist whatever the interleaving.
            #
            # The loser gets a retryable conflict rather than a transparent
            # retry, and their second attempt finds the identity and succeeds.
            # Recovering inside this request would mean unwinding a failed
            # transaction and re-reading, which is more moving parts than a
            # once-in-a-deployment race deserves.
            logger.warning(
                "google.first_login_lost_race",
                extra={"event": "google.first_login_lost_race"},
            )
            raise ConflictError("Please try signing in again.") from exc

        user.email_verified_at = datetime.now(UTC)
        session = await self._auth.authenticate_federated(
            user=user,
            workspace_slug=workspace_slug,
        )
        self._record(AuditAction.GOOGLE_LOGIN_SUCCEEDED, user=user, reason="account_created")
        logger.info(
            "google.account_created",
            extra={"event": "google.account_created", "user_id": str(user.id)},
        )
        return session

    async def complete_link(self, *, user: User, code: str, state: str) -> FederatedIdentity:
        """Attach a Google account to the person who started the flow.

        The binding is `flow.user_id`, recorded server-side when the flow began.
        Not a request field, not the token's email address - there is nothing
        here a caller can set to make the identity land on a different account.
        """
        flow, claims = await self._redeem(code=code, state=state, expected=FlowKind.LINK)
        if flow.user_id != user.id:
            # A link flow finished by somebody other than the account that
            # started it. Either a mix-up or an attempt to graft an identity
            # onto a session that did not ask for one.
            logger.warning(
                "google.link_flow_owner_mismatch",
                extra={"event": "google.link_flow_owner_mismatch", "user_id": str(user.id)},
            )
            self._record(
                AuditAction.GOOGLE_IDENTITY_LINK_FAILED,
                user=user,
                reason="flow_owner_mismatch",
            )
            raise AuthenticationError(GOOGLE_FAILED)

        owner = await self._identities.get_by_subject(provider=_PROVIDER, subject=claims.subject)
        if owner is not None:
            return self._refuse_link(user=user, owner=owner)

        if await self._identities.get_for_user(user_id=user.id, provider=_PROVIDER) is not None:
            self._record(
                AuditAction.GOOGLE_IDENTITY_LINK_FAILED,
                user=user,
                reason="already_connected",
            )
            raise ConflictError("This account is already connected to a Google account.")

        try:
            # No `last_login_at`: connecting an account is not signing in with
            # it, and "connected but never used" is a state worth being able to
            # see in the table.
            identity = self._identities.create(
                user_id=user.id,
                provider=_PROVIDER,
                subject=claims.subject,
            )
            await self._session.flush()
        except IntegrityError as exc:
            # Two links racing, for the same subject or the same account. The
            # constraints decide; this just reports it safely.
            self._record(
                AuditAction.GOOGLE_IDENTITY_LINK_FAILED,
                user=user,
                reason="lost_race",
            )
            raise ConflictError("Please try connecting Google again.") from exc

        self._adopt_verification(user=user, claims=claims)
        # A password account that has never had a picture gets one here, which
        # is usually the first time it can have had one at all.
        self._refresh_profile(user=user, claims=claims)
        self._record(AuditAction.GOOGLE_IDENTITY_LINKED, user=user, reason="linked")
        return identity

    def _refuse_link(self, *, user: User, owner: FederatedIdentity) -> FederatedIdentity:
        """Refuse a link for a Google account somebody else already connected.

        The identity is not moved. Moving it would mean anybody who can reach a
        Google account could walk it off the Wasla account it currently opens,
        and lock the rightful owner out of their own sign-in method.

        The message does not say whose it is. The caller is entitled to know
        that they cannot connect it and nothing more.
        """
        same = owner.user_id == user.id
        self._record(
            AuditAction.GOOGLE_IDENTITY_LINK_FAILED,
            user=user,
            reason="already_linked_here" if same else "owned_by_another_account",
        )
        logger.warning(
            "google.link_refused",
            extra={
                "event": "google.link_refused",
                "user_id": str(user.id),
                "same_account": same,
            },
        )
        if same:
            raise ConflictError("This Google account is already connected to your account.")
        raise ConflictError("This Google account is already connected to another account.")

    def _adopt_verification(self, *, user: User, claims: GoogleIdentityClaims) -> None:
        """Record Wasla email verification from a Google link, when it applies.

        Only when the validated Google address *equals this account's address*.
        Google proving that somebody owns `other@example.com` says nothing about
        whether this account owns the address it is registered under, and
        stamping the column from a mismatched claim would be a verification of
        the wrong mailbox.
        """
        if not claims.email_verified or user.email_verified_at is not None:
            return
        if normalise_email(claims.email) != normalise_email(user.email):
            return
        user.email_verified_at = datetime.now(UTC)

    async def unlink(self, *, user: User) -> None:
        """Disconnect Google from this account.

        Refused when it would leave the account with no way in at all: no
        password hash, and this the only identity. That check is the whole
        reason this method reads two things before writing anything - a person
        who signed up with Google and never set a password would otherwise be
        able to lock themselves out with one button.

        The recovery path if it happens anyway is an ordinary password reset:
        a Google-first account owns its mailbox, so the existing reset flow
        reaches it. Documented in ADR-049.
        """
        identity = await self._identities.get_for_user(user_id=user.id, provider=_PROVIDER)
        if identity is None:
            raise NotFoundError("No Google account is connected.")

        if user.hashed_password is None:
            remaining = await self._identities.count_for_user(user_id=user.id)
            if remaining <= 1:
                raise PermissionDeniedError(
                    "Set a password before disconnecting Google, "
                    "or you will not be able to sign in."
                )

        # One conditional DELETE. Two concurrent requests cannot both believe
        # they were the one that removed it.
        if not await self._identities.delete_for_user(user_id=user.id, provider=_PROVIDER):
            raise NotFoundError("No Google account is connected.")

        self._record(AuditAction.GOOGLE_IDENTITY_UNLINKED, user=user, reason="unlinked")

    async def _redeem(
        self,
        *,
        code: str,
        state: str,
        expected: FlowKind,
    ) -> tuple[OAuthFlow, GoogleIdentityClaims]:
        """Spend the state, exchange the code, and validate what comes back.

        The state is spent *first*. Everything after that line runs only for a
        caller who presented a state this process issued, has not been used, and
        was issued for this kind of flow - so a replay cannot reach the network,
        and a login flow cannot be finished at the linking endpoint.
        """
        flow = await self._flows.spend(state=state)
        if flow is None or flow.kind is not expected:
            logger.warning(
                "google.state_refused",
                extra={
                    "event": "google.state_refused",
                    "expected": expected.value,
                    "found": flow.kind.value if flow else None,
                },
            )
            raise AuthenticationError(GOOGLE_FAILED)

        try:
            id_token = await self._client.exchange(
                code=code,
                code_verifier=flow.code_verifier,
            )
        except GoogleExchangeError as exc:
            raise AuthenticationError(GOOGLE_FAILED) from exc

        try:
            claims = await self._verifier.verify(id_token=id_token, nonce=flow.nonce)
        except GoogleTokenInvalidError as exc:
            # `reason` is a category, never token material. It is the difference
            # between a diagnosable outage and a mystery, and it stays here
            # rather than travelling to the caller.
            logger.warning(
                "google.id_token_refused",
                extra={"event": "google.id_token_refused", "reason": exc.reason},
            )
            raise AuthenticationError(GOOGLE_FAILED) from exc
        except GoogleKeysUnavailableError as exc:
            # Not a rejected login. Answering 401 here would tell a person their
            # perfectly good Google account was refused, when the truth is that
            # we could not reach Google's key document.
            raise DependencyUnavailableError(
                "Google sign-in is temporarily unavailable.",
                details={"dependency": "google"},
            ) from exc

        return flow, claims

    def _record(
        self,
        action: AuditAction,
        *,
        user: User,
        reason: str,
        actor_kind: AuditActorKind = AuditActorKind.USER,
    ) -> None:
        """Write an audit row naming the account and the outcome.

        `meta` carries the provider and a reason and nothing else. No ID token,
        no access token, no authorization code, no nonce, no state, and no
        provider subject: the subject is a stable cross-service identifier, the
        row it would duplicate is already in `user_identities`, and an audit log
        is read by people.
        """
        self._audit.record(
            action,
            actor=user,
            actor_kind=actor_kind,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            meta={"provider": _PROVIDER.value, "reason": reason},
        )
