"""Which immediate peers may speak for the client behind them.

`TRUSTED_PROXY_IPS` is compared against `request.client.host`, which is an IP
address. It used to be compared as a *string*, and the shipped
`docker-compose.prod.yml` set it to the Docker service name ``nginx`` - a value
that can never equal an address (ADR-060). Nothing matched, so no forwarding
header was ever believed, and two controls failed silently together:

- **Authentication rate limiting collapsed to one bucket.** Every request from
  the internet was counted under the nginx container's own address, so the
  whole world shared a ten-per-minute budget. That is not a weakened limit, it
  is an outage anybody can trigger.
- **HSTS was never emitted.** `SecurityHeadersMiddleware` decides whether a
  request arrived over HTTPS from the same trust test.

So the comparison is done on parsed addresses and networks here, in one place
both callers use. An entry may be a bare address (which becomes a single-host
network) or a CIDR block, IPv4 or IPv6.

**A malformed entry is refused at startup**, following the rule the settings
module already keeps for a lifetime out of range: silently correcting or
ignoring configuration is how an operator comes to believe a proxy is trusted
when it is not. And there is deliberately **no name resolution**: a hostname
that resolves is a trust anchor somebody else's DNS can move, and the whole
point of this list is that it cannot be influenced from outside.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Collection, Iterable
from functools import lru_cache

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_trusted_proxies(entries: Iterable[str]) -> tuple[IPNetwork, ...]:
    """Turn configured entries into networks, refusing anything that is not one.

    `strict=False` so a bare address is accepted and becomes a `/32` or `/128`,
    which is what an operator naming a single proxy expects to write.

    :raises ValueError: naming the offending entry, so a container that will
        not start says which value to fix.
    """
    networks: list[IPNetwork] = []
    for entry in entries:
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError as error:
            raise ValueError(
                f"TRUSTED_PROXY_IPS entry {candidate!r} is not an IP address or "
                "CIDR network. A hostname is not accepted: it would put the "
                "trust decision under somebody else's DNS."
            ) from error
    return tuple(networks)


@lru_cache(maxsize=8)
def _cached(entries: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    """Parsed once per distinct configuration, not once per request.

    Settings are fixed for the life of a process, so this holds a single entry
    in practice. Keyed on the tuple rather than on a `Settings` instance so it
    stays a pure function of the values.
    """
    return parse_trusted_proxies(entries)


def trusted_networks(entries: Collection[str]) -> tuple[IPNetwork, ...]:
    """The configured networks, parsed and cached.

    Invalid entries raise, exactly as at startup. That is unreachable in a
    running application - `Settings` refuses to build with one - and is left
    raising rather than swallowed, because an entry that failed to parse must
    never be silently equivalent to an entry that was not there.
    """
    return _cached(tuple(entries))


def is_trusted_peer(peer: str | None, networks: Collection[IPNetwork]) -> bool:
    """Whether the direct peer is a proxy we told the application about.

    Anything that is not a parseable address is not trusted, which covers a
    unix-socket peer and the test transport that reports no client at all.
    """
    if not peer or not networks:
        return False
    try:
        address = ipaddress.ip_address(peer.strip())
    except ValueError:
        return False
    return any(address in network for network in networks)


def normalised_address(value: str | None) -> str | None:
    """One address from a forwarding header, in canonical form, or None.

    Canonical because the alternative is bucket-splitting: an IPv6 address
    written two ways is two rate-limit identities for one client, which is a
    limit that can be walked past by varying the spelling.

    A port suffix is not stripped. `X-Forwarded-For` is documented as carrying
    bare addresses, and guessing at `host:port` would mean guessing at IPv6
    colons; a value that is not an address is simply not one.
    """
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None
