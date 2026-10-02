"""The channel capacity fit rule and the plan-terms reader (ENT-05, ENT-06, ENT-09, ENT-11).

Pure arithmetic, so it is proved exhaustively here: every small combination of
general and typed slots against every small set of active connections, compared
with a brute-force assignment of connections to slots. The database-backed
suites then prove the same rule is the one enforced.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from app.db.models.billing import LimitKey
from app.db.models.channel import Channel
from app.db.models.topup import TopupEntitlement
from app.services.channel_fit import ChannelCapacity
from app.services.entitlement_terms import (
    LEGACY_CHANNEL_TYPES,
    term_channel_types,
    term_limit,
    topup_slot_channel,
)

WA, IG, MS, TG, TT = (
    Channel.WHATSAPP,
    Channel.INSTAGRAM,
    Channel.MESSENGER,
    Channel.TELEGRAM,
    Channel.TIKTOK,
)


def _brute_force_fits(general: int, typed: dict[Channel, int], active: dict[Channel, int]) -> bool:
    """Try every way of seating each connection in a typed slot of its own type or a general one."""
    seats: list[Channel | None] = [None] * general
    for channel, count in typed.items():
        seats += [channel] * count
    people = [channel for channel, count in active.items() for _ in range(count)]
    if len(people) > len(seats):
        return False
    for chosen in itertools.permutations(range(len(seats)), len(people)):
        if all(seats[seat] in (None, person) for seat, person in zip(chosen, people, strict=True)):
            return True
    return False


@pytest.mark.parametrize("general", [0, 1, 2, 3])
@pytest.mark.parametrize("typed_ig", [0, 1, 2])
@pytest.mark.parametrize("typed_wa", [0, 1])
def test_the_fit_rule_agrees_with_seating_every_connection(
    general: int, typed_ig: int, typed_wa: int
) -> None:
    typed = {channel: count for channel, count in ((IG, typed_ig), (WA, typed_wa)) if count}
    capacity = ChannelCapacity(base=general, typed_purchased=typed)
    for wa, ig, ms in itertools.product(range(4), range(4), range(3)):
        active = {channel: n for channel, n in ((WA, wa), (IG, ig), (MS, ms)) if n}
        assert capacity.fits(active) == _brute_force_fits(general, typed, active), (active, typed)


def test_a_typed_slot_absorbs_its_own_type_first_and_overflow_takes_general_slots() -> None:
    # 1 general + 1 Instagram slot, one WhatsApp active: an Instagram fits, a WhatsApp does not.
    capacity = ChannelCapacity(base=1, typed_purchased={IG: 1}, active={WA: 1})
    assert capacity.fits()
    assert capacity.fits_with(IG)
    assert not capacity.fits_with(WA)
    assert capacity.general_remaining == 0
    assert capacity.total == 2
    # Two Instagram: the second overflows into the general slot, which is free.
    only_ig = ChannelCapacity(base=1, typed_purchased={IG: 1}, active={IG: 1})
    assert only_ig.fits_with(IG)
    assert not ChannelCapacity(base=1, typed_purchased={IG: 1}, active={IG: 2}).fits_with(IG)


def test_every_connection_weighs_one_slot_whatever_its_channel() -> None:
    """ENT-06: no per-channel weights."""
    for channel in Channel:
        capacity = ChannelCapacity(base=3, active={WA: 1, IG: 1})
        assert capacity.fits_with(channel)
        full = ChannelCapacity(base=3, active={WA: 1, IG: 1, MS: 1})
        assert not full.fits_with(channel)


def test_an_unlimited_base_fits_everything_and_zero_is_zero() -> None:
    unlimited = ChannelCapacity(base=None, active={WA: 500, TT: 70})
    assert unlimited.fits() and unlimited.fits_with(TG)
    assert (unlimited.general, unlimited.total, unlimited.general_remaining) == (None, None, None)
    zero = ChannelCapacity(base=0)
    assert zero.fits() and not zero.fits_with(WA)


def test_over_limit_after_capacity_falls_and_remaining_never_goes_negative() -> None:
    """Starter 1 + 2 top-up = 3 with three active; the top-up ends: 1 slot, 3 active."""
    while_bought = ChannelCapacity(base=1, general_purchased=2, active={WA: 1, IG: 1, MS: 1})
    assert (while_bought.total, while_bought.active_total, while_bought.over_limit) == (3, 3, False)
    assert while_bought.general_remaining == 0
    after = ChannelCapacity(base=1, active={WA: 1, IG: 1, MS: 1})
    assert (after.over_limit, after.general_remaining) == (True, 0)


def test_typed_used_counts_only_what_the_typed_slots_hold() -> None:
    capacity = ChannelCapacity(base=2, typed_granted={IG: 2}, active={IG: 3})
    assert capacity.typed_used(IG) == 2
    assert capacity.overflow() == 1
    assert capacity.granted == 2 and capacity.purchased == 0


# ---------------------------------------------------------------- terms


def _terms(limits: dict[str, object], allowed: list[str] | None) -> SimpleNamespace:
    return SimpleNamespace(limits=limits, allowed_channel_types=allowed)


def test_a_legacy_version_reads_its_number_limit_as_channel_capacity() -> None:
    """ENT-05 choice A: published before ADR-131, never rewritten, read through the alias."""
    legacy = _terms({"whatsapp_numbers": 3, "agents": 5}, None)
    assert term_limit(legacy, LimitKey.CHANNEL_CONNECTIONS) == 3
    assert term_limit(legacy, LimitKey.AGENTS) == 5
    # Absent there too: unlimited, as an absent key always was.
    assert term_limit(_terms({}, None), LimitKey.CHANNEL_CONNECTIONS) is None


def test_a_new_version_is_never_read_through_the_alias() -> None:
    """A version that states its types reads channel_connections or nothing."""
    stated = _terms({"whatsapp_numbers": 3}, ["whatsapp"])
    assert term_limit(stated, LimitKey.CHANNEL_CONNECTIONS) is None
    assert term_limit(_terms({"channel_connections": 0}, []), LimitKey.CHANNEL_CONNECTIONS) == 0


def test_a_version_that_never_stated_types_allows_whatsapp_alone_never_all() -> None:
    """ENT-09: a malformed or legacy row must not open every channel."""
    assert term_channel_types(_terms({}, None)) == LEGACY_CHANNEL_TYPES == {WA}
    assert term_channel_types(None) == {WA}
    assert term_channel_types(_terms({}, [])) == frozenset()
    assert term_channel_types(_terms({}, ["instagram", "whatsapp"])) == {WA, IG}
    # A label this code does not know grants nothing rather than raising.
    assert term_channel_types(_terms({}, ["whatsapp", "carrier-pigeon"])) == {WA}


def test_a_retired_number_top_up_is_a_typed_whatsapp_slot() -> None:
    assert topup_slot_channel(TopupEntitlement.WHATSAPP_NUMBERS, None) is WA
    assert topup_slot_channel(TopupEntitlement.CHANNEL_CONNECTIONS, None) is None
    assert topup_slot_channel(TopupEntitlement.CHANNEL_CONNECTIONS, IG) is IG
