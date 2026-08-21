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
        return await self._first(self._select().where(Tenant.slug == normalise_slug(slug)))

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
