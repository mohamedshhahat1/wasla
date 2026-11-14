"""Conversation and message API contracts.

Read models are mapped field by field rather than inferred from the ORM object,
so adding a column to a table never silently widens the API.

Conversations carry identifiers rather than embedded contact objects. The models
declare no ORM relationships on purpose: a lazy load inside an async request is
blocking I/O that only shows up under load.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import inspect
from sqlalchemy.orm.base import NO_VALUE

from app.channels.policy import (
    REQUEST_TEXT_CEILING,
    ChannelState,
    OutOfWindow,
    ReplyPolicy,
    SendMechanism,
    TextUnit,
)
from app.core.media_types import CANONICAL_TYPES
from app.db.models.channel import Channel, ContactIdentity, IdentityKind
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
    ReplyActionSource,
)
from app.db.models.media import (
    UNRESOLVED_MEDIA_STATUSES,
    MediaStatus,
    MediaStorageState,
    MessageMedia,
)
from app.db.models.sentiment import ConversationPriority, SentimentLabel
from app.schemas.bounds import TEMPLATE_COMPONENTS, check_json
from app.schemas.follow_up import FollowUpStateRead
from app.schemas.text import StorableText

# The largest text any supported channel accepts, before the conversation's
# channel is known (OMNI-044). The schema rejects early and politely;
# `MessagingService.send_text` applies the channel's own limit, in its own unit,
# and is the guarantee (MSG-25). A test holds every adapter within this.
MAX_TEXT_LENGTH = REQUEST_TEXT_CEILING


class SendTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: StorableText = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    preview_url: bool = False


class SendTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StorableText = Field(min_length=1, max_length=512)
    language: StorableText = Field(min_length=2, max_length=16)
    # Forwarded to Meta, whose shape this deliberately does not model. Bounded
    # so an oversized structure is refused here rather than after a database
    # write and a Graph API round trip - see `app.schemas.bounds`.
    components: list[dict[str, Any]] | None = None
    # A file of the workspace's template header library (HMT-02): required for
    # a template whose header is an image, a video or a document. A raw media
    # header in `components` is refused.
    header_media_id: uuid.UUID | None = None

    @field_validator("components")
    @classmethod
    def _bounded_components(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if value is not None:
            check_json(value, TEMPLATE_COMPONENTS, field="components")
        return value


class ModeUpdateRequest(BaseModel):
    """Take a conversation over, or give it back to the AI.

    Taking over an AI conversation makes the caller its owner and records
    `handoff_reason`. On a conversation already human it changes nothing - use
    the assignment endpoint to move it between colleagues (PD-CRM-3, PD-CRM-5).
    """

    model_config = ConfigDict(extra="forbid")

    mode: ConversationMode
    handoff_reason: StorableText | None = Field(default=None, max_length=200)


class PriorityUpdateRequest(BaseModel):
    """Set the priority by hand.

    The only way it comes down. Assessment raises it and never lowers it, so
    returning a conversation to the ordinary queue is a decision somebody makes
    after looking at it.
    """

    model_config = ConfigDict(extra="forbid")

    priority: ConversationPriority


class AssignmentRequest(BaseModel):
    """Give a conversation to a colleague, or clear its owner.

    `expected_assigned_to_id` is required, and null is a value: it is who the
    caller believes owns the conversation now - `assigned_to_id` from the
    `ConversationRead` they are looking at. The write happens only if that is
    still the owner; otherwise the answer is 409 `stale_assignment` and nothing
    changes (PD-CRM-4). Last-writer-wins let two colleagues both be told they
    owned a customer, and let a stale screen undo a manager's reassignment.
    """

    model_config = ConfigDict(extra="forbid")

    # Null clears the assignment. A value must be an active member here.
    assigned_to_id: uuid.UUID | None = None
    expected_assigned_to_id: uuid.UUID | None


class CursorPage[ItemT](BaseModel):
    """One page, and the cursor that asks for the next.

    `next_cursor` is null when the collection is exhausted. Clients should stop
    on that rather than counting items: a full page is not proof of more, and an
    empty one is not proof of none once filtering is involved.
    """

    items: list[ItemT]
    next_cursor: str | None = None


class ReplyActionRead(BaseModel):
    """What a customer tapped (OMNI-030): the provider's id or payload, and its words."""

    id_or_payload: str | None
    title: str | None
    source: ReplyActionSource


class MediaKind(StrEnum):
    """What sort of file an attachment is, for drawing it (FEB-03)."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


class MediaAvailability(StrEnum):
    """Whether `GET …/media/{id}` can serve the file now, in the words a screen needs (FEB-03).

    `unavailable` is one word for every reason the bytes cannot be served and
    will not be - removed by retention, being removed, refused as not what was
    written, or never stored (too large, a type nothing reads). The screen says
    "not available"; why storage refused it is not the inbox's to show.
    """

    AVAILABLE = "available"
    PENDING = "pending"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class TranscriptStatus(StrEnum):
    """Where reading the file got: a voice note's transcript, an image's description (FEB-03)."""

    NONE = "none"
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


_UNSERVABLE_STORAGE: Final = frozenset(
    {MediaStorageState.PURGING, MediaStorageState.PURGED, MediaStorageState.MISMATCHED}
)
_IN_FLIGHT: Final = frozenset({MediaStatus.PENDING, MediaStatus.DOWNLOADING})


def media_availability(media: MessageMedia) -> MediaAvailability:
    """The public state of one file: what the download route would do with it now."""
    if media.storage_state is MediaStorageState.STORED:
        return MediaAvailability.AVAILABLE
    if media.storage_state in _UNSERVABLE_STORAGE:
        return MediaAvailability.UNAVAILABLE
    if media.storage_state is MediaStorageState.PENDING or media.status in _IN_FLIGHT:
        return MediaAvailability.PENDING
    if media.status is MediaStatus.FAILED:
        return MediaAvailability.FAILED
    return MediaAvailability.UNAVAILABLE


def transcript_status(media: MessageMedia) -> TranscriptStatus:
    if media.transcript is not None:
        return TranscriptStatus.READY
    if media.status in UNRESOLVED_MEDIA_STATUSES:
        return TranscriptStatus.PENDING
    if media.status is MediaStatus.FAILED:
        return TranscriptStatus.FAILED
    return TranscriptStatus.NONE


def media_kind(media: MessageMedia, message_kind: MessageKind) -> MediaKind:
    for kind in (MediaKind.IMAGE, MediaKind.AUDIO, MediaKind.VIDEO, MediaKind.DOCUMENT):
        if message_kind.value == kind.value:
            return kind
    family = (media.mime_type or "").split("/", 1)[0]
    return {
        "image": MediaKind.IMAGE,
        "audio": MediaKind.AUDIO,
        "video": MediaKind.VIDEO,
    }.get(family, MediaKind.DOCUMENT)


class MessageMediaRead(BaseModel):
    """One file of a message, as a screen draws it (FEB-03).

    `id` is what `GET /conversations/{id}/media/{media_id}` takes. Never here:
    the storage key, the provider's media id or locator, a URL, a hash, the
    last error or retention dates - the file is reached through that route
    and nothing else.
    """

    id: uuid.UUID
    kind: MediaKind
    # As the download route serves it: a type this system canonically supports,
    # else `application/octet-stream`; null when none is known yet.
    mime_type: str | None
    # The stored name, as the sender's phone gave it - text, never markup.
    filename: str | None
    byte_size: int | None
    is_voice: bool
    transcript_status: TranscriptStatus
    # What the file says (a transcript, a description), once `ready`.
    transcript: str | None
    status: MediaAvailability
    position: int
    # The message's caption, on its first file.
    caption: str | None

    @classmethod
    def from_model(cls, media: MessageMedia, *, message: Message) -> Self:
        mime = media.mime_type
        state = transcript_status(media)
        return cls(
            id=media.id,
            kind=media_kind(media, message.kind),
            mime_type=(
                None
                if mime is None
                else mime if mime in CANONICAL_TYPES else "application/octet-stream"
            ),
            filename=media.filename,
            byte_size=media.byte_size or None,
            is_voice=media.is_voice,
            transcript_status=state,
            transcript=media.transcript if state is TranscriptStatus.READY else None,
            status=media_availability(media),
            position=media.position,
            caption=message.body if media.position == 0 else None,
        )


class MessageRead(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    # The provider's id for this message, whatever the channel (OMNI-015).
    provider_message_id: str | None
    wa_message_id: str | None = Field(
        deprecated=(
            "Use provider_message_id, which carries the same value on every "
            "channel. Kept until clients have moved (docs/API.md)."
        ),
    )
    direction: MessageDirection
    kind: MessageKind
    status: MessageStatus
    # What produced this line. Exposed because it is the question a reader of
    # the transcript has, and because inferring it from `sent_by_id` gets
    # campaigns and follow-ups wrong (MSG-16).
    origin: MessageOrigin
    body: str | None
    # Set when the customer tapped a button or a list row rather than typing:
    # `body` carries its words, this carries the payload a client routes on.
    # Null on everything else. Additive (OMNI-030).
    action: ReplyActionRead | None = None
    # Set on template messages only, so a client can render which template went
    # out in place of the text it has no copy of.
    template_name: str | None
    template_language: str | None
    sent_by_id: uuid.UUID | None
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None
    failure_reason: str | None
    created_at: datetime
    # The message's files, in the provider's order; empty when it carries none
    # (FEB-03). The routes read them a page at a time, in one query.
    media: list[MessageMediaRead] = Field(default_factory=list)

    @classmethod
    def from_model(cls, message: Message, *, media: Sequence[MessageMedia] = ()) -> Self:
        return cls(
            media=[MessageMediaRead.from_model(item, message=message) for item in media],
            id=message.id,
            conversation_id=message.conversation_id,
            provider_message_id=message.wa_message_id,
            wa_message_id=message.wa_message_id,
            direction=message.direction,
            kind=message.kind,
            status=message.status,
            origin=message.origin,
            body=message.body,
            action=(
                ReplyActionRead(
                    id_or_payload=message.action_payload,
                    title=message.action_title,
                    source=message.action_source,
                )
                if message.action_source is not None
                else None
            ),
            template_name=message.template_name,
            template_language=message.template_language,
            sent_by_id=message.sent_by_id,
            sent_at=message.sent_at,
            delivered_at=message.delivered_at,
            read_at=message.read_at,
            failure_reason=message.failure_reason,
            created_at=message.created_at,
        )


class TemplateSentRead(MessageRead):
    """A person's template, and what its answer needs to say about it (FLIM-02).

    `notices` holds `messaging_limit_unknown` when it was sent while nobody had
    read the number's WhatsApp messaging limit; empty otherwise.
    """

    notices: list[str] = Field(default_factory=list)


class ParticipantRead(BaseModel):
    """Who a conversation is with (OMNI-015, FEB-04).

    `kind` says what sort of address the channel uses for them. `handle` is how
    a person recognises them on that channel: the WhatsApp number in E.164,
    the same number every member already reads in the contact picker
    (`GET /contacts`). An address that is not a phone number - a business-scoped
    user id, a page-scoped id - is never a handle and is not returned: it means
    nothing to a person and is not a number to call. `display_name` is the name
    the channel gave, as the customer typed it: text, never markup.
    """

    id: uuid.UUID
    channel: Channel
    kind: IdentityKind
    contact_id: uuid.UUID
    display_name: str | None
    handle: str | None
    # The contact's open lead, when there is one: what the customer panel opens.
    lead_id: uuid.UUID | None

    @classmethod
    def from_model(
        cls,
        identity: ContactIdentity,
        *,
        display_name: str | None,
        lead_id: uuid.UUID | None,
    ) -> Self:
        return cls(
            id=identity.id,
            channel=identity.channel,
            kind=identity.kind,
            contact_id=identity.contact_id,
            display_name=display_name,
            handle=phone_handle(identity),
            lead_id=lead_id,
        )


def phone_handle(identity: ContactIdentity) -> str | None:
    """The identity as a number a person can read, or None when it is not a phone number."""
    if identity.kind is not IdentityKind.PHONE or not identity.value.isdigit():
        return None
    return f"+{identity.value}"


class ReplyPolicyRead(BaseModel):
    """What a person may send on this conversation now (ADR-121).

    The channel's rule, stated rather than inferred: whether free text is
    allowed and until when, what may be sent after that, and the text limit in
    the channel's own unit - characters on WhatsApp, UTF-8 bytes on a channel
    that counts bytes.
    """

    free_text_allowed: bool
    window_expires_at: datetime | None
    out_of_window: OutOfWindow
    templates: bool
    text_limit: int
    text_limit_unit: TextUnit
    # `operational`, or `paused` / `unavailable` when Wasla cannot act on the
    # channel: then nothing is sendable, whatever the window (OMNI-031).
    # Additive; a client that ignores it still reads `free_text_allowed` and
    # `templates` as false.
    state: ChannelState = ChannelState.OPERATIONAL
    # Per origin (OMNI-033), additive. How a person's free text would go now -
    # `standard_window`, or `human_agent_tag` after the window on a channel
    # that has one - and whether an agent's may go at all, which a human-agent
    # tag never allows.
    free_text_mechanism: SendMechanism | None = None
    agent_free_text_allowed: bool = False

    @classmethod
    def from_policy(cls, policy: ReplyPolicy) -> Self:
        return cls(
            free_text_allowed=policy.free_text_allowed,
            window_expires_at=policy.window_expires_at,
            out_of_window=policy.out_of_window,
            templates=policy.templates,
            text_limit=policy.text_limit,
            text_limit_unit=policy.text_limit_unit,
            state=policy.state,
            free_text_mechanism=policy.free_text_mechanism,
            agent_free_text_allowed=policy.agent_free_text_allowed,
        )


class ConversationRead(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    # The connection this conversation is on. Kept under its first name; the
    # name was always neutral, and `connection_id` carries the same value.
    account_id: uuid.UUID
    connection_id: uuid.UUID
    channel: Channel
    participant: ParticipantRead
    status: ConversationStatus
    mode: ConversationMode
    assigned_to_id: uuid.UUID | None
    handoff_reason: str | None
    last_message_at: datetime | None
    last_inbound_at: datetime | None
    # The latest reading of how the customer sounds, and what it implied.
    # `priority` is what an inbox sorts on; the rest explains why it is there.
    sentiment: SentimentLabel | None
    sentiment_score: float | None
    priority: ConversationPriority
    intent: str | None
    intent_confidence: float | None
    # Whether the channel's standard free-form window is open, so a client can
    # disable its composer instead of discovering the rule by failing a send.
    # Its meaning never widens: `reply_policy` is where a channel's other rules
    # are stated (ADR-121).
    service_window_open: bool
    reply_policy: ReplyPolicyRead
    created_at: datetime
    # When anything the list shows last changed (RT-04): what `updated_since`
    # reads. Null only on a row read before its first flush.
    updated_at: datetime | None = None
    # The conversation's follow-ups (FUS-09): on `GET /conversations/{id}` only;
    # null in a list.
    follow_up: FollowUpStateRead | None = None

    @classmethod
    def from_model(
        cls,
        conversation: Conversation,
        *,
        service_window_open: bool,
        reply_policy: ReplyPolicy,
        participant: ParticipantRead,
    ) -> Self:
        return cls(
            id=conversation.id,
            contact_id=conversation.contact_id,
            account_id=conversation.account_id,
            connection_id=conversation.account_id,
            channel=conversation.channel,
            participant=participant,
            status=conversation.status,
            mode=conversation.mode,
            assigned_to_id=conversation.assigned_to_id,
            handoff_reason=conversation.handoff_reason,
            last_message_at=conversation.last_message_at,
            last_inbound_at=conversation.last_inbound_at,
            sentiment=conversation.sentiment,
            sentiment_score=conversation.sentiment_score,
            priority=conversation.priority,
            intent=conversation.intent,
            intent_confidence=conversation.intent_confidence,
            service_window_open=service_window_open,
            reply_policy=ReplyPolicyRead.from_policy(reply_policy),
            created_at=conversation.created_at,
            updated_at=_loaded_updated_at(conversation),
        )


class ConversationListPage(BaseModel):
    """A page of the inbox, and how long to wait before polling again (RT-04).

    `poll_after_seconds` is 30 while this person has no event stream open, 60
    while they do - then polling is only the safety net behind the stream.
    """

    items: list[ConversationRead]
    next_cursor: str | None
    poll_after_seconds: int


def _loaded_updated_at(conversation: Conversation) -> datetime | None:
    """`updated_at` if it is loaded: a write just flushed expires it (its `onupdate` is
    the database's clock), and reading it then would be I/O inside serialisation."""
    value = inspect(conversation).attrs.updated_at.loaded_value
    return None if value is NO_VALUE else value
