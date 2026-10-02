"""The raw inbound event log, for every channel.

One row per event a provider delivered, stored before it is interpreted: a
message, a delivery status, an echo of something the business sent. The payload
is kept whole because providers add fields over time and a log that discards
what it does not yet understand cannot be replayed later; it is cleared on a
bounded schedule once the event is processed (DB-011).

**This is the table recovery and retention read, whatever the channel** (ADR-102,
OMNI-007). An adapter stores what it parsed here and the same claim, settlement,
recovery sweep and payload retention apply to it unchanged - none of them is
reimplemented per channel.

The physical table is still called `whatsapp_events`: it was WhatsApp's log
before it was everybody's, and renaming a table this hot is a compatibility
cleanup (O8) rather than something to do beside a behaviour change. The ORM
names it for what it now is, and `WhatsAppEvent` remains as an alias.

**Idempotency is per connection** - `UNIQUE(tenant_id, account_id, event_id)` -
because a provider's event id is only guaranteed within the connection it
arrived on (OMNI-007). The workspace-wide key it replaces is kept until the
compatibility cleanup; it is stricter, so WhatsApp loses nothing, and an event
it would refuse across two connections is counted as a collision rather than
silently dropped (ADR-120).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.channel import CHANNEL_TYPE, Channel
from app.db.models.enums import _enum_type


class ChannelEventKind(StrEnum):
    """What the provider sent. `UNSUPPORTED` is kept rather than dropped.

    `ECHO` is a message the *business* sent, reported back by the provider -
    Messenger and Instagram `is_echo`, WhatsApp Coexistence
    `smb_message_echoes`. It is stored as evidence and never projected as a
    customer's message: no conversation is opened or reopened by it, no window
    is opened, and no agent is asked to answer it (OMNI-005).
    """

    MESSAGE = "message"
    STATUS = "status"
    UNSUPPORTED = "unsupported"
    ECHO = "echo"
    #: The person changed their marketing preference through the provider -
    #: WhatsApp's `user_preferences` stop or resume (OMNI-046).
    PREFERENCE = "preference"


class ChannelEventState(StrEnum):
    """How far an event has travelled through processing."""

    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"


# The database type names predate the table serving more than WhatsApp and are
# kept: a type rename buys nothing a reader of the code can see.
CHANNEL_EVENT_KIND_TYPE = _enum_type(ChannelEventKind, name="whatsapp_event_kind")
CHANNEL_EVENT_STATE_TYPE = _enum_type(ChannelEventState, name="whatsapp_event_state")


class ChannelEvent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One raw inbound event, stored before it is interpreted."""

    __tablename__ = "whatsapp_events"
    # See WhatsAppAccount: the inherited tenant index does not survive a
    # class-body __table_args__, so it is restated.
    __table_args__ = (
        # Legacy, kept until the compatibility cleanup (ADR-120).
        UniqueConstraint(
            "tenant_id",
            "event_id",
            name="uq_whatsapp_events_tenant_id_event_id",
        ),
        # The idempotency scope every channel is judged by.
        UniqueConstraint(
            "tenant_id",
            "account_id",
            "event_id",
            name="uq_whatsapp_events_tenant_id_account_id_event_id",
        ),
        Index("ix_whatsapp_events_tenant_id", "tenant_id"),
        Index("ix_whatsapp_events_account_id", "account_id"),
        Index("ix_whatsapp_events_tenant_id_state", "tenant_id", "state"),
        # What the retention sweep reads (DB-011): processed events whose raw
        # payload is still held, oldest first. Partial, so it holds only the
        # backlog, not every event the platform ever received.
        Index(
            "ix_whatsapp_events_redactable",
            "processed_at",
            postgresql_where=text("state = 'processed' AND payload IS NOT NULL"),
        ),
        # The connection the event arrived on, in the event's own workspace and
        # on the event's own channel (ADR-100). This replaced a key naming
        # `whatsapp_accounts.id` alone, which a row in another workspace could
        # satisfy.
        ForeignKeyConstraint(
            ["tenant_id", "account_id", "channel"],
            [
                "channel_connections.tenant_id",
                "channel_connections.id",
                "channel_connections.channel",
            ],
            name="fk_whatsapp_events_tenant_connection",
            ondelete="CASCADE",
        ),
        # A payload is gone only because retention removed it, and says when.
        CheckConstraint(
            "payload IS NOT NULL OR payload_redacted_at IS NOT NULL",
            name="payload_present_or_redacted",
        ),
    )

    # The connection the event arrived on. Named `account_id` for the
    # compatibility window, like `conversations.account_id`.
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    channel: Mapped[Channel] = mapped_column(
        CHANNEL_TYPE,
        nullable=False,
        server_default=Channel.WHATSAPP.value,
    )
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[ChannelEventKind] = mapped_column(CHANNEL_EVENT_KIND_TYPE, nullable=False)
    state: Mapped[ChannelEventState] = mapped_column(
        CHANNEL_EVENT_STATE_TYPE,
        nullable=False,
        default=ChannelEventState.RECEIVED,
    )
    # The event exactly as the provider sent it, until retention clears it
    # (DB-011). NULL only with `payload_redacted_at` set.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    payload_redacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


__all__ = [
    "CHANNEL_EVENT_KIND_TYPE",
    "CHANNEL_EVENT_STATE_TYPE",
    "ChannelEvent",
    "ChannelEventKind",
    "ChannelEventState",
]
