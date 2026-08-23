"""Membership model.

A membership is the only link between a user and a tenant, and the only place a
tenant role is stored.

It also carries a *status*, and that column is what makes access withdrawable
(ADR-038). Deleting the row would work too, and would be worse: it takes with it
the answer to "who removed them, and when", it makes a re-invitation
indistinguishable from a first one, and it turns an accidental removal into a
loss rather than a mistake. A revoked membership is kept and ignored.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import (
    MEMBERSHIP_STATUS_TYPE,
    TENANT_ROLE_TYPE,
    MembershipStatus,
    TenantRole,
)


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """One user's role in one tenant.

    The unique constraint stops a user from holding two roles in the same
    tenant, so role resolution is never ambiguous. It covers revoked rows too:
    somebody re-admitted reuses the row they had rather than accumulating a
    history of parallel grants that authorization would then have to rank.

    Both foreign keys cascade: removing a user or a tenant must not leave a
    grant behind.
    """

    __tablename__ = "memberships"
    # Declared here rather than inherited from the mixin, because this table
    # needs its uniqueness rule alongside the tenant index.
    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_memberships_user_id_tenant_id"),
        Index("ix_memberships_tenant_id", "tenant_id"),
        Index("ix_memberships_user_id", "user_id"),
        # Every authorization decision in the product reads this pair, on every
        # request. See `MembershipRepository.get_for_user`.
        Index("ix_memberships_tenant_id_status", "tenant_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[TenantRole] = mapped_column(TENANT_ROLE_TYPE, nullable=False)
    status: Mapped[MembershipStatus] = mapped_column(
        MEMBERSHIP_STATUS_TYPE,
        nullable=False,
        default=MembershipStatus.ACTIVE,
        server_default=MembershipStatus.ACTIVE.value,
    )
    # When the grant was withdrawn, and by whom. Kept on the row as well as in
    # the audit trail because this is the copy authorization can see: a support
    # question about why somebody lost access is answered here without a join.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # `SET NULL`, never `CASCADE`: deleting the administrator who removed
    # somebody must not delete the record of the removal.
    revoked_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    @property
    def is_active(self) -> bool:
        """Whether this grant currently authorises anything.

        The single predicate every access decision reduces to. Written once
        here so no call site has to remember which statuses count.
        """
        return self.status is MembershipStatus.ACTIVE

    @property
    def can_administer_tenant(self) -> bool:
        """Whether this membership may change tenant-wide settings.

        Includes the active check. A revoked owner is not an owner, and a
        property that answered otherwise would be a trap for the next caller.
        """
        return self.is_active and self.role in (
            TenantRole.TENANT_OWNER,
            TenantRole.TENANT_ADMIN,
        )
