"""Tenant data access."""

from __future__ import annotations

import uuid

from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import Tenant, TenantStatus
from app.repositories.base import BaseRepository


def normalise_slug(slug: str) -> str:
    """Workspace addresses are case-insensitive, so they are stored lower-cased."""
    return slug.strip().lower()


class TenantRepository(BaseRepository[Tenant]):
    """Tenants themselves.

    Not tenant-scoped, by nature: this repository is how a tenant is found in
    the first place. Callers are still responsible for checking that the
    requesting user holds a membership in whatever this returns.
    """

    model = Tenant

    async def get_by_id(self, tenant_id: uuid.UUID) -> Tenant | None:
        return await self._first(self._select().where(Tenant.id == tenant_id))

    async def get_by_slug(self, slug: str) -> Tenant | None:
        """Including tombstoned workspaces, and that is deliberate.

        A deleted workspace keeps its address for ever. Freeing it would let
        somebody register the slug a customer's invitation links, support
        tickets and bookmarks all still name, and inherit the trust attached to
        it - so the unique constraint covers deleted rows and this read has to
        see them or it would promise a slug the insert then refuses.
        """
        return await self._first(self._select().where(Tenant.slug == normalise_slug(slug)))

    async def lock(self, tenant_id: uuid.UUID) -> Tenant | None:
        """The workspace row, held under ``FOR UPDATE`` until this transaction ends.

        The serialisation point for every operation that must not interleave
        with another one on the same workspace: transferring ownership, leaving,
        removing the last owner, and deleting the workspace outright. All of
        them read a count of owners and then act on it, and a count read outside
        a lock is a decision made about a state that another transaction is in
        the middle of changing - two owners leaving at once both see "two
        owners, safe to go" and the workspace ends up with none.

        The tenant row rather than the membership rows, because the invariant is
        about the *set* of memberships and a lock on the rows you can see cannot
        stop a row you cannot see from appearing. One row per workspace also
        means these operations queue behind each other rather than deadlocking
        in an order that depends on which user id sorts first.

        A plain ``FOR UPDATE``: the caller wants this workspace, so skipping a
        locked row would silently do nothing, and ``NOWAIT`` would turn a
        half-second overlap into a failed request. These operations are rare and
        waiting is the correct behaviour.
        """
        return await self._first(self._select().where(Tenant.id == tenant_id).with_for_update())

    async def require_by_slug(self, slug: str) -> Tenant:
        tenant = await self.get_by_slug(slug)
        if tenant is None:
            raise NotFoundError("No workspace matches that address.")
        return tenant

    async def list_active(self, *, limit: int = 50) -> list[Tenant]:
        """Live workspaces only: suspended and soft-deleted ones are excluded."""
        statement = (
            self._select()
            .where(Tenant.status == TenantStatus.ACTIVE, Tenant.deleted_at.is_(None))
            .order_by(Tenant.name)
            .limit(limit)
        )
        return await self._all(statement)

    async def create(self, *, name: str, slug: str) -> Tenant:
        normalised = normalise_slug(slug)
        if await self.get_by_slug(normalised) is not None:
            # The unique constraint is the real guard; this check exists only to
            # return a useful conflict instead of an integrity error.
            raise ConflictError("That workspace address is already taken.")
        return self.add(Tenant(name=name.strip(), slug=normalised, status=TenantStatus.ACTIVE))
