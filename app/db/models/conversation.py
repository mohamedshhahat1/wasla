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
from typing import Any, Final

from sqlalchemy import (
    DDL,
    BigInteger,
    Connection,
    DateTime,
    FetchedValue,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.campaign import (
    OPT_OUT_SOURCE_TYPE,
    OPT_OUT_VIA_TYPE,
    OptOutSource,
    OptOutVia,
)
from app.db.models.channel import (
    CHANNEL_TYPE,
    Channel,
    install_contact_phone_identity,
    install_conversation_participant,
)
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
    # Sent by the business from outside Wasla - the provider's own app, or the
    # WhatsApp Business app under Coexistence - and reported back as an echo
    # (OMNI-037, ADR-129). Nobody in Wasla sent it, and it is still the
    # business talking: the AI stops and a person has the conversation.
    EXTERNAL = "external"


class ReplyActionSource(StrEnum):
    """Which provider control a customer used to answer, in neutral terms (OMNI-030)."""

    #: A quick-reply button under a WhatsApp template (`type: button`).
    BUTTON = "button"
    #: A WhatsApp interactive reply button (`interactive.button_reply`).
    BUTTON_REPLY = "button_reply"
    #: A row of a WhatsApp interactive list (`interactive.list_reply`).
    LIST_REPLY = "list_reply"
    #: A Messenger or Instagram quick reply (`message.quick_reply.payload`).
    QUICK_REPLY = "quick_reply"
    #: A Messenger or Instagram postback (`postback {title, payload}`), which
    #: ADR-125 represents as a message carrying this action (OMNI-053).
    POSTBACK = "postback"


#: The longest provider id or payload a reply action keeps. Meta documents 256
#: characters for an interactive button id and 1,000 for a Messenger payload;
#: one that is longer is not a value Meta issued, and is dropped, not cut.
MAX_ACTION_PAYLOAD_LENGTH: Final = 1_000
#: The longest display title kept. Meta's titles are 20-24 characters.
MAX_ACTION_TITLE_LENGTH: Final = 255


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
REPLY_ACTION_SOURCE_TYPE = _enum_type(ReplyActionSource, name="reply_action_source")

# The delivery states that leave a send finished. Anything else is a send whose
# outcome is still open - which for `REQUESTED` means open for ever, because
# Meta offers no way to ask what happened to a request it never answered.
RESOLVED_DELIVERY_STATES: Final[frozenset[MessageDeliveryState]] = frozenset(
    {MessageDeliveryState.SENT, MessageDeliveryState.UNDELIVERED}
)


class Contact(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A customer: the person the CRM knows, in one workspace.

    Who the customer *is to a provider* is not here any more. That is
    `ContactIdentity` - a WhatsApp phone number, a WhatsApp business-scoped user
    id, and on later channels whatever those providers call a person - and a
    contact may hold several (OMNI-001, ADR-118).

    `wa_id` stays for the compatibility window as the contact's WhatsApp phone
    number, kept equal to its phone identity by a trigger. It is nullable
    because a WhatsApp user with a username can write without Meta telling the
    business their number at all (OMNI-002): that customer is a contact with a
    business-scoped identity and no phone.

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

    # Deprecated: read a contact's identities instead (docs/API.md).
    wa_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
    # How the opt-out reached Wasla - a stop word, a tapped button, the
    # provider's own preference webhook, a provider refusal, a replay of
    # retained evidence, or a colleague (OMNI-030, OMNI-046). `source` says
    # who decided; this says by which evidence. Null where it predates it.
    opt_out_via: Mapped[OptOutVia | None] = mapped_column(OPT_OUT_VIA_TYPE, nullable=True)
    # The last time this person was re-admitted to campaigns - a colleague
    # clearing the opt-out, or the customer resuming marketing messages through
    # the provider. A replay of older opt-out evidence never overrides a newer
    # resume (OMNI-030).
    marketing_resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    """One customer talking to one connection, through one identity.

    Scoped by connection as well as contact, because a business with a sales
    number and a support number is holding two genuinely separate
    conversations with the same person - and a person who writes on WhatsApp
    and on Instagram is holding two more. A unified inbox lists them together;
    they are never one thread (ADR-119).

    `account_id` is the connection. The name predates `channel_connections`
    and is kept for the compatibility window; the API reports the same value
    as `connection_id`.

    **Who a reply goes to is pinned here, not read from the contact**
    (OMNI-004). `participant_identity_id` is the identity this conversation
    addresses - the one that wrote - so a contact holding a phone number and a
    business-scoped id, or later an Instagram id too, is never addressed by
    whichever of them a send happened to pick. Three keys make the pin
    something the database checks: the connection and the conversation share a
    channel, and the participant belongs to this conversation's contact, in its
    workspace, on that same channel.
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
        # The inbox narrowed to one connection, in the inbox's own order
        # (OMNI-014). Without it the filter walks the workspace's whole range
        # and discards by predicate - slowest for the largest, oldest
        # workspaces, the shape migration 0019 already fixed once.
        Index(
            "ix_conversations_tenant_id_account_id_last_message_at",
            "tenant_id",
            "account_id",
            text("last_message_at DESC NULLS LAST"),
            text("id DESC"),
        ),
        # The inbox narrowed to one channel, in the inbox's own order
        # (OMNI-048); the connection index above does not serve it.
        Index(
            "ix_conversations_tenant_id_channel_last_message_at",
            "tenant_id",
            "channel",
            text("last_message_at DESC NULLS LAST"),
            text("id DESC"),
        ),
        Index("ix_conversations_participant_identity_id", "participant_identity_id"),
        # The contact and the connection this conversation is with must belong
        # to the same workspace it does, and the database is what says so
        # (ADR-100). A plain `contact_id -> contacts.id` accepts a conversation
        # in tenant A against tenant B's contact; no API path builds one -
        # ingestion derives every id from one resolved connection inside one
        # tenant-scoped service - but "no path does this" is a property of
        # today's code, and this is a property of the schema.
        ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_conversations_tenant_contact",
            ondelete="CASCADE",
        ),
        # The connection, which for WhatsApp shares its number's id - so this
        # replaced the key into `whatsapp_accounts` without rewriting a row
        # (0084, ADR-117). The channel is part of the key.
        ForeignKeyConstraint(
            ["tenant_id", "account_id", "channel"],
            [
                "channel_connections.tenant_id",
                "channel_connections.id",
                "channel_connections.channel",
            ],
            name="fk_conversations_tenant_connection",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "contact_id", "participant_identity_id", "channel"],
            [
                "contact_identities.tenant_id",
                "contact_identities.contact_id",
                "contact_identities.id",
                "contact_identities.channel",
            ],
            name="fk_conversations_tenant_participant",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id_id"),
        # Also redundant as uniqueness, and the target of the lead key that
        # makes a lead's conversation agree with the lead's customer (CRM-14).
        UniqueConstraint(
            "tenant_id",
            "id",
            "contact_id",
            name="uq_conversations_tenant_id_id_contact_id",
        ),
        # The target of the key that makes a message's connection its
        # conversation's connection.
        UniqueConstraint(
            "tenant_id",
            "id",
            "account_id",
            name="uq_conversations_tenant_id_id_account_id",
        ),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The channel of `account_id`'s connection, held here so the key above can
    # make the two agree and a policy can be chosen without a join. The
    # default is for the compatibility window's older writers and is only ever
    # right for WhatsApp: a writer that forgets it on another channel is
    # refused by the connection key, never filed on the wrong channel.
    channel: Mapped[Channel] = mapped_column(
        CHANNEL_TYPE,
        nullable=False,
        server_default=Channel.WHATSAPP.value,
    )
    # Pinned at creation to the identity that wrote. A writer that names none
    # gets the contact's only identity on this channel from a trigger, and a
    # contact with none or with several gets no guess - the participant key
    # refuses the row instead (`FetchedValue`: the value may come back from the
    # database).
    participant_identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        server_default=FetchedValue(),
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
    # When an automated reply last told this customer they were talking to an
    # automated assistant - written once that reply was delivered (OMNI-041).
    automation_disclosed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When a colleague last handed the conversation back to the AI. A later
    # AI reply discloses again: the customer may have been talking to a person.
    ai_resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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

    # The last position handed to a message in this conversation (AI-01).
    # Written by the `messages` insert trigger and by nothing else - application
    # code never assigns it, so the ORM never includes it in an UPDATE and a
    # stale value loaded into a session cannot overwrite the real one.
    last_message_sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    @property
    def is_ai_handled(self) -> bool:
        return self.mode is ConversationMode.AI

    @property
    def needs_attention(self) -> bool:
        """Whether this conversation should be surfaced ahead of the queue."""
        return self.priority is not ConversationPriority.NORMAL


# Every message writes its conversation twice: the sequence trigger bumps
# `last_message_sequence`, and the projection or the send moves the indexed
# `last_message_at`. The bump touches no indexed column, so it can be a HOT
# update - if the page has room for the new version. Ten percent of each page is
# left for exactly that (DB-020); measured, the bump went from 92% to 98.6% HOT
# and the table's indexes stopped growing with it. Set by migration 0080.
CONVERSATIONS_FILLFACTOR: Final = 90

event.listen(
    Conversation.__table__,
    "after_create",
    DDL(f"ALTER TABLE conversations SET (fillfactor = {CONVERSATIONS_FILLFACTOR})"),  # type: ignore[no-untyped-call]
)


class Message(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One message in either direction.

    `wa_message_id` is the provider's id for this message - the API also calls
    it `provider_message_id`. Nullable because an outbound row is written before
    the provider is called: a send that fails must still leave evidence that it
    was attempted.

    **A provider id is unique per connection**, and that is the identity status
    projection and inbound replay are judged by (OMNI-005). `connection_id` is
    the conversation's connection, derived by the database at insert and held
    to it by a key, so it is never a second fact somebody could set wrongly.
    The older workspace-wide uniqueness stays until the compatibility cleanup
    (O8): it is stricter, so it loses nothing for WhatsApp, and it must go
    before a channel whose ids are only unique per connection ships.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Legacy, kept until the compatibility cleanup (ADR-120).
        UniqueConstraint(
            "tenant_id",
            "wa_message_id",
            name="uq_messages_tenant_id_wa_message_id",
        ),
        # The provider message identity the neutral path is judged by.
        UniqueConstraint(
            "tenant_id",
            "connection_id",
            "wa_message_id",
            name="uq_messages_tenant_id_connection_id_wa_message_id",
        ),
        # The target of the keys that make a file's message, and an agent
        # turn's trigger, belong to the same conversation and workspace
        # (OMNI-022, OMNI-005).
        UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "id",
            name="uq_messages_tenant_id_conversation_id_id",
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
        # ...and its connection is that conversation's connection.
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "connection_id"],
            ["conversations.tenant_id", "conversations.id", "conversations.account_id"],
            name="fk_messages_tenant_conversation_connection",
            ondelete="CASCADE",
        ),
        # One position per conversation. Also the index a transcript is read
        # through, newest first.
        UniqueConstraint(
            "conversation_id",
            "sequence",
            name="uq_messages_conversation_id_sequence",
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The conversation's connection, written by the sequence trigger below for
    # every writer that does not name it - which is every writer: a message's
    # connection is not a choice, it is where its conversation lives.
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        server_default=FetchedValue(),
    )
    # Where this message sits in its conversation, assigned by the database at
    # insert (AI-01). The conversation's order is this column and never
    # `created_at`: `created_at` is PostgreSQL's `now()`, which is the start of
    # the *transaction*, so every message one webhook delivery wrote shares one
    # instant and a sort on it hands the model a scrambled transcript.
    #
    # Assigned by a trigger rather than by the repository so that every
    # producer takes part - inbound projection, an agent's reply, a colleague's,
    # a campaign, a follow-up, and whatever writes a message next - without any
    # of them having to remember to. `FetchedValue` tells the ORM the database
    # supplies it, so it is read back on insert rather than sent as NULL.
    sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=FetchedValue(),
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
    # When the provider says it sent this message, on the provider's clock - a
    # send receipt, an echo or a `sent` status. `sent_at` is Wasla's clock,
    # taken after the provider answered, and a read watermark ("everything sent
    # at or before this instant was read") is the provider's (OMNI-042).
    provider_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    # What the customer tapped, when this message is a tap (OMNI-030, ADR-124).
    # The title is also the body, so the transcript reads as words; the payload
    # is what routing and the opt-out match read, because display text is
    # translated and edited and a payload is not. Null on everything else.
    action_source: Mapped[ReplyActionSource | None] = mapped_column(
        REPLY_ACTION_SOURCE_TYPE, nullable=True
    )
    action_payload: Mapped[str | None] = mapped_column(
        String(MAX_ACTION_PAYLOAD_LENGTH), nullable=True
    )
    action_title: Mapped[str | None] = mapped_column(String(MAX_ACTION_TITLE_LENGTH), nullable=True)

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


# The ordering primitive (AI-01), kept in the database because that is the only
# place every writer passes through.
#
# One conditional UPDATE ... RETURNING on the conversation row. Two transactions
# writing to one conversation serialise on that row's lock - the second waits for
# the first to commit and then reads the incremented counter - so positions are
# unique without a `max()+1` read that two writers could both make. The lock is
# one the write already took in practice: inbound projection touches
# `last_inbound_at` and a send touches `last_message_at` on the same row.
#
# A row naming no conversation in its own workspace gets position 0, which is
# never persisted: the composite foreign key refuses the row a moment later under
# its own name, rather than a NOT NULL error describing a symptom.
#
# The same statement hands the row its connection (OMNI-005): the conversation's
# own, read from the row it has just locked. A writer that names a connection
# keeps it - and the key `fk_messages_tenant_conversation_connection` refuses it
# if it is not the conversation's. A row naming no conversation here gets its
# conversation id in place of a connection, which is never persisted for the
# same reason position 0 is not.
#
# Migration 0058 carries its own frozen copy of this text; a change here needs a
# migration of its own. 0080 pinned its search_path (DB-024); 0082 added the
# connection.
MESSAGE_SEQUENCE_FUNCTION: Final = """
CREATE OR REPLACE FUNCTION wasla_assign_message_sequence() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_catalog
AS $$
DECLARE
    conversation_connection uuid;
BEGIN
    UPDATE conversations
       SET last_message_sequence = last_message_sequence + 1
     WHERE id = NEW.conversation_id
       AND tenant_id = NEW.tenant_id
    RETURNING last_message_sequence, account_id INTO NEW.sequence, conversation_connection;
    NEW.sequence = COALESCE(NEW.sequence, 0);
    NEW.connection_id = COALESCE(NEW.connection_id, conversation_connection, NEW.conversation_id);
    RETURN NEW;
END;
$$
"""

MESSAGE_SEQUENCE_TRIGGER: Final = """
CREATE TRIGGER trg_messages_assign_sequence
BEFORE INSERT ON messages
FOR EACH ROW EXECUTE FUNCTION wasla_assign_message_sequence()
"""


def _install_message_sequence(_target: object, connection: Connection, **_kwargs: Any) -> None:
    """Create the ordering trigger alongside `messages` in a model-built schema.

    A `create_all` schema needs it as much as a migrated one: without it every
    insert fails NOT NULL, and the fast test build would stop describing the
    database a deployment runs. Sent as driver SQL, so nothing in the function
    body is parsed for bind parameters on the way.
    """
    connection.exec_driver_sql(MESSAGE_SEQUENCE_FUNCTION)
    connection.exec_driver_sql(MESSAGE_SEQUENCE_TRIGGER)


event.listen(Message.__table__, "after_create", _install_message_sequence)
# The compatibility triggers of ADR-117/118, for a model-built schema. A
# migrated one gets them from 0082.
event.listen(Contact.__table__, "after_create", install_contact_phone_identity)
event.listen(Conversation.__table__, "after_create", install_conversation_participant)
