"""What an adapter hands the shared inbound pipeline: provider-neutral events.

Every provider's webhook is parsed at its own edge, by its own adapter, into
these - and nothing downstream of the edge sees a provider payload shape again
(OMNI-006). Shared ingestion, the projection, identity resolution, recovery and
every hand-off read `InboundEvent`; none of them imports a WhatsApp DTO.

Three rules the types carry:

- **The sender is a set of identifiers, asserted together.** A WhatsApp message
  from a known number carries a phone and a business-scoped id; one from a user
  with a username may carry only the id (OMNI-002). The adapter lists what the
  provider said, most-preferred address first, and nothing else - never a name
  or a guess.
- **Direction is explicit.** An `ECHO` is a message the business sent, reported
  back; it is never a customer's message, whatever its shape (OMNI-005).
- **Nothing an adapter refuses disappears.** A delivery is its events plus a
  count of what was refused, by a bounded reason, so a payload that parsed to
  nothing is visible as exactly that (OMNI-010, OMNI-021).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from app.db.models.channel import Channel, IdentityKind
from app.db.models.conversation import (
    MAX_ACTION_PAYLOAD_LENGTH,
    MAX_ACTION_TITLE_LENGTH,
    MessageKind,
    MessageStatus,
    ReplyActionSource,
)
from app.db.models.media import MediaLocatorKind


class InboundKind(StrEnum):
    """What an inbound event is, before anything is concluded from it."""

    #: A customer's message.
    MESSAGE = "message"
    #: A report about a message the business sent: delivered, read, failed.
    STATUS = "status"
    #: A message the business sent, reported back by the provider - Messenger
    #: and Instagram `is_echo`, WhatsApp Coexistence app sends. Evidence, never
    #: a customer's turn.
    ECHO = "echo"
    #: The person changed their marketing preference through the provider
    #: (WhatsApp `user_preferences`, OMNI-046). Never a turn.
    PREFERENCE = "preference"


@dataclass(frozen=True, slots=True)
class MarketingPreference:
    """A person's marketing preference, as the provider recorded it (OMNI-046)."""

    #: `stop` or `resume`.
    value: str
    #: The provider's category, `marketing_messages` for WhatsApp.
    category: str


class RefusalReason(StrEnum):
    """Why an adapter refused part of a delivery. Bounded: it is a metric label.

    Never a provider's error text and never anything from the payload - these
    are Wasla's own words for what was wrong with the shape.
    """

    #: The payload belongs to another provider product - a Page event posted
    #: to the WhatsApp endpoint. A misrouted subscription, most often.
    FOREIGN_OBJECT = "foreign_object"
    #: A change this adapter does not process (template updates, Coexistence
    #: history and app-state sync). Counted so it is not silent.
    UNSUPPORTED_FIELD = "unsupported_field"
    #: No connection identifier, so no workspace could own it.
    MISSING_CONNECTION = "missing_connection"
    #: A message or status without the provider's id for it.
    MISSING_EVENT_ID = "missing_event_id"
    #: A message with no sender identifier the provider documents.
    MISSING_SENDER = "missing_sender"
    #: A sender identifier longer than any form the provider documents.
    IDENTIFIER_TOO_LONG = "identifier_too_long"
    #: A status report without a status.
    MISSING_STATUS = "missing_status"
    #: The envelope is not the provider's shape at all.
    MALFORMED = "malformed"


#: The refusals that can mean a customer's message was lost, as opposed to a
#: change nobody subscribed Wasla to. What the alert fires on (OMNI-021).
MESSAGE_LOSS_REASONS: frozenset[RefusalReason] = frozenset(
    {
        RefusalReason.FOREIGN_OBJECT,
        RefusalReason.MISSING_CONNECTION,
        RefusalReason.MISSING_EVENT_ID,
        RefusalReason.MISSING_SENDER,
        RefusalReason.IDENTIFIER_TOO_LONG,
        RefusalReason.MALFORMED,
    }
)


@dataclass(frozen=True, slots=True)
class ReplyAction:
    """What a customer tapped: the provider's id or payload, and the words on it.

    Kept apart from the event's text so that routing never has to read display
    text where a payload exists - a button's title is translated and edited by
    whoever wrote the template, its payload is not (OMNI-030).
    """

    source: ReplyActionSource
    id_or_payload: str | None
    title: str | None


@dataclass(frozen=True, slots=True)
class Identifier:
    """One address a provider used for the person on the other end."""

    kind: IdentityKind
    value: str


@dataclass(frozen=True, slots=True)
class AttachmentLocator:
    """Where a provider says one attached file can be fetched from.

    A handle (WhatsApp) is resolved through the provider's API with the
    connection's credential; a URL is fetched directly through the SSRF guard
    and that provider's own host allow-list. Neither is ever a path here, and
    `filename` is only ever shown to a person.
    """

    locator_kind: MediaLocatorKind
    locator: str
    #: The provider's media family - image, document, audio, video, sticker.
    media_kind: str
    mime_type: str | None = None
    filename: str | None = None
    is_voice: bool = False
    #: When the locator stops working, where the provider says.
    expires_at: datetime | None = None
    #: The provider's own checksum - recorded, never trusted for the bytes.
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class StatusUpdate:
    """A report about a message the business sent.

    Per message (`message_id`) or as a watermark ("every message sent at or
    before this instant"). Messenger reports reads only as a watermark; WhatsApp
    and Instagram name the message (OMNI-011).
    """

    status: MessageStatus
    #: The provider's own word, for logs and evidence. Never shown to anybody.
    provider_status: str
    message_id: str | None = None
    watermark: datetime | None = None


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """One thing a provider delivered, in terms every adapter shares."""

    channel: Channel
    #: The provider's name for the connection this arrived on - a WhatsApp
    #: phone number id, a Page id. Routing resolves the workspace from this
    #: and from nothing the sender controls.
    connection_key: str
    kind: InboundKind
    #: The deduplication key within the connection.
    event_id: str
    #: The provider's timestamp, when it sent one.
    occurred_at: datetime | None
    #: Every identifier the provider asserted for the sender, most-preferred
    #: address first. Empty for a status.
    sender: tuple[Identifier, ...] = ()
    #: The provider's id for the message this is (a message or an echo).
    message_id: str | None = None
    message_kind: MessageKind = MessageKind.UNSUPPORTED
    #: What the customer typed, caption included.
    text: str | None = None
    attachments: tuple[AttachmentLocator, ...] = ()
    #: The provider's id of the message this one replies to.
    reply_to: str | None = None
    #: The control the customer tapped, when this message is a tap (OMNI-030).
    #: `text` then carries its title, so a reader of the transcript sees words.
    action: ReplyAction | None = None
    status: StatusUpdate | None = None
    #: A marketing stop or resume, for a `PREFERENCE` event (OMNI-046).
    preference: MarketingPreference | None = None
    profile_name: str | None = None
    #: The provider's own record of this event, stored as evidence and
    #: redacted on the retention schedule (DB-011).
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """A message or an echo is identified by the provider's message id (OMNI-032, ADR-120).

        Ingestion stores the message under `message_id`; inbound recovery and
        the stranded-media sweep find it again from the stored event by
        `event_id`. Those are one value for every Meta product - a message id
        is the event's identity - and the contract used to say so nowhere: an
        adapter that composed its event ids (as WhatsApp does for statuses)
        would have had every message that missed the queue abandoned as
        `projection_missing` and never answered. Refused at construction, so
        such an adapter fails its contract suite rather than a customer.
        Statuses are not messages and keep composed ids.
        """
        if self.kind in (InboundKind.MESSAGE, InboundKind.ECHO) and (
            not self.message_id or self.event_id != self.message_id
        ):
            raise ValueError(
                "a message or echo event must carry the provider's message id as its event id"
            )


@dataclass(frozen=True, slots=True)
class ParsedDelivery:
    """One webhook delivery as an adapter understood it."""

    events: tuple[InboundEvent, ...]
    refused: Mapping[RefusalReason, int] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def refused_total(self) -> int:
        return sum(self.refused.values())

    @property
    def message_loss(self) -> int:
        """Refusals that can have been a customer's message."""
        return sum(
            count for reason, count in self.refused.items() if reason in MESSAGE_LOSS_REASONS
        )


def tally(reasons: Counter[RefusalReason]) -> Mapping[RefusalReason, int]:
    """A read-only copy of a refusal counter, for a frozen delivery."""
    return MappingProxyType(dict(reasons))


__all__ = [
    "MAX_ACTION_PAYLOAD_LENGTH",
    "MAX_ACTION_TITLE_LENGTH",
    "MESSAGE_LOSS_REASONS",
    "AttachmentLocator",
    "Identifier",
    "InboundEvent",
    "InboundKind",
    "MarketingPreference",
    "ParsedDelivery",
    "RefusalReason",
    "ReplyAction",
    "ReplyActionSource",
    "StatusUpdate",
    "tally",
]
