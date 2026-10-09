"""Whether a set of channel connections fits a workspace's channel capacity (ENT-05..09).

Pure arithmetic over counts, kept apart from the database so the rule is one
function every caller shares - the guard, the capacity-reduction flow, the
summaries and the tests - and so it can be proved exhaustively.

**Capacity has two kinds of slot** (ENT-11, ADR-131):

* **General** slots - the plan version's `channel_connections`, plus general
  channel top-ups and platform grants. Any channel type the plan allows may
  use one.
* **Typed** slots - top-ups and grants bought or given for one channel type.
  Only a connection of that type may use one.

A typed slot absorbs connections of its own type first; whatever overflows it
uses general slots. So a set of active connections fits exactly when

    sum over types T of max(0, active[T] - typed[T])  <=  general

and a new connection of type T is allowed only if the set *after* adding it
fits. An unlimited base is unlimited: everything fits, and no typed slot matters.
Every connection weighs one slot, whatever its channel (ENT-06).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from app.db.models.channel import Channel


def _frozen(values: Mapping[Channel, int] | None) -> Mapping[Channel, int]:
    return MappingProxyType({channel: count for channel, count in (values or {}).items() if count})


@dataclass(frozen=True, slots=True)
class ChannelCapacity:
    """One workspace's channel slots, and what occupies them, at one moment.

    `base` is the plan version's general slots (None for unlimited). The
    purchased and granted figures are live top-ups and platform grants: general
    ones in `general_*`, typed ones by channel in `typed_*`. `active` counts
    the connections that take a slot - active and unreleased - by channel.
    `allowed` is the plan's channel types, carried so one object answers both
    questions the guard asks.
    """

    base: int | None
    general_purchased: int = 0
    general_granted: int = 0
    typed_purchased: Mapping[Channel, int] = field(default_factory=dict)
    typed_granted: Mapping[Channel, int] = field(default_factory=dict)
    active: Mapping[Channel, int] = field(default_factory=dict)
    allowed: frozenset[Channel] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "typed_purchased", _frozen(self.typed_purchased))
        object.__setattr__(self, "typed_granted", _frozen(self.typed_granted))
        object.__setattr__(self, "active", _frozen(self.active))

    # ----------------------------------------------------------------- slots

    @property
    def unlimited(self) -> bool:
        return self.base is None

    @property
    def general(self) -> int | None:
        """General slots: base plus general top-ups and grants. None if unlimited."""
        if self.base is None:
            return None
        return self.base + self.general_purchased + self.general_granted

    @property
    def typed(self) -> Mapping[Channel, int]:
        """Typed slots by channel: purchased plus granted."""
        channels = set(self.typed_purchased) | set(self.typed_granted)
        return MappingProxyType(
            {
                channel: self.typed_purchased.get(channel, 0) + self.typed_granted.get(channel, 0)
                for channel in channels
            }
        )

    @property
    def total(self) -> int | None:
        """Every slot, general and typed. None if unlimited."""
        general = self.general
        if general is None:
            return None
        return general + sum(self.typed.values())

    @property
    def purchased(self) -> int:
        return self.general_purchased + sum(self.typed_purchased.values())

    @property
    def granted(self) -> int:
        return self.general_granted + sum(self.typed_granted.values())

    # -------------------------------------------------------------- occupancy

    @property
    def active_total(self) -> int:
        return sum(self.active.values())

    def overflow(self, active: Mapping[Channel, int] | None = None) -> int:
        """How many connections typed slots do not absorb, and so need general ones."""
        counts = self.active if active is None else active
        typed = self.typed
        return sum(max(0, count - typed.get(channel, 0)) for channel, count in counts.items())

    def fits(self, active: Mapping[Channel, int] | None = None) -> bool:
        """Whether `active` (the current set by default) fits these slots."""
        general = self.general
        return general is None or self.overflow(active) <= general

    def fits_with(self, channel: Channel, *, count: int = 1) -> bool:
        """Whether the current set plus `count` connections of `channel` fits."""
        after = dict(self.active)
        after[channel] = after.get(channel, 0) + count
        return self.fits(after)

    @property
    def over_limit(self) -> bool:
        """The current set no longer fits - after a top-up expired, say."""
        return not self.fits()

    @property
    def general_remaining(self) -> int | None:
        """General slots still free for the next connection of a type with no typed slot."""
        general = self.general
        if general is None:
            return None
        return max(general - self.overflow(), 0)

    def typed_used(self, channel: Channel) -> int:
        """How many of `channel`'s typed slots its connections occupy."""
        return min(self.active.get(channel, 0), self.typed.get(channel, 0))


__all__ = ["ChannelCapacity"]
