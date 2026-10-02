"""A person's marketing consent on one channel (ENT-19, ADR-131).

A customer who says STOP on WhatsApp has stopped WhatsApp marketing - from
every WhatsApp number of the workspace - and nothing else: the same person on
Instagram or Messenger is still reachable there until they say so there. So an
opt-out belongs to the pair (contact, channel), never to the contact alone and
never to one connection.

One row per contact and channel, created the first time either an opt-out or a
resume is recorded for that pair, and only ever through `app.services.opt_out`:

- ``marketing_opt_out_at`` - when this person asked to stop on this channel; a
  timestamp, because "since when" is what a dispute turns on. Never moved
  forward by a second stop.
- ``opt_out_source`` / ``opt_out_via`` - who decided and by which evidence, the
  vocabulary every opt-out has always carried (OMNI-030, OMNI-046).
- ``resumed_at`` / ``resume_source`` / ``resume_via`` - the last re-admission on
  this channel: a colleague clearing the opt-out, or the customer resuming
  through the provider. A replay of older evidence never overrides a newer
  resume (OMNI-030).

Opt-out is the only thing recorded. Opt-*in* is not a column, because a
campaign can only reach someone who has written to this business on that
channel at all - the audience is built from conversations, and there is no
route that uploads a list of numbers (CAMPAIGNS.md).

The key carries the workspace, and the contact is reached through the
workspace-agreed key `contacts (tenant_id, id)`, so a consent can never name
another workspace's contact.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin
from app.db.models.campaign import (
    OPT_OUT_SOURCE_TYPE,
    OPT_OUT_VIA_TYPE,
    OptOutSource,
    OptOutVia,
)
from app.db.models.channel import CHANNEL_TYPE, Channel


class ContactChannelConsent(Base, TenantScopedMixin, TimestampMixin):
    """Whether one contact accepts marketing on one channel."""

    __tablename__ = "contact_channel_consents"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "contact_id", "channel", name="pk_contact_channel_consents"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_contact_channel_consents_tenant_contact",
            ondelete="CASCADE",
        ),
        # Restated, not inherited: see TenantScopedMixin.
        Index("ix_contact_channel_consents_tenant_id", "tenant_id"),
        # An opt-out is recorded with who decided it, or not at all.
        CheckConstraint(
            "(marketing_opt_out_at IS NULL) = (opt_out_source IS NULL)",
            name="opt_out_has_source",
        ),
        CheckConstraint(
            "opt_out_via IS NULL OR marketing_opt_out_at IS NOT NULL", name="via_needs_opt_out"
        ),
        CheckConstraint(
            "(resume_source IS NULL AND resume_via IS NULL) OR resumed_at IS NOT NULL",
            name="resume_provenance_needs_resume",
        ),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[Channel] = mapped_column(CHANNEL_TYPE, nullable=False)
    marketing_opt_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opt_out_source: Mapped[OptOutSource | None] = mapped_column(OPT_OUT_SOURCE_TYPE, nullable=True)
    opt_out_via: Mapped[OptOutVia | None] = mapped_column(OPT_OUT_VIA_TYPE, nullable=True)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resume_source: Mapped[OptOutSource | None] = mapped_column(OPT_OUT_SOURCE_TYPE, nullable=True)
    resume_via: Mapped[OptOutVia | None] = mapped_column(OPT_OUT_VIA_TYPE, nullable=True)

    @property
    def accepts_marketing(self) -> bool:
        """Whether marketing may reach this person on this channel."""
        return self.marketing_opt_out_at is None


__all__ = ["ContactChannelConsent"]
