"""Tenant invitation model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import (
    INVITATION_STATUS_TYPE,
    TENANT_ROLE_TYPE,
    InvitationStatus,
    TenantRole,
)


class TenantInvitation(UUIDPrimaryKeyMixin, TimestampMixin, TenantScopedMixin, Base):
    """An invitation for an email address to join a tenant with a role.

    Only a hash of the invitation token is stored, so a database leak cannot be
    replayed to accept invitations: the raw token exists only in the message
    sent to the invitee. The invitee may not have an account yet, which is why
    this table records an email address rather than a user.
    """

    __tablename__ = "tenant_invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_tenant_invitations_token_hash"),
        Index("ix_tenant_invitations_tenant_id", "tenant_id"),
        Index("ix_tenant_invitations_tenant_id_email", "tenant_id", "email"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[TenantRole] = mapped_column(TENANT_ROLE_TYPE, nullable=False)
    status: Mapped[InvitationStatus] = mapped_column(
        INVITATION_STATUS_TYPE,
        nullable=False,
        default=InvitationStatus.PENDING,
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    def is_open(self, *, now: datetime) -> bool:
        """Whether the invitation can still be accepted.

        Expiry is evaluated against a caller-supplied clock so the rule stays
        testable and does not depend on process time.
        """
        return self.status is InvitationStatus.PENDING and self.expires_at > now
