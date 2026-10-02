"""Which adapter serves which channel - its state, and the refusal when it cannot act.

`Channel` lists channels the model can represent; this lists the ones Wasla can
actually operate. WhatsApp is the only adapter. A connection or conversation on
any other channel finds no adapter here, and every path that would act on it -
send, fetch a file, run an agent - refuses with `ChannelUnavailableError` rather
than falling back to WhatsApp's (ADR-117). That fallback is the wrong-channel
send the whole seam exists to prevent.

**A channel is in one of three states** (OMNI-031, ADR-126):

| State | Inbound | Inbox | Send, AI, file fetch |
| --- | --- | --- | --- |
| operational | stored and projected | rendered | allowed |
| paused | stored and projected | rendered, nothing sendable | refused |
| unavailable (no adapter) | no route | rendered, nothing sendable | refused |

Pausing is configuration (`PAUSED_CHANNELS`), not a deploy, and never requires
removing the adapter: rolling a channel back must not break any other channel's
inbox. Presentation is the one thing that tolerates every state - it renders a
conversation it cannot act on, instead of failing the whole page.

The default registry is built lazily so this module imports no provider; tests
construct their own with a synthetic adapter.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import timedelta
from functools import lru_cache
from types import MappingProxyType

from app.channels.adapter import ChannelAdapter
from app.channels.metering import message_meters
from app.channels.policy import ChannelPolicy, ChannelState
from app.core.exceptions import ValidationError
from app.db.models.channel import Channel

#: How long a sweep waits before looking at a paused channel again. Nothing is
#: staged while it waits, so a pause spends no follow-up or campaign attempt.
PAUSED_RECHECK = timedelta(minutes=15)


class ChannelUnavailableError(ValidationError):
    """Wasla cannot act on this channel, so nothing is sent or fetched on it."""

    message = "Wasla cannot operate this channel."


class ChannelPausedError(ChannelUnavailableError):
    """The channel is paused: its inbound is kept, and nothing is sent or fetched."""

    message = "This channel is paused. Messages are received, and nothing can be sent."


class ChannelRegistry:
    """Adapters by channel, and which of them are paused. Immutable once built."""

    def __init__(
        self,
        adapters: Mapping[Channel, ChannelAdapter],
        *,
        paused: Iterable[Channel] = (),
    ) -> None:
        """Register adapters. A channel with no decided usage meter is refused.

        Every channel in the vocabulary has its meters decided (ENT-22), so the
        refusal guards the next label added without one: it cannot go live
        uncounted. `paused` may name a channel with no adapter; it is then
        simply unavailable.
        """
        for channel, adapter in adapters.items():
            if adapter.channel is not channel:
                raise ValueError(f"the {adapter.channel} adapter was registered for {channel}")
            if message_meters(channel) is None:
                raise ValueError(f"{channel} has no decided usage meter (ENT-22)")
        self._adapters: Mapping[Channel, ChannelAdapter] = MappingProxyType(dict(adapters))
        self._paused: frozenset[Channel] = frozenset(paused)

    @property
    def channels(self) -> frozenset[Channel]:
        return frozenset(self._adapters)

    def state_for(self, channel: Channel) -> ChannelState:
        if channel not in self._adapters:
            return ChannelState.UNAVAILABLE
        if channel in self._paused:
            return ChannelState.PAUSED
        return ChannelState.OPERATIONAL

    def adapter_for(self, channel: Channel) -> ChannelAdapter:
        """The adapter to *act* with: refused unless the channel is operational."""
        adapter = self._adapters.get(channel)
        if adapter is None:
            raise ChannelUnavailableError()
        if channel in self._paused:
            raise ChannelPausedError()
        return adapter

    def policy_for(self, channel: Channel) -> ChannelPolicy:
        """The policy to act under: refused unless the channel is operational."""
        return self.adapter_for(channel).policy

    def known_policy(self, channel: Channel) -> ChannelPolicy | None:
        """The channel's policy to *describe* it with, paused or not; None without an adapter.

        For presentation only - what a client is told about a conversation it
        may not be able to act on. Nothing that sends reads it.
        """
        adapter = self._adapters.get(channel)
        return adapter.policy if adapter is not None else None


@lru_cache(maxsize=1)
def default_registry() -> ChannelRegistry:
    """The channels this deployment operates: WhatsApp, less any `PAUSED_CHANNELS`."""
    from app.core.config import get_settings
    from app.integrations.whatsapp.adapter import WhatsAppAdapter

    return ChannelRegistry(
        {Channel.WHATSAPP: WhatsAppAdapter()},
        paused=(Channel(name) for name in get_settings().paused_channels),
    )


__all__ = [
    "PAUSED_RECHECK",
    "ChannelPausedError",
    "ChannelRegistry",
    "ChannelState",
    "ChannelUnavailableError",
    "default_registry",
]
