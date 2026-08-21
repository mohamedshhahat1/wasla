"""Tenant model.

A tenant is one customer workspace, and the isolation boundary for every piece
of business data in the platform.
"""

from __future__ import annotations

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import TENANT_STATUS_TYPE, TenantStatus


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A customer workspace.

    Tenants are soft-deleted: business data outlives the decision to close an
    account, and billing or audit questions arrive after the fact.
    """

    __tablename__ = "tenants"
    __table_args__ = (UniqueConstraint("slug", name="uq_tenants_slug"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[TenantStatus] = mapped_column(
        TENANT_STATUS_TYPE,
        nullable=False,
        default=TenantStatus.ACTIVE,
    )

    @property
    def is_active(self) -> bool:
        """True only while the workspace may be used.

        Suspension and soft deletion are separate states, and either one is
        enough to stop service.
        """
        return self.status is TenantStatus.ACTIVE and self.deleted_at is None
