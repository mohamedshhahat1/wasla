"""Granting and withdrawing authority over the whole platform.

The one privilege in this system with no HTTP route, and the absence is the
design (ADR-094). A platform role reaches into every workspace on the platform;
an endpoint that granted one would need a caller who already held it, which
answers the bootstrap question with itself, and would put the platform's own
escalation path on the internet for the sake of an operation performed a handful
of times in a deployment's life.

So the actor here is somebody who can already reach the database through the
application's own configuration - a person with a shell on the deployment - and
what this module adds is that the act is *recorded*. `users.platform_role` used
to be written by an `UPDATE` typed at a psql prompt: no trail, no validation,
and nothing in the repository that said it was the supported step.

Note what is deliberately not here. No user is created. A role can only be
granted to an account that already exists and signed up in the ordinary way, so
this command cannot manufacture an identity - it can only change what an
existing one is allowed to do.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.enums import PlatformRole
from app.db.models.user import User
from app.platform.hierarchy import (
    live_platform_role,
    live_platform_staff,
    require_surviving_platform_owner,
)
from app.services.audit_service import AuditTrail

logger = get_logger(__name__)

TARGET_TYPE = "user"


@dataclass(frozen=True, slots=True)
class RoleChange:
    """What the command did, for an operator reading its output."""

    user_id: uuid.UUID
    email: str
    previous: PlatformRole | None
    current: PlatformRole | None

    @property
    def changed(self) -> bool:
        return self.previous is not self.current


class PlatformRoleService:
    """Reads and writes `users.platform_role`, and records every change.

    Not tenant-scoped, and cannot be: a platform role is a property of a global
    identity rather than of a membership, which is the whole distinction
    `require_platform_roles` rests on.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        # No tenant: a platform action is recorded against the platform, which
        # is what a null `tenant_id` means in this table.
        self._audit = AuditTrail(session)

    async def _resolve(self, identity: str) -> User:
        """The account this identity names, by exact id or exact address.

        Two forms because an operator has one or the other to hand and neither
        is guessable. Matching is exact - no prefix, no substring, no "did you
        mean" - because a fuzzy match here grants platform authority to somebody
        who was not named.
        """
        try:
            user_id = uuid.UUID(identity)
        except ValueError:
            statement = select(User).where(func.lower(User.email) == identity.strip().lower())
        else:
            statement = select(User).where(User.id == user_id)

        user = (await self._session.execute(statement)).scalars().one_or_none()
        if user is None:
            raise ValidationError(f"No account matches {identity!r}.")
        return user

    async def owners(self) -> list[User]:
        """Everyone who can exercise platform ownership right now.

        This used to select on `platform_role` alone, and that was AUTHZ-02. A
        tombstoned account keeps its role, so a deleted owner still counted as
        "remaining" and `revoke`'s last-owner guard - the one thing standing
        between an installation and having nobody who can administer it - could
        be satisfied by an account that no longer existed. Delete one of two
        owners, then revoke the survivor, and the guard says yes. No adversary
        is required; that is just the order an operator does things in.

        The filter now comes from `app.platform.hierarchy`, shared with the
        account lifecycle. The defect was never a wrong `WHERE` clause - it was
        that "is a platform owner" had two independent spellings which disagreed
        about what a deleted account is.
        """
        statement = select(User).where(live_platform_role(PlatformRole.PLATFORM_OWNER))
        return list((await self._session.execute(statement)).scalars().all())

    async def staff(self) -> list[User]:
        """Everyone who currently holds any platform role, for `list`.

        Same lifecycle filter, for the same reason in a smaller way: an
        operator asking who has authority over this platform is asking about
        today, and a roster padded with accounts deleted last year answers a
        question nobody asked.
        """
        statement = select(User).where(live_platform_staff()).order_by(User.email)
        return list((await self._session.execute(statement)).scalars().all())

    async def grant(self, identity: str, role: PlatformRole) -> RoleChange:
        """Give an existing account a platform role.

        Idempotent: granting the role somebody already holds changes nothing and
        writes no entry, so a command re-run after a failed deploy does not fill
        the trail with acts that did not happen.
        """
        user = await self._resolve(identity)
        if user.is_deleted:
            # A tombstone is documented as never reusable, and the deletion path
            # clears `platform_role` precisely so no deleted row holds authority.
            # Granting one would put the role back on an account that can never
            # authenticate again - an entry in the trail saying somebody was
            # made a platform owner, and no owner at the end of it.
            raise ValidationError(
                f"{user.email} has been permanently deleted and cannot hold a platform role."
            )
        if not user.is_active:
            # Suspended rather than gone, so this is a sequencing mistake rather
            # than an impossibility, and the message says which order to do it
            # in. Granting silently would leave authority that only takes effect
            # if somebody later re-enables the account for an unrelated reason.
            raise ValidationError(
                f"{user.email} is disabled. Re-enable the account before granting it a role."
            )

        previous = user.platform_role
        if previous is role:
            return RoleChange(user.id, user.email, previous, role)

        if previous is PlatformRole.PLATFORM_OWNER:
            # A grant is also a *demotion* when the account already owns the
            # platform: `grant <the last owner> platform_admin` removes the last
            # owner through a command whose name suggests it only ever adds.
            # Same invariant, same lock - the shape of the act decides, not the
            # subcommand it arrived under.
            await require_surviving_platform_owner(self._session, target=user.id)

        user.platform_role = role
        self._audit.record(
            AuditAction.PLATFORM_ROLE_GRANTED,
            # `SYSTEM`, not `PLATFORM_STAFF`: the actor is an operator at a
            # shell, and there is no authenticated user to attribute this to.
            # Claiming the *target* did it would be worse than saying nothing.
            actor_kind=AuditActorKind.SYSTEM,
            target_type=TARGET_TYPE,
            target_id=user.id,
            target_label=user.email,
            meta={
                "role": role.value,
                "previous_role": previous.value if previous else None,
                "granted_by": "operator_command",
            },
        )
        logger.info(
            "platform.role_granted",
            extra={"event": "platform.role_granted", "user_id": str(user.id), "role": role.value},
        )
        return RoleChange(user.id, user.email, previous, role)

    async def revoke(self, identity: str) -> RoleChange:
        """Take a platform role away.

        Refuses to remove the last owner. Not tidiness: an installation with no
        platform owner has no supported way back except this same command, and
        an operator who has just locked themselves out of the platform dashboard
        at two in the morning is not in a good position to discover that.
        """
        user = await self._resolve(identity)
        previous = user.platform_role
        if previous is None:
            return RoleChange(user.id, user.email, None, None)

        if previous is PlatformRole.PLATFORM_OWNER:
            # Counted and revoked inside one advisory-locked critical section.
            # The count on its own was check-then-act: this command and a
            # `DELETE /platform/users/{id}` against the other owner could each
            # read "two owners, one may go" and commit, and the installation
            # would end up with none - a race between two operators doing
            # perfectly ordinary things at the same moment.
            await require_surviving_platform_owner(self._session, target=user.id)

        user.platform_role = None
        self._audit.record(
            AuditAction.PLATFORM_ROLE_REVOKED,
            actor_kind=AuditActorKind.SYSTEM,
            target_type=TARGET_TYPE,
            target_id=user.id,
            target_label=user.email,
            meta={"previous_role": previous.value, "revoked_by": "operator_command"},
        )
        logger.info(
            "platform.role_revoked",
            extra={"event": "platform.role_revoked", "user_id": str(user.id)},
        )
        return RoleChange(user.id, user.email, previous, None)


__all__ = ["PlatformRoleService", "RoleChange"]
