"""Contacts, conversations and messages.

These three live in one module because they are one aggregate: a message has no
meaning outside a conversation, and a conversation has no meaning without the
contact it is with.

The WhatsApp event log stays separate on purpose. Events are what Meta sent;
these tables are what Wasla concluded. Keeping the raw log means a projection
bug can be fixed and replayed rather than losing the traffic.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.campaign import OPT_OUT_SOURCE_TYPE, OptOutSource
from app.db.models.enums import _enum_type
from app.db.models.sentiment import (
    CONVERSATION_PRIORITY_TYPE,
    MAX_INTENT_LENGTH,
    SENTIMENT_LABEL_TYPE,
    ConversationPriority,
    SentimentLabel,
)


class ConversationStatus(StrEnum):
    """Where a conversation sits in the queue of work."""

    OPEN = "open"
    PENDING = "pending"
    CLOSED = "closed"


class ConversationMode(StrEnum):
    """Who answers. `HUMAN` stops automatic AI replies entirely."""

    AI = "ai"
    HUMAN = "human"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageKind(StrEnum):
    """Mirrors the WhatsApp message types Wasla handles."""

    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    LOCATION = "location"
    INTERACTIVE = "interactive"
    # Outbound only. Meta renders an approved template from its own copy, so
    # what Wasla holds is the name and language it asked for, never the text the
    # customer read.
    TEMPLATE = "template"
    UNSUPPORTED = "unsupported"


class MessageStatus(StrEnum):
    """Delivery state, advanced by webhook status events.

    Inbound messages are `RECEIVED` and stay there: the states after it describe
    Wasla's own sends.
    """

    RECEIVED = "received"
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


CONVERSATION_STATUS_TYPE = _enum_type(ConversationStatus, name="conversation_status")
CONVERSATION_MODE_TYPE = _enum_type(ConversationMode, name="conversation_mode")
MESSAGE_DIRECTION_TYPE = _enum_type(MessageDirection, name="message_direction")
MESSAGE_KIND_TYPE = _enum_type(MessageKind, name="message_kind")
MESSAGE_STATUS_TYPE = _enum_type(MessageStatus, name="message_status")


class Contact(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A customer, identified by the WhatsApp id Meta reports.

    Unique per workspace rather than globally: the same person may be a customer
    of two businesses on the platform, and those must be separate records.
    """

    __tablename__ = "contacts"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        UniqueConstraint("tenant_id", "wa_id", name="uq_contacts_tenant_id_wa_id"),
        Index("ix_contacts_tenant_id", "tenant_id"),
    )

    wa_id: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # When this person asked to stop receiving campaigns, and who recorded it.
    # A timestamp rather than a boolean: "since when" is the question a dispute
    # about a marketing message actually turns on.
    marketing_opt_out_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    opt_out_source: Mapped[OptOutSource | None] = mapped_column(
        OPT_OUT_SOURCE_TYPE,
        nullable=True,
    )

    @property
    def accepts_campaigns(self) -> bool:
        """Whether a broadcast may include this person.

        Opt-out is the only thing checked here. Opt-*in* is not a column,
        because a campaign can only reach someone who has written to this
        business at all — the audience is built from conversations, and there is
        no route that uploads a list of numbers. See CAMPAIGNS.md.
        """
        return self.marketing_opt_out_at is None


class Conversation(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One customer talking to one connected WhatsApp number.

    Scoped by account as well as contact, because a business with a sales number
    and a support number is holding two genuinely separate conversations with
    the same person.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "contact_id",
            "account_id",
            name="uq_conversations_tenant_id_contact_id_account_id",
        ),
        Index("ix_conversations_tenant_id", "tenant_id"),
        Index("ix_conversations_tenant_id_status", "tenant_id", "status"),
        Index("ix_conversations_tenant_id_last_message_at", "tenant_id", "last_message_at"),
        Index("ix_conversations_tenant_id_priority", "tenant_id", "priority"),
        Index("ix_conversations_contact_id", "contact_id"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[ConversationStatus] = mapped_column(
        CONVERSATION_STATUS_TYPE,
        nullable=False,
        default=ConversationStatus.OPEN,
    )
    mode: Mapped[ConversationMode] = mapped_column(
        CONVERSATION_MODE_TYPE,
        nullable=False,
        default=ConversationMode.AI,
    )
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    handoff_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Denormalised deliberately. Every outbound send checks the 24-hour service
    # window, and that check must not depend on scanning the message table.
    last_inbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # The most recent reading of how the customer sounds. Current state only;
    # every reading is kept on `message_sentiments`, which is where a history
    # or a count over time comes from.
    sentiment: Mapped[SentimentLabel | None] = mapped_column(
        SENTIMENT_LABEL_TYPE,
        nullable=True,
    )
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Raised by a negative reading, never lowered by a positive one. See
    # `raised_priority`: giving it back is a person's decision.
    priority: Mapped[ConversationPriority] = mapped_column(
        CONVERSATION_PRIORITY_TYPE,
        nullable=False,
        default=ConversationPriority.NORMAL,
        server_default=ConversationPriority.NORMAL.value,
    )
    intent: Mapped[str | None] = mapped_column(String(MAX_INTENT_LENGTH), nullable=True)
    intent_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    @property
    def is_ai_handled(self) -> bool:
        return self.mode is ConversationMode.AI

    @property
    def needs_attention(self) -> bool:
        """Whether this conversation should be surfaced ahead of the queue."""
        return self.priority is not ConversationPriority.NORMAL


class Message(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One message in either direction.

    `wa_message_id` is nullable because an outbound row is written before Meta is
    called: a send that fails must still leave evidence that it was attempted.
    It is unique per workspace once set, which is what makes status projection
    and inbound replay idempotent.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "wa_message_id",
            name="uq_messages_tenant_id_wa_message_id",
        ),
        Index("ix_messages_tenant_id", "tenant_id"),
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    wa_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    direction: Mapped[MessageDirection] = mapped_column(MESSAGE_DIRECTION_TYPE, nullable=False)
    kind: Mapped[MessageKind] = mapped_column(
        MESSAGE_KIND_TYPE,
        nullable=False,
        default=MessageKind.TEXT,
    )
    status: Mapped[MessageStatus] = mapped_column(
        MESSAGE_STATUS_TYPE,
        nullable=False,
        default=MessageStatus.PENDING,
    )
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set on TEMPLATE messages and null everywhere else. Kept as columns rather
    # than folded into `body`, because a follow-up or a campaign has to be able
    # to ask which template it sent without parsing prose back out of a string.
    template_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    template_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Set when a human or an agent sent it; null for customer messages.
    sent_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
