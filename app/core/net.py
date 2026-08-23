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

**Every hop is checked, and the connection is pinned to the address that was
checked.** Validating a name and then handing the name to the HTTP client
validates nothing: the client resolves it again when it opens the socket, and a
hostile authoritative server answering with TTL 0 can return a public address to
the first lookup and a private one to the second. That is not theoretical - it
was reproduced against this module, which allowed the URL and then read the body
of a service on loopback. `GuardedTransport` closes it by resolving once,
judging every address, and connecting to a literal address rather than to a name
that can change its mind.

The name is not thrown away when the address is pinned. The `Host` header keeps
the original authority and TLS keeps the original server name, so certificate
verification still binds to the hostname the caller asked for - pinning the
route must not weaken the identity check.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Final
from urllib.parse import urlsplit

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

ALLOWED_SCHEMES: Final = frozenset({"https"})
# Enough to satisfy a provider that redirects to a CDN, and few enough that a
# redirect loop cannot be used to spend a worker.
MAX_REDIRECTS: Final = 3
# Used when a URL names no port, both for resolution and for deciding
# whether the `Host` header needs one.
DEFAULT_HTTPS_PORT: Final = 443


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


def _judge(host: str, addresses: set[str]) -> None:
    """Refuse unless every address this name answers with is publicly routable.

    Every address, not the first: a name with one public and one private record
    would otherwise pass half the time, and which half is the attacker's choice.
    """
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


def resolve_public_host(host: str, port: int) -> list[str]:
    """Resolve a host once and return its addresses, or refuse.

    One resolution, and the caller connects to what comes back. Resolving again
    at connect time is the whole vulnerability, so this returns addresses rather
    than a verdict: a function that answered yes/no would leave the caller
    holding the name.

    An address literal is judged directly and never resolved. That matters for
    correctness as much as speed - a redirect to `https://169.254.169.254/` has
    no name to look up, and passing it to a resolver would be asking a question
    with an obvious answer.
    """
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        parsed = None

    if parsed is not None:
        # A literal, including the decimal and octal spellings a resolver would
        # otherwise normalise for us. Judged as written.
        _judge(host, {str(parsed)})
        return [host]

    try:
        resolved = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as error:
        raise UnsafeUrlError("the host could not be resolved") from error

    addresses = {str(entry[4][0]) for entry in resolved}
    _judge(host, addresses)
    # Ordered so a caller picking the first gets a stable choice, and so IPv4
    # and IPv6 answers stay distinguishable in a log or a test.
    return sorted(addresses)


def validate_outbound_url(url: str) -> list[str]:
    """Raise `UnsafeUrlError` unless this URL is safe to fetch.

    Returns the validated addresses, so a caller that wants to connect to one
    of them can. Callers that only want the verdict may ignore the return
    value; `GuardedTransport` is the one that uses it.

    Resolution is done here rather than trusting the hostname, because a name is
    not a destination: `metadata.example.com` may be an A record for
    169.254.169.254, and a host allow-list alone would wave it through.
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"scheme {parts.scheme!r} is not permitted")

    host = parts.hostname
    if not host:
        raise UnsafeUrlError("the URL names no host")

    return resolve_public_host(host, parts.port or DEFAULT_HTTPS_PORT)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """An httpx transport that resolves once and connects to what it validated.

    **This is the enforcement point, not a convenience.** A caller can forget to
    validate a URL; it cannot forget to use its own transport. Every outbound
    client this application builds is constructed with one, so the guarantee is
    a property of the client rather than of remembering.

    How the pin works. The request URL's host is replaced with a validated
    address literal, which anyio recognises as an address and connects to
    directly instead of resolving. The `Host` header is set to the original
    authority so the server routes correctly, and `sni_hostname` is set to the
    original host so TLS presents the right name *and* verifies the certificate
    against it. Pinning the route must not weaken the identity check - a
    transport that connected to an IP and verified the certificate against that
    IP would trade one hole for a larger one.

    Redirects are not followed here. The clients that need them follow by hand
    so each hop is a separate request through this transport, and therefore a
    separate resolution, judgement and pin.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host:
            raise UnsafeUrlError("the URL names no host")
        if request.url.scheme not in ALLOWED_SCHEMES:
            raise UnsafeUrlError(f"scheme {request.url.scheme!r} is not permitted")

        port = request.url.port or DEFAULT_HTTPS_PORT
        addresses = resolve_public_host(host, port)
        pinned = addresses[0]

        if pinned == host:
            # Already a literal, already judged. Nothing to rewrite, and
            # rewriting would only risk mangling an IPv6 authority.
            return await super().handle_async_request(request)

        # `httpx.URL.copy_with(host=...)` brackets an IPv6 literal for us.
        request.url = request.url.copy_with(host=pinned)
        request.headers["Host"] = _authority(host, request.url.port)
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await super().handle_async_request(request)


def _authority(host: str, port: int | None) -> str:
    """The `Host` header value for the name that was asked for.

    The port is included only when it is not the default, matching what any
    client would have sent - a server comparing `Host` against a virtual host
    configuration should not see a difference because we pinned the route.
    """
    bracketed = f"[{host}]" if ":" in host else host
    if port is None or port == DEFAULT_HTTPS_PORT:
        return bracketed
    return f"{bracketed}:{port}"


def build_guarded_client(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
    """An `AsyncClient` that cannot be aimed at the deployment network.

    The single constructor every integration uses, so "which clients are
    guarded?" has one answer.
    """
    return httpx.AsyncClient(timeout=timeout, transport=GuardedTransport())
