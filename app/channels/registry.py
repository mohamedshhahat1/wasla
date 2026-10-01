"""Which adapter serves which channel - and the refusal when none does.

`Channel` lists channels the model can represent; this lists the ones Wasla can
actually operate. WhatsApp is the only entry. A connection or conversation on any
other channel finds no adapter here, and every path that would act on it - send,
fetch a file, choose a policy - refuses with `ChannelUnavailableError` rather
than falling back to WhatsApp's (ADR-117). That fallback is the wrong-channel
send the whole seam exists to prevent.

The default registry is built lazily so this module imports no provider; tests
construct their own with a synthetic adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from types import MappingProxyType

from app.channels.adapter import ChannelAdapter
from app.channels.metering import message_meters
from app.channels.policy import ChannelPolicy
from app.core.exceptions import ValidationError
from app.db.models.channel import Channel


class ChannelUnavailableError(ValidationError):
    """Wasla has no adapter for this channel, so nothing is sent or fetched on it."""

    message = "Wasla cannot operate this channel."


class ChannelRegistry:
    """Adapters by channel. Immutable once built."""

    def __init__(
        self,
        adapters: Mapping[Channel, ChannelAdapter],
        *,
        unmetered: bool = False,
    ) -> None:
        """Register adapters. A channel with no decided usage meter is refused.

        `unmetered` is for a synthetic adapter in a test, which opts out of the
        rule explicitly; nothing in the application passes it (ADR-122).
        """
        for channel, adapter in adapters.items():
            if adapter.channel is not channel:
                raise ValueError(f"the {adapter.channel} adapter was registered for {channel}")
            if not unmetered and message_meters(channel) is None:
                raise ValueError(f"{channel} has no decided usage meter (ADR-122)")
        self._adapters: Mapping[Channel, ChannelAdapter] = MappingProxyType(dict(adapters))

    @property
    def channels(self) -> frozenset[Channel]:
        return frozenset(self._adapters)

    def adapter_for(self, channel: Channel) -> ChannelAdapter:
        adapter = self._adapters.get(channel)
        if adapter is None:
            raise ChannelUnavailableError()
        return adapter

    def policy_for(self, channel: Channel) -> ChannelPolicy:
        return self.adapter_for(channel).policy


@lru_cache(maxsize=1)
def default_registry() -> ChannelRegistry:
    """The channels this deployment operates: WhatsApp."""
    from app.integrations.whatsapp.adapter import WhatsAppAdapter

    return ChannelRegistry({Channel.WHATSAPP: WhatsAppAdapter()})


__all__ = ["ChannelRegistry", "ChannelUnavailableError", "default_registry"]
