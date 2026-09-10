"""Membership data access.

**Every read here is filtered to active memberships unless its name says
otherwise.** That is the enforcement point for revocation (ADR-038): the
authorization dependency calls `require_for_user` on every request, so a
membership whose status is no longer active stops authorising at the next
request rather than at the next token expiry.

The two reads that deliberately see revoked rows - `get_any_for_user` and
`list_members` - are named so they cannot be reached for by accident, and both
exist for administration screens rather than for access decisions.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, Select, select

from app.db.models import Membership, MembershipStatus, Tenant, TenantRole
from app.repositories.base import BaseRepository, TenantScopedRepository


class MembershipRepository(TenantScopedRepository[Membership]):
    """Memberships inside one tenant."""

    model = Membership

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Membership.tenant_id == self.tenant_id

    def _active(self) -> Select[tuple[Membership]]:
        """The default read. Revoked rows are invisible to it."""
        return self._select().where(Membership.status == MembershipStatus.ACTIVE)

    async def get_for_user(self, user_id: uuid.UUID) -> Membership | None:
        """The membership that currently authorises this user here, if any."""
        return await self._first(self._active().where(Membership.user_id == user_id))

    async def require_for_user(self, user_id: uuid.UUID) -> Membership:
        """The membership that authorises this user here, or a 404.

        A revoked membership is not found rather than refused, matching every
        other scoped lookup: a caller learns that they cannot act, not whether
        a row exists that once let them.
        """
        return await self._require(self._active().where(Membership.user_id == user_id))

    async def get_any_for_user(self, user_id: uuid.UUID) -> Membership | None:
        """Including revoked rows. For administration, never for access.

        Re-admitting somebody reuses their existing row - the unique constraint
        requires it - so the invitation path needs to see a revoked membership
        in order to reactivate it.
        """
        return await self._first(self._select().where(Membership.user_id == user_id))

    async def list_members(self, *, include_revoked: bool = False) -> list[Membership]:
        """The workspace's roster, oldest first.

        Revoked rows are excluded by default so that the common caller - "who is
        in this workspace" - cannot accidentally count somebody who was removed.
        """
        statement = self._select() if include_revoked else self._active()
        return await self._all(statement.order_by(Membership.created_at))

    async def count_active_owners(self) -> int:
        """How many people can still administer this workspace at the top level.

        Read before a removal or a demotion. A workspace with no owner has
        nobody who can invite one, which makes it unrecoverable without
        platform intervention.
        """
        owners = await self._all(
            self._active().where(Membership.role == TenantRole.TENANT_OWNER),
        )
        return len(owners)

    async def list_active_owners(self) -> list[Membership]:
        """Everyone who currently holds ownership of this workspace.

        The rows rather than a count, because the callers that need this need to
        know *who*: an ownership transfer has to tell an owner apart from the
        person they are handing it to, and a last-owner refusal is only correct
        if the one remaining owner is the caller themselves.

        Read under the tenant lock (see ``TenantRepository.lock``) wherever the
        answer is about to be acted on. Read outside it this is a snapshot, and
        a snapshot of an invariant is not the invariant.
        """
        return await self._all(
            self._active()
            .where(Membership.role == TenantRole.TENANT_OWNER)
            .order_by(Membership.created_at)
        )

    async def add_member(self, *, user_id: uuid.UUID, role: TenantRole) -> Membership:
        """Grant a role.

        The tenant comes from this repository, never from caller input, so a
        forged tenant id in a request cannot place a membership elsewhere.
        """
        return self.add(
            Membership(
                tenant_id=self.tenant_id,
                user_id=user_id,
                role=role,
                status=MembershipStatus.ACTIVE,
            )
        )


class UserMembershipRepository(BaseRepository[Membership]):
    """One user's memberships across every tenant.

    This is the only read in the tenancy layer that spans tenants, and it is
    deliberately kept in its own class. It is always keyed by the authenticated
    user's own id, which is what makes it safe, and separating it means
    :class:`MembershipRepository` has no unfiltered read path at all.

    It answers one question: which workspaces may this person open?
    """

    model = Membership

    async def list_for_user(self, user_id: uuid.UUID) -> list[Membership]:
        """Active memberships only.

        This backs workspace listing and workspace selection, both of which are
        access decisions: a revoked workspace must disappear from the switcher
        at the same moment it stops answering requests, or somebody keeps a dead
        entry in their sidebar and reads a 404 as a bug.
        """
        statement = (
            self._select()
            .where(
                Membership.user_id == user_id,
                Membership.status == MembershipStatus.ACTIVE,
            )
            .order_by(Membership.created_at)
        )
        return await self._all(statement)

    async def list_owned_live_workspaces(
        self, user_id: uuid.UUID
    ) -> list[tuple[Membership, Tenant]]:
        """Workspaces this person owns that are still alive, with the tenant row.

        Read before an account is deleted. Closing an account must not strand a
        workspace with no owner, and answering that question needs the tenant
        alongside the membership: a suspended or already-tombstoned workspace is
        not something the departing owner has to resolve first, and asking them
        to hand over a workspace that no longer exists would make deletion
        impossible for exactly the people most likely to want it.

        Joined rather than looked up per membership, because the caller is about
        to render every row of this to somebody as a list of things they must
        deal with, and a query per workspace to build one refusal is a query per
        workspace on a path that is already refusing.
        """
        statement = (
            select(Membership, Tenant)
            .join(Tenant, Tenant.id == Membership.tenant_id)
            .where(
                Membership.user_id == user_id,
                Membership.status == MembershipStatus.ACTIVE,
                Membership.role == TenantRole.TENANT_OWNER,
                Tenant.deleted_at.is_(None),
            )
            .order_by(Tenant.name)
        )
        rows = await self.session.execute(statement)
        return [(membership, tenant) for membership, tenant in rows]

    async def list_all_for_user(self, user_id: uuid.UUID) -> list[Membership]:
        """Every membership this person holds, revoked ones included.

        For account deletion, which withdraws them all. Deliberately unfiltered:
        the active-only read above is the access decision, and this one is the
        cleanup that must not miss a row merely because it was already inert.
        """
        return await self._all(self._select().where(Membership.user_id == user_id))
