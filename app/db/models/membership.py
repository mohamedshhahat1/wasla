"""Membership model.

A membership is the only link between a user and a tenant, and the only place a
tenant role is stored.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import TENANT_ROLE_TYPE, TenantRole


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """One user's role in one tenant.

    The unique constraint stops a user from holding two roles in the same
    tenant, so role resolution is never ambiguous. Both foreign keys cascade:
    removing a user or a tenant must not leave a grant behind.
    """

    __tablename__ = "memberships"
    # Declared here rather than inherited from the mixin, because this table
    # needs its uniqueness rule alongside the tenant index.
    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_memberships_user_id_tenant_id"),
        Index("ix_memberships_tenant_id", "tenant_id"),
        Index("ix_memberships_user_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[TenantRole] = mapped_column(TENANT_ROLE_TYPE, nullable=False)

    @property
    def can_administer_tenant(self) -> bool:
        """Whether this membership may change tenant-wide settings."""
        return self.role in (TenantRole.TENANT_OWNER, TenantRole.TENANT_ADMIN)
