"""Outbound URL validation.

Every URL this application fetches that it did not construct itself passes
through here. Today that is one caller - the WhatsApp media download, whose URL
arrives in a provider response rather than being built from configuration - and
that is exactly the shape server-side request forgery takes: a fetch whose
destination somebody else chose.

The threat is not "an attacker steals our Meta token". httpx strips
`Authorization` when a redirect leaves the origin, so a redirect to an attacker
host receives no credential. It is that **the request happens at all**. A worker
sitting inside the deployment network can reach things the internet cannot:
`169.254.169.254` for cloud instance credentials, `127.0.0.1:6379` for the Redis
that holds the refresh-token denylist and the agent queue, PostgreSQL, and any
neighbouring service. The response body is then stored as media and can be read
back through the API, which turns a blind request into a read primitive.

Two rules, and both are needed:

**The scheme must be https.** Plaintext to a provider is worth refusing on its
own, and it also removes `file://`, `ftp://` and the rest as a class rather than
by enumeration.

**Every hop is checked, not just the first.** Validating the URL a provider
hands us and then following redirects blindly validates nothing - the redirect
is the attack. Resolution happens per hop and the address, not the name, is what
is judged.

This does not defeat DNS rebinding: a name that resolves to a public address
here and a private one when the socket is opened would pass. Closing that needs
the connection pinned to the address that was checked, which means a custom
transport. It is recorded rather than implemented because the caller is a
provider URL rather than a user-supplied one, and the cost is not proportionate
yet - see `docs/SECURITY.md`.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Final
from urllib.parse import urlsplit

from app.core.logging import get_logger

logger = get_logger(__name__)

ALLOWED_SCHEMES: Final = frozenset({"https"})
# Enough to satisfy a provider that redirects to a CDN, and few enough that a
# redirect loop cannot be used to spend a worker.
MAX_REDIRECTS: Final = 3


class UnsafeUrlError(Exception):
    """A URL that must not be fetched.

    Deliberately not a `WaslaError`: this is never something to explain to an
    HTTP caller. It is caught by the integration that raised it and reported as
    a failure to fetch, so a probe learns nothing about what was refused or why.
    """


def _is_public(address: str) -> bool:
    """Whether an address is one the public internet could route to.

    `is_global` covers loopback, link-local (and therefore the cloud metadata
    endpoint at 169.254.169.254), the RFC 1918 ranges, carrier-grade NAT,
    multicast and reserved space in one property, for IPv4 and IPv6 alike -
    including the IPv4-mapped IPv6 forms that a hand-written range list
    reliably forgets.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_global


def validate_outbound_url(url: str) -> None:
    """Raise `UnsafeUrlError` unless this URL is safe to fetch.

    Resolution is done here rather than trusting the hostname, because a name is
    not a destination: `metadata.example.com` may be an A record for
    169.254.169.254, and a host allow-list alone would wave it through. Every
    address the name resolves to must be public - checking only the first would
    let a name with one public and one private record through half the time.
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"scheme {parts.scheme!r} is not permitted")

    host = parts.hostname
    if not host:
        raise UnsafeUrlError("the URL names no host")

    try:
        resolved = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as error:
        raise UnsafeUrlError("the host could not be resolved") from error

    addresses = {str(entry[4][0]) for entry in resolved}
    if not addresses:
        raise UnsafeUrlError("the host resolved to nothing")

    for address in addresses:
        if not _is_public(address):
            # The address is not logged: it is the thing being probed for, and a
            # log line naming it turns the log into the oracle the refusal
            # exists to prevent.
            logger.warning(
                "net.unsafe_url_refused",
                extra={"event": "net.unsafe_url_refused", "host": host},
            )
            raise UnsafeUrlError("the host resolves to a non-public address")
