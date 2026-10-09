"""Which usage meters a channel's messages are counted under (ENT-22, ADR-131).

Decided for every channel in the vocabulary. The meters are channel-neutral -
one received, one sent - and every row carries the channel and the connection
as dimensions:

- WhatsApp writes `whatsapp_message_received` / `whatsapp_message_sent`, the
  meters every invoice, allowance and top-up has always read. They are the
  neutral meters' WhatsApp instance, mapped 1:1: nothing renames, re-types or
  re-counts them, so usage stays reproducible exactly as recorded
  (ARCHITECTURE section 19a) and no usage cycle is split between two labels.
- Every other channel writes `message_received` / `message_sent`.

`period_messages` adds up every label in `RECEIVED` and `SENT`, so a message
counts once whatever its channel. The registry still refuses an adapter for a
channel with no entry here - a label added to the vocabulary without a meter
cannot go live uncounted - but a channel's meter being decided does not make it
operable: that still takes an adapter (ADR-117).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.db.models.channel import Channel
from app.db.models.usage import UsageEventType


@dataclass(frozen=True, slots=True)
class MessageMeters:
    received: UsageEventType
    sent: UsageEventType


WHATSAPP_METERS: Final = MessageMeters(
    received=UsageEventType.WHATSAPP_MESSAGE_RECEIVED,
    sent=UsageEventType.WHATSAPP_MESSAGE_SENT,
)
NEUTRAL_METERS: Final = MessageMeters(
    received=UsageEventType.MESSAGE_RECEIVED,
    sent=UsageEventType.MESSAGE_SENT,
)

MESSAGE_METERS: Final[Mapping[Channel, MessageMeters]] = MappingProxyType(
    {
        channel: WHATSAPP_METERS if channel is Channel.WHATSAPP else NEUTRAL_METERS
        for channel in Channel
    }
)

#: Every label a received message is counted under, whatever its channel.
RECEIVED: Final[tuple[UsageEventType, ...]] = (
    WHATSAPP_METERS.received,
    NEUTRAL_METERS.received,
)
#: Every label a sent message is counted under, whatever its channel.
SENT: Final[tuple[UsageEventType, ...]] = (WHATSAPP_METERS.sent, NEUTRAL_METERS.sent)


def message_meters(channel: Channel) -> MessageMeters | None:
    """The meters for `channel`, or None for a channel nobody decided a meter for."""
    return MESSAGE_METERS.get(channel)


__all__ = [
    "MESSAGE_METERS",
    "NEUTRAL_METERS",
    "RECEIVED",
    "SENT",
    "WHATSAPP_METERS",
    "MessageMeters",
    "message_meters",
]
