"""Membership data access."""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement

from app.db.models import Membership, TenantRole
from app.repositories.base import BaseRepository, TenantScopedRepository


class MembershipRepository(TenantScopedRepository[Membership]):
    """Memberships inside one tenant."""

    model = Membership

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Membership.tenant_id == self.tenant_id

    async def get_for_user(self, user_id: uuid.UUID) -> Membership | None:
        return await self._first(self._select().where(Membership.user_id == user_id))

    async def require_for_user(self, user_id: uuid.UUID) -> Membership:
        """The membership that authorises this user here, or a 404."""
        return await self._require(self._select().where(Membership.user_id == user_id))

    async def list_members(self) -> list[Membership]:
        return await self._all(self._select().order_by(Membership.created_at))

    async def add_member(self, *, user_id: uuid.UUID, role: TenantRole) -> Membership:
        """Grant a role.

        The tenant comes from this repository, never from caller input, so a
        forged tenant id in a request cannot place a membership elsewhere.
        """
        return self.add(Membership(tenant_id=self.tenant_id, user_id=user_id, role=role))


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
        statement = (
            self._select().where(Membership.user_id == user_id).order_by(Membership.created_at)
        )
        return await self._all(statement)
