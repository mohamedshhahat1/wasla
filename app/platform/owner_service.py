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
        """Everyone currently holding the owner role."""
        statement = select(User).where(User.platform_role == PlatformRole.PLATFORM_OWNER)
        return list((await self._session.execute(statement)).scalars().all())

    async def staff(self) -> list[User]:
        """Everyone holding any platform role, for the `list` command."""
        statement = select(User).where(User.platform_role.is_not(None)).order_by(User.email)
        return list((await self._session.execute(statement)).scalars().all())

    async def grant(self, identity: str, role: PlatformRole) -> RoleChange:
        """Give an existing account a platform role.

        Idempotent: granting the role somebody already holds changes nothing and
        writes no entry, so a command re-run after a failed deploy does not fill
        the trail with acts that did not happen.
        """
        user = await self._resolve(identity)
        previous = user.platform_role
        if previous is role:
            return RoleChange(user.id, user.email, previous, role)

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
            remaining = [row for row in await self.owners() if row.id != user.id]
            if not remaining:
                raise ValidationError(
                    "This is the only platform owner. Grant the role to somebody "
                    "else before removing it from this account."
                )

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
