"""Webhook payload parsing.

Nothing in this module raises. Meta adds fields and message types continuously,
and a parser that rejects what it does not recognise would drop legitimate
traffic the day a new type ships. Unrecognised entries are counted so the
skipping is visible, and the raw payload is stored whole by the caller so
anything not understood today can be replayed later.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

TEXT_TYPE = "text"

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

    `profile_name` comes from the delivery's `contacts` block rather than from
    the message itself, which is the only place Meta sends it. It is last and
    optional so the parser's existing callers are unaffected.

    `text` carries a media message's caption as well as a text message's body,
    because a caption is what the customer actually typed. What Wasla later
    infers about the file - a transcript, a description - is deliberately not
    put here: the two must stay distinguishable in the stored conversation.
    """

    event_id: str
    phone_number_id: str
    from_number: str
    message_type: str
    timestamp: datetime | None
    text: str | None
    raw: dict[str, Any]
    profile_name: str | None = None
    media: InboundMedia | None = None


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


@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    messages: tuple[InboundMessage, ...]
    statuses: tuple[DeliveryStatus, ...]
    ignored: int

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

    The filename is passed through exactly as Meta sent it and is never used to
    build a path. It arrives from a stranger's phone, and a value like
    "../../etc/passwd" is a request, not an accident. Storage derives its own
    key; this is only ever shown to a person.
    """
    if message_type not in MEDIA_TYPES:
        return None

    descriptor = _mapping(message.get(message_type))
    media_id = _text(descriptor.get("id"))
    if media_id is None:
        return None

    return InboundMedia(
        media_id=media_id,
        kind=message_type,
        mime_type=_mime_type(descriptor.get("mime_type")),
        sha256=_text(descriptor.get("sha256")),
        filename=_text(descriptor.get("filename")),
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
    return text.split(";", 1)[0].strip() or None


def _profile_names(value: Mapping[str, Any]) -> dict[str, str]:
    """Map WhatsApp id to profile name from the delivery's contacts block.

    The block is optional and a customer may have no name set, so a missing
    entry is normal rather than a parse failure.
    """
    names: dict[str, str] = {}
    for raw_contact in _sequence(value.get("contacts")):
        contact = _mapping(raw_contact)
        wa_id = _text(contact.get("wa_id"))
        name = _text(_mapping(contact.get("profile")).get("name"))
        if wa_id is not None and name is not None:
            names[wa_id] = name
    return names


def parse_webhook(payload: Mapping[str, Any]) -> WebhookEnvelope:
    """Flatten Meta's nested envelope into messages and statuses."""
    messages: list[InboundMessage] = []
    statuses: list[DeliveryStatus] = []
    ignored = 0

    for entry in _sequence(payload.get("entry")):
        for change in _sequence(_mapping(entry).get("changes")):
            value = _mapping(_mapping(change).get("value"))
            phone_number_id = _text(_mapping(value.get("metadata")).get("phone_number_id"))
            if phone_number_id is None:
                # Without it there is no way to know which workspace this is for.
                ignored += 1
                continue

            profile_names = _profile_names(value)

            for raw_message in _sequence(value.get("messages")):
                message = _mapping(raw_message)
                event_id = _text(message.get("id"))
                from_number = _text(message.get("from"))
                if event_id is None or from_number is None:
                    ignored += 1
                    continue

                message_type = _text(message.get("type")) or "unknown"
                messages.append(
                    InboundMessage(
                        event_id=event_id,
                        phone_number_id=phone_number_id,
                        from_number=from_number,
                        message_type=message_type,
                        timestamp=_timestamp(message.get("timestamp")),
                        text=_message_text(message, message_type),
                        raw=message,
                        profile_name=profile_names.get(from_number),
                        media=_media(message, message_type),
                    )
                )

            for raw_status in _sequence(value.get("statuses")):
                status_payload = _mapping(raw_status)
                message_id = _text(status_payload.get("id"))
                status = _text(status_payload.get("status"))
                if message_id is None or status is None:
                    ignored += 1
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
                    )
                )

    return WebhookEnvelope(
        messages=tuple(messages),
        statuses=tuple(statuses),
        ignored=ignored,
    )
