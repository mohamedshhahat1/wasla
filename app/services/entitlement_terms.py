"""Reading a plan version's terms: limits and allowed channel types (ADR-131).

The one module that knows two retired shapes, so nothing else ever has to:

* **`whatsapp_numbers` is read as `channel_connections`** on versions published
  before ADR-131 (ENT-05, choice A). Those rows are immutable - a trigger refuses
  every UPDATE, and it is never disabled - so they are not rewritten; they are
  read. A version is "before" exactly when its `allowed_channel_types` is null,
  because every version published since must state them (a trigger refuses
  NULL on INSERT) and none of them may carry the old key (the same trigger).
* **A version with no channel types is read as WhatsApp alone** (ENT-09) - the
  only channel that has ever been operable - and never as "every channel": a
  malformed or legacy row must not open channels nobody sold.

`EntitlementService` reads every limit through `term_limit` and every channel
set through `term_channel_types`. The catalogue displays use the same two
functions, so a legacy version shows the capacity it is enforced at. Nothing
else may call `PlanVersion.limit_for` for a resource it enforces; a unit test
holds the codebase to that.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Protocol

from app.db.models.billing import RETIRED_LIMIT_KEY, LimitKey, validated_limit
from app.db.models.channel import Channel
from app.db.models.topup import TopupEntitlement

#: What a version that never stated its channel types allows (ENT-09).
LEGACY_CHANNEL_TYPES: Final[frozenset[Channel]] = frozenset({Channel.WHATSAPP})

#: Every channel label, in vocabulary order. What an "every type" plan names.
ALL_CHANNEL_TYPES: Final[tuple[Channel, ...]] = tuple(Channel)


class Terms(Protocol):
    """A plan version, or a plan row read as one before it has versions."""

    @property
    def limits(self) -> dict[str, object]: ...

    @property
    def allowed_channel_types(self) -> list[str] | None: ...


def is_legacy(terms: Terms) -> bool:
    """Whether these terms were published before ADR-131 (no channel types stated)."""
    return terms.allowed_channel_types is None


def term_limit(terms: Terms, key: LimitKey) -> int | None:
    """The ceiling `terms` set for `key`, or None for unlimited (ADR-029).

    `channel_connections` on a legacy version is its `whatsapp_numbers`: the
    number limit those terms were sold with is the capacity they are held to.
    Absent there too, it is unlimited, exactly as an absent key always was.
    """
    limits = terms.limits or {}
    if key is LimitKey.CHANNEL_CONNECTIONS and is_legacy(terms) and key.value not in limits:
        return validated_limit(limits.get(RETIRED_LIMIT_KEY))
    return validated_limit(limits.get(key.value))


def term_channel_types(terms: Terms | None) -> frozenset[Channel]:
    """The channel types `terms` allow; WhatsApp alone when they never said."""
    if terms is None or terms.allowed_channel_types is None:
        return LEGACY_CHANNEL_TYPES
    return frozenset(_labels(terms.allowed_channel_types))


def topup_slot_channel(
    entitlement: TopupEntitlement, channel_type: Channel | None
) -> Channel | None:
    """Which channel a live channel top-up's slots serve; None for a general slot.

    A `whatsapp_numbers` top-up bought before ADR-131 bought slots for WhatsApp
    numbers, so it is read as a typed WhatsApp slot - exactly what was paid for,
    neither narrower nor wider (ENT-05, ENT-10).
    """
    if entitlement is TopupEntitlement.WHATSAPP_NUMBERS:
        return Channel.WHATSAPP
    return channel_type


def ordered(channels: Iterable[Channel]) -> list[Channel]:
    """`channels` in vocabulary order, for responses and audit entries."""
    chosen = set(channels)
    return [channel for channel in ALL_CHANNEL_TYPES if channel in chosen]


def _labels(values: Iterable[str]) -> Iterable[Channel]:
    known = {channel.value: channel for channel in Channel}
    for value in values:
        # A label the trigger admitted is always known; one that is not (a
        # newer schema read by older code) grants nothing rather than raising.
        channel = known.get(value)
        if channel is not None:
            yield channel


__all__ = [
    "ALL_CHANNEL_TYPES",
    "LEGACY_CHANNEL_TYPES",
    "Terms",
    "is_legacy",
    "ordered",
    "term_channel_types",
    "term_limit",
    "topup_slot_channel",
]
