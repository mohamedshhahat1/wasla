"""Account lifecycle and session revocation.

The one place `users.token_version` is written, and the one place
`users.is_active` is written after registration (ADR-036).

Before this existed, `is_active` was checked on every request and set by exactly
one line - `is_active=True` in `UserRepository.create`. Nothing could ever set
it false, so the check guarded a column no code path could change, and the only
way to end a session was to rotate `JWT_SECRET`, which signs out every user of
every tenant at once. That is an outage, not a revocation.

Four rules hold across everything here:

**Every state change bumps the version.** Disabling, re-enabling, revoking
sessions and changing a password all raise it by one. Re-enabling is the one
that looks unnecessary and is not: without it, tokens minted before a suspension
would start working again the moment the account came back, so a disable-enable
cycle would resurrect exactly the credentials the disable was meant to kill.

**Authority is separated by what the thing belongs to.** An account is a global
identity spanning workspaces, so disabling one is a *platform* action - a tenant
administrator who could do it would be able to evict somebody from every other
workspace they belong to. What a person does to their own account needs no
administrator at all.

**Nothing here logs a token, and nothing returns one.** Revocation is expressed
as a version number; the tokens it invalidates are never named.

**Every state change tells the account holder.** The audit trail tells staff;
it does not tell the person whose sessions just ended, and they are the one who
knows whether they did it. The notice is queued on this session (ADR-042), so
it commits with the change or not at all - and it is addressed to the row's own
`email`, never to anything from the request.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.user import User
from app.repositories import UserRepository
from app.services.audit_service import AuditTrail
from app.services.email_service import EmailOutbox
from app.services.email_templates import EmailTemplate

logger = get_logger(__name__)


class AccountService:
    """Disable, re-enable, revoke sessions, and change a password.

    Owns no transaction. The request-scoped session commits when the request
    succeeds, which matters here more than usual: an audit entry describing a
    revocation that rolled back would be a log of something that did not happen,
    and an email announcing it would be worse - unlike a log line, it cannot be
    taken back.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        # Defaulted rather than required so existing construction sites keep
        # working; the request-scoped provider passes the real settings in.
        self._settings = settings if settings is not None else get_settings()
        self._users = UserRepository(session)
        # No tenant: these are platform-level acts on a global identity.
        self._audit = AuditTrail(session)
        self._outbox = EmailOutbox(session, self._settings)

    async def _require(self, user_id: uuid.UUID) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError("No account matches that identifier.")
        return user

    @staticmethod
    def _bump(user: User) -> int:
        """Raise the version, invalidating every token already issued.

        Returns the new value so a caller can record it. The number itself is
        not a secret - it says nothing about any token - so it is safe in an
        audit entry, and having it there is what makes a revocation auditable
        after the fact.
        """
        user.token_version += 1
        return user.token_version

    async def _notify(self, user: User, template: EmailTemplate, *, version: int) -> None:
        """Queue the notice for a security change on this account.

        Keyed on the *new* version, so each act notifies once however many
        times the request is retried, and no two acts can share a key. None of
        these templates take a variable, so there is nothing to pass and
        nothing caller-influenced to escape.

        Not tenant-scoped: an account is a global identity, and a workspace
        stamped on the row would misattribute a platform act to whichever
        workspace the person happened to be using.
        """
        await self._outbox.enqueue(
            template=template,
            recipient=user.email,
            idempotency_key=f"security-{template.value}:{user.id}:{version}",
            user_id=user.id,
        )

    async def disable(self, *, user_id: uuid.UUID, actor: User) -> User:
        """Suspend an account and end every session it holds.

        Idempotent on `is_active`, but **not** on the version: disabling an
        already-disabled account still bumps, because the reason somebody
        presses it twice is usually that they are not certain the first one
        took effect.
        """
        user = await self._require(user_id)
        if user.id == actor.id:
            # Locking yourself out of the platform is not a thing to do by
            # accident, and there may be no other administrator to undo it.
            raise ValidationError("An administrator cannot disable their own account.")

        user.is_active = False
        version = self._bump(user)
        self._audit.record(
            AuditAction.USER_DISABLED,
            actor=actor,
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            meta={"token_version": version},
        )
        await self._notify(user, EmailTemplate.ACCOUNT_DISABLED, version=version)
        logger.info(
            "account.disabled",
            extra={
                "event": "account.disabled",
                "user_id": str(user.id),
                "actor_id": str(actor.id),
            },
        )
        return user

    async def enable(self, *, user_id: uuid.UUID, actor: User) -> User:
        """Restore an account, without restoring anything it used to hold.

        The bump here is the whole point. A token minted before the suspension
        is still signed and may still be inside its lifetime; without raising
        the version, re-enabling would hand it back its authority. Somebody
        coming back from suspension signs in again.
        """
        user = await self._require(user_id)
        user.is_active = True
        version = self._bump(user)
        self._audit.record(
            AuditAction.USER_ENABLED,
            actor=actor,
            actor_kind=AuditActorKind.PLATFORM_STAFF,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            meta={"token_version": version},
        )
        await self._notify(user, EmailTemplate.ACCOUNT_ENABLED, version=version)
        logger.info(
            "account.enabled",
            extra={
                "event": "account.enabled",
                "user_id": str(user.id),
                "actor_id": str(actor.id),
            },
        )
        return user

    async def revoke_sessions(self, *, user: User) -> User:
        """Sign this person out everywhere, leaving the account usable.

        The self-service half of revocation, and the reason it exists: somebody
        who thinks a session leaked should not have to find a platform
        administrator, and should not have to lose their account to end it.
        Signing in again immediately afterwards is the expected behaviour.
        """
        version = self._bump(user)
        self._audit.record(
            AuditAction.USER_SESSIONS_REVOKED,
            actor=user,
            actor_kind=AuditActorKind.USER,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            meta={"token_version": version},
        )
        # Worth sending even though the person just asked for it: if they did
        # not, this is the only signal they get, and the request needed only a
        # live access token - which is exactly what a leaked session is.
        await self._notify(user, EmailTemplate.SESSIONS_REVOKED, version=version)
        logger.info(
            "account.sessions_revoked",
            extra={"event": "account.sessions_revoked", "user_id": str(user.id)},
        )
        return user

    async def change_password(
        self,
        *,
        user: User,
        current_password: str,
        new_password: str,
    ) -> User:
        """Replace a password, proving the old one first, and end every session.

        Not a reset. A reset is for somebody who cannot sign in and needs a
        message sent to an address they control; this is for somebody already
        signed in, so the proof is the current password rather than an emailed
        token. Both now notify the account afterwards, and both do it through
        the outbox - see `password_reset_service` for the other half.

        Ending every session on success is what makes this useful against a
        leaked token rather than merely tidy: the usual reason to change a
        password is that something may have been taken.
        """
        if user.hashed_password is None:
            # An account created by an invitation that was never completed. It
            # has no current password to prove, so there is nothing to replace.
            raise ValidationError("This account has no password set.")

        if not verify_password(password=current_password, password_hash=user.hashed_password):
            # Deliberately the same shape as a failed login. A caller who has an
            # access token but not the password is exactly the caller this is
            # defending against.
            raise AuthenticationError("The current password is incorrect.")

        validate_password_strength(new_password)
        if verify_password(password=new_password, password_hash=user.hashed_password):
            raise ValidationError("The new password must differ from the current one.")

        user.hashed_password = hash_password(new_password)
        version = self._bump(user)
        self._audit.record(
            AuditAction.PASSWORD_CHANGED,
            actor=user,
            actor_kind=AuditActorKind.USER,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            meta={"token_version": version},
        )
        # The notice a compromised account depends on. Whoever changed the
        # password now holds every session; this message is addressed to the
        # mailbox on the account, which is the one thing they may not hold.
        await self._notify(user, EmailTemplate.PASSWORD_CHANGED, version=version)
        logger.info(
            "account.password_changed",
            extra={"event": "account.password_changed", "user_id": str(user.id)},
        )
        return user

    async def set_password(self, *, user: User, new_password: str) -> User:
        """Give a first password to an account that has none.

        The counterpart to :meth:`change_password`, and deliberately a separate
        method rather than a branch inside it. `change_password` proves control
        with the current password; an account that has never had one cannot
        prove anything that way, so the proof here is the authenticated session
        itself. Keeping them apart is what stops this from becoming a way to
        replace a password without knowing it: the guard below refuses any
        account that already has a hash, and there is no argument that relaxes
        it.

        Who needs it: somebody who signed up with Google. `_enrol` creates that
        account with `hashed_password=None` on purpose, and until now every
        route that could give it one refused - `change_password` for want of a
        current password, and the reset flow because it declines passwordless
        accounts rather than becoming an oracle. `unlink` meanwhile refuses to
        disconnect Google while there is no password, telling the person to set
        one. This is the route that sentence refers to (ADR-057).

        It is emphatically **not** the path an invitation used to take. An
        invitation is a workspace's statement about somebody; a session is that
        person. See `InvitationService.accept`.

        Everything after the guard is `change_password`'s policy, reused rather
        than restated: the same strength rule, the same version bump, the same
        audit action, the same notice to the address on the account. Ending
        every session includes the one making this call, which is the existing
        semantics and the right ones - a first password is a credential change,
        and `AccountStateResponse` carries the new version so a client knows to
        sign in again.
        """
        if user.hashed_password is not None:
            # Not a not-found and not a conflict about state the caller cannot
            # see: they know perfectly well whether they have a password, and
            # the route that replaces one is `/auth/password`.
            raise ValidationError(
                "This account already has a password. Use the password change endpoint."
            )

        validate_password_strength(new_password)
        user.hashed_password = hash_password(new_password)
        version = self._bump(user)
        self._audit.record(
            AuditAction.PASSWORD_CHANGED,
            actor=user,
            actor_kind=AuditActorKind.USER,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            # Distinguishes a first password from a replacement without needing
            # a second audit action, and therefore without a migration to add
            # one to the enum.
            meta={"token_version": version, "initial": True},
        )
        await self._notify(user, EmailTemplate.PASSWORD_CHANGED, version=version)
        logger.info(
            "account.password_set",
            extra={"event": "account.password_set", "user_id": str(user.id)},
        )
        return user
