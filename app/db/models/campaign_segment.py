"""Saved campaign segments: named audience rules a workspace reuses (PG-06, ADR-149).

A segment is **rules, not a list**: how recently a person wrote and which lead
statuses - the same filters an audience is written from, never a set of
contact ids. Using one copies its rules onto the campaign at the moment the
audience is written, so editing or deleting the segment afterwards never
changes a campaign that used it.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

MAX_SEGMENT_NAME_LENGTH: Final = 100
MAX_SEGMENT_DESCRIPTION_LENGTH: Final = 500
#: How many segments one workspace may keep.
MAX_SEGMENTS_PER_WORKSPACE: Final = 100


class CampaignSegment(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One workspace's named audience rules. 0116."""

    __tablename__ = "campaign_segments"
    __table_args__ = (
        Index("ix_campaign_segments_tenant_id", "tenant_id"),
        UniqueConstraint("tenant_id", "name", name="uq_campaign_segments_tenant_id_name"),
        # The target of a campaign's tenant-composite key (ADR-100).
        UniqueConstraint("tenant_id", "id", name="uq_campaign_segments_tenant_id_id"),
        # A number of the segment's own workspace, or any number (null).
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            name="fk_campaign_segments_tenant_account",
            ondelete="CASCADE",
        ),
        # Rules, never a list of people (PG-06).
        CheckConstraint("NOT (rules ? 'contact_ids')", name="rules_have_no_contacts"),
    )

    name: Mapped[str] = mapped_column(String(MAX_SEGMENT_NAME_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `{"last_inbound_within_days": int | null, "lead_statuses": [...]}`.
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


__all__ = [
    "MAX_SEGMENTS_PER_WORKSPACE",
    "MAX_SEGMENT_DESCRIPTION_LENGTH",
    "MAX_SEGMENT_NAME_LENGTH",
    "CampaignSegment",
]
