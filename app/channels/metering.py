"""Which usage meters a channel's messages are counted under (OMNI-013, ADR-122).

Only WhatsApp's are decided, and they are the meters every invoice, allowance
and top-up has always read: `whatsapp_message_received` and
`whatsapp_message_sent`. Nothing here renames, re-types or re-counts them - usage
events must stay reproducible exactly as recorded (ARCHITECTURE section 19a).

Whether another channel's messages count against the same allowance, their own,
or none is a commercial decision this code does not take (ADR-122). The
registry refuses to register an adapter for a channel with no entry here, so no
channel can go live uncounted by accident; a synthetic adapter in a test opts
out explicitly.
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


MESSAGE_METERS: Final[Mapping[Channel, MessageMeters]] = MappingProxyType(
    {
        Channel.WHATSAPP: MessageMeters(
            received=UsageEventType.WHATSAPP_MESSAGE_RECEIVED,
            sent=UsageEventType.WHATSAPP_MESSAGE_SENT,
        ),
    }
)


def message_meters(channel: Channel) -> MessageMeters | None:
    """The meters for `channel`, or None where no commercial decision exists."""
    return MESSAGE_METERS.get(channel)


__all__ = ["MESSAGE_METERS", "MessageMeters", "message_meters"]
