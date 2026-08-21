"""Scheduled nudges: a message to send later, unless the customer speaks first.

A follow-up is a durable row rather than a delayed job in Redis, and that is the
central decision here (ADR-022). It has to be cancellable by a person, visible in
an interface, auditable after the fact, and correct across a restart — all of
which want a table. Putting the schedule in Redis as well would create a second
source of truth that drifts from the first the moment one of them is written
without the other.

The row therefore carries everything the send needs: what to say inside the
service window, which approved template to use outside it, and why it was
scheduled at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type
from app.db.models.lead import ACTOR_KIND_TYPE, ActorKind

# A nudge that has been retried this often is not going to succeed. Kept low:
# each attempt is a message the customer might receive, so an over-eager retry
# is worse than giving up and leaving the row for someone to look at.
MAX_ATTEMPTS: Final = 3

MAX_BODY_LENGTH: Final = 4096
MAX_REASON_LENGTH: Final = 300


class FollowUpStatus(StrEnum):
    """Where a scheduled nudge ended up.

    `SKIPPED` and `FAILED` are deliberately different. `FAILED` means the send
    was attempted and something broke — Meta rejected it, the network went away
    — and trying again may well work. `SKIPPED` means Wasla decided not to send:
    the service window had closed and no approved template was configured, so
    sending would have breached WhatsApp's rules. That is a policy outcome, not
    an error, and retrying it can never succeed. Collapsing the two would invite
    a retry loop against a wall.
    """

    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SKIPPED = "skipped"


# Statuses that are finished. A follow-up in one of these no longer occupies the
# "one pending nudge per conversation" slot.
TERMINAL_FOLLOW_UP_STATUSES: Final[frozenset[FollowUpStatus]] = frozenset(
    {
        FollowUpStatus.SENT,
        FollowUpStatus.CANCELLED,
        FollowUpStatus.FAILED,
        FollowUpStatus.SKIPPED,
    },
)

FOLLOW_UP_STATUS_TYPE = _enum_type(FollowUpStatus, name="follow_up_status")


class FollowUp(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One scheduled message, waiting for its moment or for a reason not to send.

    At most one follow-up per conversation is `PENDING`, enforced by a partial
    unique index. An agent that decides to schedule a nudge on every turn would
    otherwise queue five of them at one customer; scheduling again while one is
    pending reschedules that row instead.

    `body` is used inside the 24-hour service window. `template_name` and
    `template_language` are used outside it, where Meta accepts approved
    templates only. A follow-up with neither usable option is `SKIPPED` rather
    than sent, which is the whole point of storing both.
    """

    __tablename__ = "follow_ups"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        Index("ix_follow_ups_tenant_id", "tenant_id"),
        Index("ix_follow_ups_tenant_id_status", "tenant_id", "status"),
        Index("ix_follow_ups_conversation_id", "conversation_id"),
        Index("ix_follow_ups_lead_id", "lead_id"),
        Index("ix_follow_ups_tenant_id_created_at", "tenant_id", "created_at"),
        # The worker's only query: pending rows whose time has come, oldest
        # first. Partial, because everything else in the table is finished work
        # and scanning it would grow the sweep without bound as history piles up.
        Index(
            "ix_follow_ups_due",
            "scheduled_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # One waiting nudge per conversation. Partial, so a conversation that has
        # already been followed up can be followed up again later.
        Index(
            "uq_follow_ups_pending_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Optional: a follow-up may be about an opportunity, or may simply be a
    # conversation nobody wants to drop. Nulled rather than cascaded if the lead
    # goes, because the nudge is still owed to the customer either way.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[FollowUpStatus] = mapped_column(
        FOLLOW_UP_STATUS_TYPE,
        nullable=False,
        default=FollowUpStatus.PENDING,
    )

    # What to send inside the service window.
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What to send outside it. Free text until Phase 11 gives templates a
    # registry of their own; nothing here can confirm Meta has approved it.
    template_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    template_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    template_components: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # Why this was scheduled — "the customer said they would think about it".
    # Written for a colleague reading the conversation later, not for the model.
    reason: Mapped[str | None] = mapped_column(String(MAX_REASON_LENGTH), nullable=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_kind: Mapped[ActorKind] = mapped_column(
        ACTOR_KIND_TYPE,
        nullable=False,
        default=ActorKind.USER,
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(String(MAX_REASON_LENGTH), nullable=True)

    # The message actually sent, once there is one. Nulled rather than cascaded
    # so deleting a message cannot erase the record that a nudge went out.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    @property
    def is_pending(self) -> bool:
        return self.status is FollowUpStatus.PENDING

    @property
    def has_template(self) -> bool:
        """Whether this can be sent outside the service window at all."""
        return bool(self.template_name and self.template_language)

    @property
    def is_exhausted(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS
