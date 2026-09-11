"""Who among platform staff may act on whom, and the owner who must remain.

Two invariants live here, together, because they failed together (ADR-099).

**The hierarchy.** `platform_owner` outranks `platform_admin`. Before this
module they were the same authority wearing different names: all eleven
`/platform/*` routes sat behind `PlatformStaffDep`, `PlatformOwnerDep` was
defined and guarded nothing, and `AccountService.delete` asked only whether the
target was the *caller* - never what the target was. So a platform admin could
tombstone every platform owner on the installation and keep reading
`/platform/tenants` afterwards (AUTHZ-01). Nothing customer-facing was exposed;
what was lost was the platform's own way back in, irreversibly.

**The last live owner.** `PlatformRoleService.owners()` selected on
`platform_role` alone. A tombstoned account keeps its role, so a deleted owner
still counted as "remaining" and the guard whose whole purpose is to stop an
installation reaching zero owners could be satisfied by an owner who no longer
existed (AUTHZ-02). Ordinary operator sequencing reached it - delete one of two
owners, revoke the other - with no adversary involved.

Both fixes rest on one definition, `live_platform_role`, and the point of
putting it here is that there is exactly one. The bug was not that somebody
wrote a wrong filter; it was that "is a platform owner" was spelled out
independently in the account lifecycle and in the role lifecycle, and the two
spellings disagreed about what a deleted account is.

**Counting is not enough.** Every guard here reads a set and then acts on it,
which is check-then-act: two owners deleting each other at the same instant
both read "two owners, one may go" and the installation ends up with none. So
the count and the mutation happen inside one critical section, serialised by a
singleton advisory lock - the same shape `WorkspaceService` uses for the tenant
owner set, differing only in that a platform has no row to lock, because the
invariant is about the platform itself.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import ColumnElement, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDeniedError, ValidationError
from app.db.models.enums import PlatformRole
from app.db.models.user import User

# Namespace for platform-security advisory locks, distinct from the workspace
# creation namespace in `app.services.workspace_service` so the two cannot
# block each other. The key is a constant rather than derived: the resource
# being protected is "the set of platform owners", of which there is one.
_PLATFORM_LOCK_NAMESPACE: Final = 0x5741_5302
_PLATFORM_OWNER_LOCK_KEY: Final = 1

LAST_PLATFORM_OWNER_MESSAGE: Final = (
    "This is the only remaining platform owner. Grant the role to somebody "
    "else before removing it from this account."
)
PLATFORM_HIERARCHY_MESSAGE: Final = (
    "A platform administrator cannot act on a platform owner's account."
)


def live_platform_role(role: PlatformRole) -> ColumnElement[bool]:
    """The canonical predicate: this role, held by an account that still exists.

    Three conditions, and the two that were missing are the finding. An account
    is a *live* holder of a platform role only while it is active and not
    tombstoned - a deleted user keeps `platform_role` for the audit trail's
    sake, and reading that column alone counts authority nobody can exercise.

    Returned as a clause rather than a list of rows so callers compose it into
    whatever they already select - a count, a `FOR UPDATE`, an existence check -
    without a second spelling of the condition appearing at the call site.
    """
    return (User.platform_role == role) & User.deleted_at.is_(None) & User.is_active.is_(True)


def live_platform_staff() -> ColumnElement[bool]:
    """Anyone holding any platform role, under the same lifecycle filter.

    Used by the `list` command, so an operator asking who has authority today
    is not shown accounts that were deleted last year.
    """
    return User.platform_role.is_not(None) & User.deleted_at.is_(None) & User.is_active.is_(True)


async def lock_platform_owners(session: AsyncSession) -> None:
    """Serialise this transaction against every other owner-set change.

    `pg_advisory_xact_lock`, not a row lock, because the invariant is about a
    *set* and no row represents it. Locking the owner rows you can see cannot
    stop a row you cannot see from changing - and the specific race is two
    transactions each locking the account it is about to remove, which
    serialises nothing at all, because they are different rows.

    Held until commit or rollback, released by PostgreSQL with no cleanup path
    to get wrong. Every guard below takes it before counting; a caller that
    counts without it has written the bug this exists to prevent.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
        {"namespace": _PLATFORM_LOCK_NAMESPACE, "key": _PLATFORM_OWNER_LOCK_KEY},
    )


async def live_platform_owners(
    session: AsyncSession, *, excluding: uuid.UUID | None = None
) -> list[User]:
    """Every account that can exercise platform ownership right now.

    `excluding` answers the only question callers actually ask - "who is left
    if this one goes" - at the point where the exclusion cannot be forgotten.
    """
    statement = select(User).where(live_platform_role(PlatformRole.PLATFORM_OWNER))
    if excluding is not None:
        statement = statement.where(User.id != excluding)
    return list((await session.execute(statement)).scalars().all())


async def require_surviving_platform_owner(session: AsyncSession, *, target: uuid.UUID) -> None:
    """Refuse an act that would leave the platform with no live owner.

    Takes the lock first and counts second, so the answer cannot go stale
    between the two. Callers must perform the mutation in the same transaction;
    doing it in another one is the check-then-act this exists to close.

    A `ValidationError` rather than a permission refusal, because the caller is
    allowed to do this - it is the *state* that refuses. The operator CLI and
    the HTTP routes both surface it, and the message says what to do next: an
    operator who has just locked themselves out of the platform dashboard is
    not in a position to work it out.
    """
    await lock_platform_owners(session)
    if not await live_platform_owners(session, excluding=target):
        raise ValidationError(LAST_PLATFORM_OWNER_MESSAGE)


def require_platform_authority_over(*, actor: User, target: User) -> None:
    """Refuse a destructive act by an admin against an owner.

    The whole hierarchy, in one place, stated positively: an owner may act on
    any platform account; an admin may act on any account that does not hold
    platform ownership.

    Deliberately *not* `live_platform_role`, and the difference is the guard.
    That predicate answers "who can exercise ownership right now", which is the
    right question when counting who would be left. This one answers "is this
    an owner's account", and a **disabled** owner is still one: the role is
    intact and any staff member can re-enable it. Reusing the narrow predicate
    here would let disable-then-delete walk around the guard - refused at the
    first step only while the owner is enabled, permitted at the second the
    moment anybody else disabled them.

    Tombstones need no exemption. `AccountService.delete` clears the role as it
    writes the tombstone, and every administrative lookup that reaches this
    function excludes deleted rows anyway, so "holds the owner role" and "is an
    owner worth protecting" are the same set.

    Self-action is not this function's business. It is refused earlier and for
    a different reason - an owner acting on themselves is not a hierarchy
    violation, it is somebody locking themselves out - and keeping the two
    apart is why each has its own message.
    """
    if actor.platform_role is PlatformRole.PLATFORM_OWNER:
        return
    if target.platform_role is PlatformRole.PLATFORM_OWNER:
        raise PermissionDeniedError(PLATFORM_HIERARCHY_MESSAGE)


__all__ = [
    "LAST_PLATFORM_OWNER_MESSAGE",
    "PLATFORM_HIERARCHY_MESSAGE",
    "live_platform_owners",
    "live_platform_role",
    "live_platform_staff",
    "lock_platform_owners",
    "require_platform_authority_over",
    "require_surviving_platform_owner",
]
