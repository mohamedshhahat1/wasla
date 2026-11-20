"""Who belongs to a workspace, and how that ends.

Revocation is a status on the membership, not a deleted row and not a token
operation (ADR-038). Three consequences follow from that choice, and all three
are the reason for it.

**It takes effect on the next request.** `get_active_workspace` loads the
membership on every request rather than trusting the token, so a revoked
membership stops authorising immediately. Nothing has to expire and no token has
to be revoked.

**It does not touch the rest of the person's life.** Bumping `token_version`
would sign them out of every *other* workspace they belong to, and out of their
own account. Being removed from one workspace is not a reason to be ejected from
a different company's.

**It is answerable afterwards.** The row keeps who removed whom and when, which
a delete would take with it.

The rules below are about not stranding a workspace. Every one of them reduces
to: there must always be somebody who can let people back in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.db.models import Membership, MembershipStatus, TenantRole, User
from app.db.models.audit import AuditAction, AuditActorKind
from app.repositories import MembershipRepository, TenantRepository, UserRepository
from app.services.audit_service import AuditTrail
from app.services.member_departure import release_departing_member

logger = get_logger(__name__)

# The refusal a client has to act on rather than merely display: it is the one
# that tells somebody they must hand the workspace over, or close it, before
# they can go. Shared with `WorkspaceService`, which raises the same code from
# the account-deletion path for the same reason.
LAST_WORKSPACE_OWNER = "last_workspace_owner"

# The role-change refusals a screen tells apart (FEB-42, EC8-02). Each shares
# its status with another refusal, so the code is what distinguishes them.
MEMBER_ROLE_SELF = "member_role_self"
MEMBER_ROLE_OWNER = "member_role_owner"
MEMBER_ROLE_ADMIN_OWNER_REQUIRED = "member_role_admin_owner_required"
MEMBER_NOT_ACTIVE = "member_not_active"

# The roles a role change moves between. Ownership is handed over through the
# transfer, never assigned here.
ASSIGNABLE_ROLES = frozenset({TenantRole.MEMBER, TenantRole.TENANT_ADMIN})


@dataclass(frozen=True, slots=True)
class MemberView:
    """One row of the roster, with the account details a screen needs.

    A view rather than the ORM object because the useful columns live on two
    tables and the caller should not be issuing a second query per member to
    find out somebody's name.
    """

    membership: Membership
    user: User


class MembershipService:
    """Listing, revoking and reinstating workspace membership.

    Workspace-scoped at construction: the tenant comes from the authenticated
    context and cannot be redirected by anything in a request.
    """

    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._memberships = MembershipRepository(session, tenant_id=tenant_id)
        self._users = UserRepository(session)
        self._tenants = TenantRepository(session)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    async def list_members(self, *, include_revoked: bool = False) -> list[MemberView]:
        """The roster.

        Revoked rows are available but off by default, because "who is in this
        workspace" and "who has ever been in this workspace" are different
        questions and only one of them is what a member list means.
        """
        memberships = await self._memberships.list_members(include_revoked=include_revoked)
        # Two statements whatever the roster's size: the memberships, then
        # their people by id. It was one query per member (DB-014).
        users = await self._users.get_many([membership.user_id for membership in memberships])
        return [
            MemberView(membership=membership, user=users[membership.user_id])
            for membership in memberships
            if membership.user_id in users
        ]

    async def revoke(
        self,
        *,
        actor: User,
        actor_role: TenantRole,
        user_id: uuid.UUID,
    ) -> Membership:
        """Withdraw somebody's access to this workspace.

        The rules, and what each one is protecting:

        - **A member may remove themselves.** Leaving does not need permission.
        - **Removing somebody else needs owner or admin.** A member who could
          evict their colleagues would make the role boundary decorative.
        - **Only an owner may remove an owner.** Otherwise an administrator can
          promote themselves by subtraction.
        - **The last active owner cannot be removed, including by themselves.**
          A workspace with no owner has nobody who can invite one; it is not
          recoverable from inside, and the person who did it usually did not
          mean to.
        - **Removing somebody already removed is a conflict, not a no-op.** The
          caller is looking at a stale roster and should see it refreshed.

        The last-owner rule is only a rule if it is read under a lock. Two
        owners leaving at the same moment each counted two owners, each
        concluded that leaving was safe, and the workspace ended with none - the
        count and the write have to be in the same critical section. Taking the
        tenant row first (`TenantRepository.lock`) puts them there, and puts this
        operation in the same queue as ownership transfer and workspace
        deletion, which race with it for the same reason.
        """
        # Before any read of the membership set. The lock is the whole point and
        # a lock taken after the decision is decoration.
        await self._tenants.lock(self._tenant_id)

        membership = await self._memberships.get_any_for_user(user_id)
        if membership is None:
            # Not "forbidden": whether a person exists in another workspace is
            # not something this endpoint discloses.
            raise ValidationError("That person is not a member of this workspace.")
        if not membership.is_active:
            raise ConflictError("That person is no longer a member of this workspace.")

        removing_self = membership.user_id == actor.id
        if not removing_self and actor_role not in (
            TenantRole.TENANT_OWNER,
            TenantRole.TENANT_ADMIN,
        ):
            raise PermissionDeniedError("This action requires a different role in this workspace.")
        if (
            not removing_self
            and membership.role is TenantRole.TENANT_OWNER
            and actor_role is not TenantRole.TENANT_OWNER
        ):
            raise PermissionDeniedError("Only a workspace owner can remove another owner.")

        if membership.role is TenantRole.TENANT_OWNER:
            owners = await self._memberships.count_active_owners()
            if owners <= 1:
                raise ConflictError(
                    "This workspace would be left without an owner. "
                    "Transfer ownership or delete the workspace first.",
                    # A client shows a different screen for this than for a
                    # stale roster, and both are 409, so the code is the only
                    # thing that tells them apart.
                    error_code=LAST_WORKSPACE_OWNER,
                )

        membership.status = MembershipStatus.REVOKED
        membership.revoked_at = datetime.now(UTC)
        membership.revoked_by_id = actor.id
        # Flushed first: the UPDATE is what locks the membership row, and an
        # assignment to this person share-locks it, so from here on nothing
        # new can be handed to them and the release below sees everything that
        # was (CRM-11).
        await self._session.flush()
        await release_departing_member(
            self._session,
            tenant_id=self._tenant_id,
            user_id=user_id,
            actor=actor,
        )

        target = await self._users.get_by_id(user_id)
        self._audit.record(
            AuditAction.MEMBER_LEFT if removing_self else AuditAction.MEMBER_REMOVED,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="membership",
            target_id=membership.id,
            target_label=target.email if target is not None else None,
            meta={"role": membership.role.value},
        )
        logger.info(
            "membership.revoked",
            extra={
                "event": "membership.revoked",
                "tenant_id": str(self._tenant_id),
                "user_id": str(user_id),
                "actor_id": str(actor.id),
                "self_service": removing_self,
            },
        )
        return membership

    async def reinstate(
        self,
        *,
        actor: User,
        actor_role: TenantRole,
        user_id: uuid.UUID,
        role: TenantRole,
    ) -> Membership:
        """Let a removed member back in, at a role the actor may grant.

        Exists so that re-admitting somebody does not require an invitation
        round trip to an address that may no longer receive mail. It reuses the
        existing row - the unique constraint on `(user_id, tenant_id)` requires
        it - so the removal and the return are both visible on one record.

        Granting ownership stays an owner's decision, matching the invitation
        path exactly: an administrator who could reinstate somebody as an owner
        could mint themselves a peer with authority they do not have.
        """
        if actor_role not in (TenantRole.TENANT_OWNER, TenantRole.TENANT_ADMIN):
            raise PermissionDeniedError("This action requires a different role in this workspace.")
        if role is TenantRole.TENANT_OWNER and actor_role is not TenantRole.TENANT_OWNER:
            raise PermissionDeniedError("Only a workspace owner can grant ownership.")

        membership = await self._memberships.get_any_for_user(user_id)
        if membership is None:
            raise ValidationError("That person has never been a member of this workspace.")
        if membership.is_active:
            raise ConflictError(
                "That person is already a member of this workspace.", error_code="already_member"
            )

        membership.status = MembershipStatus.ACTIVE
        membership.role = role
        membership.revoked_at = None
        membership.revoked_by_id = None
        await self._session.flush()

        target = await self._users.get_by_id(user_id)
        self._audit.record(
            AuditAction.MEMBER_REINSTATED,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="membership",
            target_id=membership.id,
            target_label=target.email if target is not None else None,
            meta={"role": role.value},
        )
        logger.info(
            "membership.reinstated",
            extra={
                "event": "membership.reinstated",
                "tenant_id": str(self._tenant_id),
                "user_id": str(user_id),
                "actor_id": str(actor.id),
            },
        )
        return membership

    async def change_role(
        self,
        *,
        actor: User,
        actor_role: TenantRole,
        user_id: uuid.UUID,
        role: TenantRole,
    ) -> Membership:
        """Move an active member between member and administrator (FEB-42).

        The rules, and what each one is protecting:

        - **Owner or administrator only.** The dependency already says so; the
          service repeats it because it is the service that is reused.
        - **Never to or from owner.** Ownership has its own hand-over, which
          keeps the last-owner rule in one place.
        - **Never your own role.** Leaving and the ownership transfer exist for
          that; a self-demotion by mistake strands nobody but is still a
          surprise, and a self-promotion is not a thing an administrator has.
        - **An administrator may promote a member but not demote a peer.** The
          same authority the invitation and reinstatement paths already give
          an administrator, and no more: a peer demoting a peer is a takeover.
        - **A removed person is refused.** Their role is set when they are
          readmitted.
        - **The same role again is answered unchanged and not audited.**

        Read under the tenant row lock, the queue removal, reinstatement and
        ownership transfer stand in, so a removal racing this change cannot
        leave a revoked row with a fresh role or an audit row for a change
        that did not stand. Authorization loads the membership on every
        request, so the person's next request carries the new role.
        """
        if actor_role not in (TenantRole.TENANT_OWNER, TenantRole.TENANT_ADMIN):
            raise PermissionDeniedError("This action requires a different role in this workspace.")
        if role not in ASSIGNABLE_ROLES:
            raise ConflictError(
                "Ownership is handed over, not assigned: use the ownership transfer.",
                error_code=MEMBER_ROLE_OWNER,
            )
        if user_id == actor.id:
            raise ConflictError("You cannot change your own role.", error_code=MEMBER_ROLE_SELF)

        await self._tenants.lock(self._tenant_id)

        membership = await self._memberships.get_any_for_user(user_id)
        if membership is None:
            raise ValidationError("That person is not a member of this workspace.")
        if not membership.is_active:
            raise ConflictError(
                "That person is no longer a member of this workspace.",
                error_code=MEMBER_NOT_ACTIVE,
            )
        if membership.role is TenantRole.TENANT_OWNER:
            raise ConflictError(
                "Ownership is handed over, not assigned: use the ownership transfer.",
                error_code=MEMBER_ROLE_OWNER,
            )
        if membership.role is role:
            return membership
        if membership.role is TenantRole.TENANT_ADMIN and actor_role is not TenantRole.TENANT_OWNER:
            raise PermissionDeniedError(
                "Only a workspace owner can change an administrator's role.",
                error_code=MEMBER_ROLE_ADMIN_OWNER_REQUIRED,
            )

        previous = membership.role
        membership.role = role
        await self._session.flush()

        target = await self._users.get_by_id(user_id)
        self._audit.record(
            AuditAction.MEMBER_ROLE_CHANGED,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="membership",
            target_id=membership.id,
            target_label=target.email if target is not None else None,
            meta={"from": previous.value, "to": role.value},
        )
        logger.info(
            "membership.role_changed",
            extra={
                "event": "membership.role_changed",
                "tenant_id": str(self._tenant_id),
                "user_id": str(user_id),
                "actor_id": str(actor.id),
            },
        )
        return membership
