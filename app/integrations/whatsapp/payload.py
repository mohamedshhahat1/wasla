"""Webhook payload parsing - the WhatsApp adapter's view of what Meta sent.

Nothing in this module raises. Meta adds fields and message types continuously,
and a parser that rejects what it does not recognise would drop legitimate
traffic the day a new type ships. Unrecognised entries are counted - by a
bounded reason, so a metric can say *why* - and the raw payload is stored whole
by the caller so anything not understood today can be replayed later.

Two rules this module used to break, both silently:

**A sender is whoever Meta says it is, and Meta no longer always says a phone
number** (OMNI-002). Since April 2026 every message webhook carries a
business-scoped user id (`messages[].from_user_id`, `contacts[].user_id`), and a
user with a username may arrive with no `from` and no `wa_id` at all. The parser
required `from`, so such a message was counted as ignored, answered 200, never
stored and never recovered - a customer's first message lost without trace. A
message is now accepted with any identifier Meta documents, and every one it
carries is kept: a phone and a business-scoped id arriving together are Meta
asserting they are the same person, which is the only basis on which Wasla ever
links two identities (ADR-118).

**A payload is WhatsApp's only if it says so** (OMNI-010). The top-level `object`
must be `whatsapp_business_account` and each change's `field` must be one this
adapter processes. A Page event posted here by a misconfigured subscription, or
a Coexistence `smb_message_echoes` change, used to parse to nothing with
`ignored: 0`; both are now refused and counted by reason.

Provider facts relied on here were re-checked against Meta's documentation on
2026-10-01 (OMNICHANNEL_READINESS_FINDINGS_REMEDIATION.md, section 5).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final

from app.channels.inbound import RefusalReason
from app.core.filenames import display_filename
from app.core.media_types import MAX_MIME_TYPE_LENGTH
from app.db.models.channel import MAX_IDENTITY_VALUE_LENGTH
from app.db.models.media import MAX_MEDIA_HANDLE_LENGTH

TEXT_TYPE = "text"

# The one `object` a WhatsApp Business Account webhook carries.
WHATSAPP_OBJECT: Final = "whatsapp_business_account"

# The change fields this adapter processes. Messages and their statuses both
# arrive under `messages`; everything else a WABA can be subscribed to -
# template status updates, account updates, and the Coexistence fields
# `history`, `smb_app_state_sync` and `smb_message_echoes` - is refused and
# counted until something here handles it (ADR-120).
SUPPORTED_FIELDS: Final = frozenset({"messages"})

# The width of `contacts.wa_id`. A phone number is at most fifteen digits
# (E.164); anything past this column is not a number Meta issued.
MAX_PHONE_LENGTH: Final = 32

# Meta's media message types. Voice notes arrive as "voice" rather than "audio"
# and carry the same descriptor, so both are read the same way; the distinction
# survives in the raw payload for anyone who needs it.
MEDIA_TYPES: Final = ("image", "document", "audio", "voice", "video", "sticker")


@dataclass(frozen=True, slots=True)
class InboundMedia:
    """The descriptor Meta sends instead of the file itself.

    `media_id` is a handle, not a URL: the file is fetched in two steps and the
    handle expires, which is why downloading is a worker's job rather than
    something the webhook could do on the way past.

    `sha256` is Meta's own checksum of the bytes. It is kept for the same reason
    a document's content hash is - recognising the same file twice - and never
    trusted as a substitute for hashing what actually arrived.
    """

    media_id: str
    kind: str
    mime_type: str | None
    sha256: str | None
    filename: str | None
    is_voice: bool


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A customer message. `event_id` is Meta's own message id.

    The sender is `from_number` (the phone number, `from`) and/or
    `from_user_id` (the business-scoped user id, `from_user_id`) - at least one
    of the two, and since April 2026 usually both. `from_parent_user_id` is the
    parent business-scoped id Meta adds for businesses managing several
    portfolios; it is carried, never used to link anything.

    `profile_name` comes from the delivery's `contacts` block rather than from
    the message itself, which is the only place Meta sends it.

    `text` carries a media message's caption as well as a text message's body,
    because a caption is what the customer actually typed. What Wasla later
    infers about the file - a transcript, a description - is deliberately not
    put here: the two must stay distinguishable in the stored conversation.
    """

    event_id: str
    phone_number_id: str
    from_number: str | None
    message_type: str
    timestamp: datetime | None
    text: str | None
    raw: dict[str, Any]
    profile_name: str | None = None
    media: InboundMedia | None = None
    from_user_id: str | None = None
    from_parent_user_id: str | None = None
    #: `context.id`: the message this one replies to, as Meta names it.
    context_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryStatus:
    """A status update for a message we sent.

    `event_id` is composed as `{message_id}:{status}`. Meta reports sent,
    delivered and read for the same message under the same id, so keying on the
    id alone would file the first status and discard the rest as duplicates.
    """

    event_id: str
    phone_number_id: str
    message_id: str
    recipient: str | None
    status: str
    timestamp: datetime | None
    raw: dict[str, Any]
    recipient_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    """What one delivery held. `ignored` is every refusal; `refused` says why."""

    messages: tuple[InboundMessage, ...]
    statuses: tuple[DeliveryStatus, ...]
    ignored: int
    refused: Mapping[RefusalReason, int] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def is_empty(self) -> bool:
        return not self.messages and not self.statuses


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return list(value)
    return []


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bounded(value: Any, limit: int) -> str | None:
    """An identifier Meta could have issued, or None."""
    text = _text(value)
    return text if text is not None and len(text) <= limit else None


def _timestamp(value: Any) -> datetime | None:
    """Meta sends epoch seconds as a string. Anything else is discarded."""
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _message_text(message: Mapping[str, Any], message_type: str) -> str | None:
    """The words the customer typed, whether alone or attached to a file.

    A caption counts. It is the customer's own sentence and often carries the
    whole question - "how much is this one?" under a photo - so dropping it
    would leave the agent with a picture and no idea what was being asked.

    What Wasla later concludes about the file is not text and does not come back
    from here.
    """
    if message_type == TEXT_TYPE:
        return _text(_mapping(message.get(TEXT_TYPE)).get("body"))
    if message_type in MEDIA_TYPES:
        return _text(_mapping(message.get(message_type)).get("caption"))
    return None


def _media(message: Mapping[str, Any], message_type: str) -> InboundMedia | None:
    """Read the media descriptor, or None if this message carries no file.

    An entry without an id is treated as no media at all rather than as a
    parse failure: the message itself is still worth storing, and there is
    nothing to download without the handle.

    The filename is never used to build a path. It arrives from a stranger's
    phone, and a value like "../../etc/passwd" is a request, not an accident.
    Storage derives its own key; this is only ever shown to a person.

    Every string here is brought within its column before anything is stored
    (MEDIA-05). A 301-character document name used to fail the webhook's write
    and, because the whole delivery is one transaction, lose every sibling
    message with it on each of Meta's retries. The name is normalised and
    bounded (`display_filename`); a declared type too wide to record is dropped,
    since the bytes decide the type anyway; and a handle longer than any Meta
    issues is treated as no handle at all.
    """
    if message_type not in MEDIA_TYPES:
        return None

    descriptor = _mapping(message.get(message_type))
    media_id = _text(descriptor.get("id"))
    if media_id is None or len(media_id) > MAX_MEDIA_HANDLE_LENGTH:
        return None

    return InboundMedia(
        media_id=media_id,
        kind=message_type,
        mime_type=_mime_type(descriptor.get("mime_type")),
        sha256=_text(descriptor.get("sha256")),
        filename=display_filename(descriptor.get("filename")),
        # Meta marks a recorded voice note this way; an attached audio file
        # arrives without it. Both are transcribed, but only one is somebody
        # speaking to the business, and that is worth keeping.
        is_voice=message_type == "voice" or descriptor.get("voice") is True,
    )


def _mime_type(value: Any) -> str | None:
    """Strip the codec parameters Meta appends to audio types.

    A voice note arrives as "audio/ogg; codecs=opus". The parameters matter to a
    decoder and not to us, and keeping them would make two identical types
    compare unequal wherever the value is matched.
    """
    text = _text(value)
    if text is None:
        return None
    declared = text.split(";", 1)[0].strip()
    if not declared or len(declared) > MAX_MIME_TYPE_LENGTH:
        return None
    return declared


def _profile_names(value: Mapping[str, Any]) -> dict[str, str]:
    """Map every identifier Meta names a contact by to its profile name.

    The block is optional and a customer may have no name set, so a missing
    entry is normal rather than a parse failure. Keyed by the phone number and
    by the business-scoped id alike, so a sender known by either finds it.
    """
    names: dict[str, str] = {}
    for raw_contact in _sequence(value.get("contacts")):
        contact = _mapping(raw_contact)
        name = _text(_mapping(contact.get("profile")).get("name"))
        if name is None:
            continue
        for key in ("wa_id", "user_id"):
            identifier = _text(contact.get(key))
            if identifier is not None:
                names[identifier] = name
    return names


def _sender(message: Mapping[str, Any]) -> tuple[str | None, str | None, bool]:
    """The phone and business-scoped id a message was sent from, and whether one was dropped.

    Each is kept only if it is within what Meta documents; one that is not is
    dropped rather than stored, and reported so the caller can refuse the
    message if nothing usable is left.
    """
    raw_phone = _text(message.get("from"))
    raw_user = _text(message.get("from_user_id"))
    phone = _bounded(raw_phone, MAX_PHONE_LENGTH)
    user = _bounded(raw_user, MAX_IDENTITY_VALUE_LENGTH)
    dropped = (raw_phone is not None and phone is None) or (raw_user is not None and user is None)
    return phone, user, dropped


def parse_webhook(payload: Mapping[str, Any]) -> WebhookEnvelope:
    """Flatten Meta's nested envelope into messages and statuses, counting what is refused."""
    messages: list[InboundMessage] = []
    statuses: list[DeliveryStatus] = []
    refused: Counter[RefusalReason] = Counter()
    entries = _sequence(payload.get("entry"))

    obj = payload.get("object")
    if obj != WHATSAPP_OBJECT:
        # Not a WhatsApp Business Account delivery: another Meta product's
        # (a misrouted subscription) or not Meta's shape at all. Every entry
        # is refused and counted - and a payload with no entries still counts
        # once, so it is never an empty success.
        reason = (
            RefusalReason.FOREIGN_OBJECT
            if isinstance(obj, str) and obj
            else RefusalReason.MALFORMED
        )
        refused[reason] += max(len(entries), 1)
        return _envelope(messages, statuses, refused)

    for entry in entries:
        for raw_change in _sequence(_mapping(entry).get("changes")):
            change = _mapping(raw_change)
            change_field = change.get("field")
            # A string first: a list is unhashable and would raise on the
            # membership test, and this module does not raise.
            if not isinstance(change_field, str) or change_field not in SUPPORTED_FIELDS:
                refused[RefusalReason.UNSUPPORTED_FIELD] += 1
                continue

            value = _mapping(change.get("value"))
            phone_number_id = _text(_mapping(value.get("metadata")).get("phone_number_id"))
            if phone_number_id is None:
                # Without it there is no way to know which workspace this is for.
                refused[RefusalReason.MISSING_CONNECTION] += 1
                continue

            profile_names = _profile_names(value)

            for raw_message in _sequence(value.get("messages")):
                message = _mapping(raw_message)
                event_id = _text(message.get("id"))
                if event_id is None:
                    refused[RefusalReason.MISSING_EVENT_ID] += 1
                    continue
                phone, user_id, dropped = _sender(message)
                if phone is None and user_id is None:
                    refused[
                        (
                            RefusalReason.IDENTIFIER_TOO_LONG
                            if dropped
                            else RefusalReason.MISSING_SENDER
                        )
                    ] += 1
                    continue

                message_type = _text(message.get("type")) or "unknown"
                messages.append(
                    InboundMessage(
                        event_id=event_id,
                        phone_number_id=phone_number_id,
                        from_number=phone,
                        message_type=message_type,
                        timestamp=_timestamp(message.get("timestamp")),
                        text=_message_text(message, message_type),
                        raw=message,
                        profile_name=profile_names.get(phone or "")
                        or profile_names.get(user_id or ""),
                        media=_media(message, message_type),
                        from_user_id=user_id,
                        from_parent_user_id=_bounded(
                            message.get("from_parent_user_id"), MAX_IDENTITY_VALUE_LENGTH
                        ),
                        context_id=_text(_mapping(message.get("context")).get("id")),
                    )
                )

            for raw_status in _sequence(value.get("statuses")):
                status_payload = _mapping(raw_status)
                message_id = _text(status_payload.get("id"))
                status = _text(status_payload.get("status"))
                if message_id is None:
                    refused[RefusalReason.MISSING_EVENT_ID] += 1
                    continue
                if status is None:
                    refused[RefusalReason.MISSING_STATUS] += 1
                    continue

                statuses.append(
                    DeliveryStatus(
                        event_id=f"{message_id}:{status}",
                        phone_number_id=phone_number_id,
                        message_id=message_id,
                        recipient=_text(status_payload.get("recipient_id")),
                        status=status,
                        timestamp=_timestamp(status_payload.get("timestamp")),
                        raw=status_payload,
                        recipient_user_id=_bounded(
                            status_payload.get("recipient_user_id"), MAX_IDENTITY_VALUE_LENGTH
                        ),
                    )
                )

    return _envelope(messages, statuses, refused)


def _envelope(
    messages: list[InboundMessage],
    statuses: list[DeliveryStatus],
    refused: Counter[RefusalReason],
) -> WebhookEnvelope:
    return WebhookEnvelope(
        messages=tuple(messages),
        statuses=tuple(statuses),
        ignored=sum(refused.values()),
        refused=MappingProxyType(dict(refused)),
    )
