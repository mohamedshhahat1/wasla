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
from typing import Any

TEXT_TYPE = "text"


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A customer message. `event_id` is Meta's own message id."""

    event_id: str
    phone_number_id: str
    from_number: str
    message_type: str
    timestamp: datetime | None
    text: str | None
    raw: dict[str, Any]


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
    """Only genuine text is extracted. Everything else keeps its raw payload."""
    if message_type != TEXT_TYPE:
        return None
    return _text(_mapping(message.get(TEXT_TYPE)).get("body"))


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
