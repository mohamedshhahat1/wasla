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
from typing import Final

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.campaign import OPT_OUT_SOURCE_TYPE, OptOutSource
from app.db.models.enums import _enum_type
from app.db.models.invoice import MAX_IDEMPOTENCY_KEY_LENGTH
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


class MessageOrigin(StrEnum):
    """What produced this line in the transcript.

    Attribution used to be inferred from `sent_by_id`, and the inference was
    wrong twice: a campaign carries its creator, so it read as a human reply,
    and a follow-up carries nobody, so it read as an AI reply (MSG-16). It was
    recoverable by joining `campaign_recipients` or `follow_ups` on
    `message_id` - but not by reading the transcript, which is what an auditor,
    an analytics query and a colleague scrolling the inbox all actually do.

    Total over every message, inbound included, so one column answers the
    question for any row rather than answering it only where a caller
    remembered to set it. `CUSTOMER` is the inbound value; `direction` still
    says which way it went, and this says who caused it.

    The set is open at the end on purpose. WhatsApp Coexistence lets the
    Business App originate messages on a number Wasla also holds, and those are
    neither `HUMAN` (no Wasla user sent them) nor `AGENT`. A `BUSINESS_APP`
    member slots in beside these when that work happens; adding an enum label
    is an `ALTER TYPE`, which is why having the column at all is the part worth
    doing now.
    """

    CUSTOMER = "customer"
    HUMAN = "human"
    AGENT = "agent"
    CAMPAIGN = "campaign"
    FOLLOW_UP = "follow_up"
    # Not produced by anything today. Reserved for a message the platform sends
    # on its own behalf rather than on a workspace's - a service notice - so
    # that when one exists it is not filed as somebody's reply.
    SYSTEM = "system"


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


class MessageDeliveryState(StrEnum):
    """Whether Meta may already have this message, for an outbound send.

    `MessageStatus` answers "what happened to it" and is advanced by webhook
    status events. This answers a different question, and it is the one the
    sending code has to ask before it acts: *may this logical message be sent
    to Meta now, and might Meta already have taken it?* A single status cannot
    carry both, because the honest answer to the second is sometimes "nobody
    knows" while the first still reads `pending` (ADR-093).

    NULL on every inbound message and on every row written before this existed.
    """

    #: Committed, and Meta has not been asked to deliver anything. Provably
    #: nothing reached a customer, so this send may still be made.
    CLAIMED = "claimed"
    #: The request either has been made or is about to be. Meta may have
    #: accepted it, and nothing may send this message again on that basis.
    REQUESTED = "requested"
    #: Meta accepted it and named it. Delivery is Meta's business from here and
    #: is reported by the status webhooks.
    SENT = "sent"
    #: Nothing was delivered and that is known rather than assumed - Meta read
    #: the request and declined it, or the request provably never left this
    #: process. A *new* send may be made; this one is finished.
    UNDELIVERED = "undelivered"


CONVERSATION_STATUS_TYPE = _enum_type(ConversationStatus, name="conversation_status")
CONVERSATION_MODE_TYPE = _enum_type(ConversationMode, name="conversation_mode")
MESSAGE_DIRECTION_TYPE = _enum_type(MessageDirection, name="message_direction")
MESSAGE_KIND_TYPE = _enum_type(MessageKind, name="message_kind")
MESSAGE_ORIGIN_TYPE = _enum_type(MessageOrigin, name="message_origin")
MESSAGE_STATUS_TYPE = _enum_type(MessageStatus, name="message_status")
MESSAGE_DELIVERY_STATE_TYPE = _enum_type(MessageDeliveryState, name="message_delivery_state")

# The delivery states that leave a send finished. Anything else is a send whose
# outcome is still open - which for `REQUESTED` means open for ever, because
# Meta offers no way to ask what happened to a request it never answered.
RESOLVED_DELIVERY_STATES: Final[frozenset[MessageDeliveryState]] = frozenset(
    {MessageDeliveryState.SENT, MessageDeliveryState.UNDELIVERED}
)


class Contact(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A customer, identified by the WhatsApp id Meta reports.

    Unique per workspace rather than globally: the same person may be a customer
    of two businesses on the platform, and those must be separate records.
    """

    __tablename__ = "contacts"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        UniqueConstraint("tenant_id", "wa_id", name="uq_contacts_tenant_id_wa_id"),
        # Redundant as a uniqueness claim - `id` is the primary key, so
        # `(tenant_id, id)` cannot repeat - and required as a *target*: a
        # composite foreign key can only reference a uniquely constrained set of
        # columns. `conversations` points here through one (ADR-100).
        UniqueConstraint("tenant_id", "id", name="uq_contacts_tenant_id_id"),
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
        # Analytics: conversations opened in a window. The tenant index
        # alone finds the workspace and then discards most of what it read
        # by filter, which costs more the longer a workspace has existed.
        Index("ix_conversations_tenant_id_created_at", "tenant_id", "created_at"),
        # The contact and the account this conversation is with must belong to
        # the same workspace it does, and the database is what says so
        # (ADR-100). A plain `contact_id -> contacts.id` accepts a conversation
        # in tenant A against tenant B's contact; no API path builds one -
        # ingestion derives every id from one resolved account inside one
        # tenant-scoped service - but "no path does this" is a property of
        # today's code, and this is a property of the schema.
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_conversations_tenant_contact",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            name="fk_conversations_tenant_account",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id_id"),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
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
        # Analytics: traffic in a window. Distinct from the index above,
        # which serves one conversation's transcript rather than a
        # workspace-wide count, and this is the largest table in the schema
        # after usage events.
        Index("ix_messages_tenant_id_created_at", "tenant_id", "created_at"),
        # Sends whose outcome is still open. Partial, because on a healthy
        # deployment this is the empty set and a full index over the largest
        # table in the schema would be paid for continuously to answer a
        # question nobody usually has (ADR-093).
        Index(
            "ix_messages_unresolved_delivery",
            "tenant_id",
            "created_at",
            postgresql_where=text("delivery_state IN ('claimed', 'requested')"),
        ),
        # What makes a repeated submission a repeat rather than a second
        # message. The read in `MessagingService` is the fast path; this is the
        # guarantee, and it is what decides the race when two submissions of
        # one key arrive together.
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_messages_tenant_id_idempotency_key",
        ),
        # A message belongs to a conversation in its own workspace (ADR-100).
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_messages_tenant_conversation",
            ondelete="CASCADE",
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
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
    # Nullable because an inbound message has no delivery protocol to be in the
    # middle of, and because every row written before ADR-093 predates the
    # question. Read through the two properties below rather than compared to
    # `None` at call sites.
    delivery_state: Mapped[MessageDeliveryState | None] = mapped_column(
        MESSAGE_DELIVERY_STATE_TYPE,
        nullable=True,
        default=None,
    )
    # A caller's own key for the request that produced this message, so a
    # retried or double-clicked submission is recognised instead of putting a
    # second copy on the customer's phone (MSG-15). The same shape
    # `payments.idempotency_key` uses, for the same reason and with the same
    # scoping: keys are generated by clients, so two workspaces choosing the
    # same UUID must not collide.
    #
    # Nullable, and NULLs are distinct under the unique constraint below, so
    # any number of sends without a key coexist. Most do not carry one.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(MAX_IDEMPOTENCY_KEY_LENGTH),
        nullable=True,
    )
    # What produced this message. Set explicitly at every creation site rather
    # than defaulted, because a default is what an unlabelled campaign send
    # would silently inherit - and inheriting the wrong attribution is the
    # defect this column exists to remove (MSG-16).
    origin: Mapped[MessageOrigin] = mapped_column(MESSAGE_ORIGIN_TYPE, nullable=False)

    @property
    def delivery_uncertain(self) -> bool:
        """Whether Meta may hold this message without anyone knowing.

        The one state that must never be answered with another send. Meta
        publishes no way to ask what became of a request it did not answer, so
        `REQUESTED` is terminal in practice: a person decides, from the
        conversation, whether to write again (ADR-093).
        """
        return self.delivery_state is MessageDeliveryState.REQUESTED

    @property
    def delivery_resolved(self) -> bool:
        """Whether this send has a known outcome, either way."""
        return self.delivery_state in RESOLVED_DELIVERY_STATES
