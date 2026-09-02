"""DNS rebinding: validating one address and connecting to another.

`validate_outbound_url` resolved a name and refused private addresses, and then
handed the *name* to httpx, which resolved it again when it opened the socket. A
hostile authoritative server answering with TTL 0 returns a public address to
the first lookup and a private one to the second, and nothing notices.

That was not theoretical. Before `GuardedTransport` existed it was reproduced
against this codebase: the validator allowed the URL, the connection landed on a
loopback service, and its body came back. This file keeps it closed.

The decisive property is **that there is no second resolution to poison**. The
transport resolves once, judges every address, and rewrites the request to an
address literal - which anyio connects to directly rather than looking up. So
the test that matters is not "a rebind is refused" but "the connection layer is
never asked to resolve at all", and that is `test_a_guarded_request_resolves_
exactly_once_and_never_again` below.

Pinning the route must not weaken the identity check, so the `Host` header and
the TLS server name keep the original hostname. A transport that verified a
certificate against the pinned IP would have traded one hole for a larger one.

The last section asks a different question: **which clients are guarded?** The
intended answer is "all of them", and the way that stops being true is one
integration built by hand. Paymob was that integration (SEC-08), so the check is
now an assertion over every outbound client rather than a sentence in a
docstring.
"""

from __future__ import annotations

import contextlib
import socket
from typing import Any

import anyio._core._sockets as anyio_sockets
import httpx
import pytest

from app.core.net import (
    GuardedTransport,
    UnsafeUrlError,
    build_guarded_client,
    resolve_public_host,
    validate_outbound_url,
)
from app.integrations.billing.paymob import PaymobProvider
from app.integrations.openai import client as openai_client
from app.integrations.openai import embeddings
from app.integrations.whatsapp import client as whatsapp_client

HOSTNAME = "rebind.example"
PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"

PRIVATE_ANSWERS = [
    pytest.param("127.0.0.1", id="loopback-v4"),
    pytest.param("10.0.0.5", id="rfc1918"),
    pytest.param("169.254.169.254", id="link-local-metadata"),
    pytest.param("::1", id="loopback-v6"),
    pytest.param("fd00::1", id="unique-local-v6"),
    pytest.param("fe80::1", id="link-local-v6"),
    pytest.param("::ffff:127.0.0.1", id="ipv4-mapped-loopback"),
    pytest.param("100.64.0.1", id="carrier-grade-nat"),
]


class Resolver:
    """A scripted `getaddrinfo`, patched into both resolution paths.

    Both matter and they are different code: `app.core.net` resolves through
    `socket.getaddrinfo`, and anyio - which is what actually opens the socket -
    resolves through its own async wrapper. A test that patched only one would
    prove nothing about the gap between them, which is precisely the gap.
    """

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.validating_calls: list[str] = []
        self.connecting_calls: list[str] = []

    def _next(self) -> str:
        # The last answer repeats, so "public once, private for ever after" is
        # written as two entries rather than a sequence somebody has to count.
        index = min(
            len(self.validating_calls) + len(self.connecting_calls) - 1, len(self._answers) - 1
        )
        return self._answers[max(index, 0)]

    def _entry(self, address: str, port: int) -> list[tuple[Any, ...]]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_socket = socket.getaddrinfo
        real_anyio = anyio_sockets.getaddrinfo

        def validating(host, port, *args: Any, **kwargs: Any):
            if host != HOSTNAME:
                return real_socket(host, port, *args, **kwargs)
            self.validating_calls.append(host)
            return self._entry(self._next(), port or 443)

        async def connecting(host, port, **kwargs):
            name = host.decode() if isinstance(host, bytes) else host
            if name != HOSTNAME:
                return await real_anyio(host, port, **kwargs)
            self.connecting_calls.append(name)
            return self._entry(self._next(), port or 443)

        monkeypatch.setattr(socket, "getaddrinfo", validating)
        monkeypatch.setattr(anyio_sockets, "getaddrinfo", connecting)


# --------------------------------------------------------------- the pin


async def test_a_guarded_request_resolves_exactly_once_and_never_again(monkeypatch):
    """The property that makes rebinding impossible rather than merely refused.

    One resolution happens, in the transport, and it is judged. The connection
    layer is then handed an address literal, so it never asks a resolver
    anything - there is no second answer for a hostile server to give.

    The connection itself fails, because 93.184.216.34 is not listening for this
    test. That is fine and is not what is being asserted: the assertion is on
    who resolved what.
    """
    resolver = Resolver(PUBLIC_V4)
    resolver.install(monkeypatch)

    async with build_guarded_client(timeout=httpx.Timeout(0.2)) as client:
        with pytest.raises(httpx.HTTPError):
            await client.get(f"https://{HOSTNAME}/x")

    assert resolver.validating_calls == [HOSTNAME]
    assert (
        resolver.connecting_calls == []
    ), "the connection layer resolved the name again, so the pin is not holding"


async def test_a_name_that_turns_private_after_validation_is_refused(monkeypatch):
    """The reproduction, kept as a regression.

    Public to the first lookup, loopback to every one after. Before the
    transport existed this reached a loopback service and returned its body.
    """
    resolver = Resolver(PUBLIC_V4, "127.0.0.1")
    resolver.install(monkeypatch)

    # The standalone validator sees the public answer and allows it - which is
    # exactly why the validator alone was not enough.
    assert validate_outbound_url(f"https://{HOSTNAME}/x") == [PUBLIC_V4]

    async with build_guarded_client(timeout=httpx.Timeout(0.2)) as client:
        with pytest.raises(UnsafeUrlError):
            await client.get(f"https://{HOSTNAME}/x")


@pytest.mark.parametrize("address", PRIVATE_ANSWERS)
async def test_a_name_resolving_inward_is_refused_by_the_transport(monkeypatch, address):
    """Every family and every private range, judged at the transport."""
    resolver = Resolver(address)
    resolver.install(monkeypatch)

    async with build_guarded_client(timeout=httpx.Timeout(0.2)) as client:
        with pytest.raises(UnsafeUrlError):
            await client.get(f"https://{HOSTNAME}/x")


async def test_one_private_answer_among_public_ones_refuses_the_lot(monkeypatch):
    """A name with a mixed answer set is refused, not sampled.

    Judging only the address that happens to be picked would let a name with one
    public and one private record through half the time, and which half is the
    attacker's choice.
    """
    real = socket.getaddrinfo

    def mixed(host, port, *args: Any, **kwargs: Any):
        if host != HOSTNAME:
            return real(host, port, *args, **kwargs)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_V4, port or 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port or 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed)

    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(f"https://{HOSTNAME}/x")


# ------------------------------------------------------- identity is kept


async def test_the_pin_keeps_the_hostname_for_routing_and_for_tls(monkeypatch):
    """Connecting to an address must not mean trusting a certificate for it.

    The request that leaves carries the pinned address in the URL, the original
    authority in `Host`, and the original name as the TLS server name - so the
    certificate is still verified against the host the caller asked for.
    """
    resolver = Resolver(PUBLIC_V4)
    resolver.install(monkeypatch)
    seen: list[httpx.Request] = []

    class Capturing(GuardedTransport):
        async def handle_async_request(self, request):
            # Let the guard rewrite, then stop before the socket.
            with contextlib.suppress(httpx.HTTPError):
                await super().handle_async_request(request)
            seen.append(request)
            return httpx.Response(200)

    async with httpx.AsyncClient(transport=Capturing(), timeout=httpx.Timeout(0.2)) as client:
        await client.get(f"https://{HOSTNAME}/x")

    request = seen[0]
    assert request.url.host == PUBLIC_V4
    assert request.headers["Host"] == HOSTNAME
    assert request.extensions["sni_hostname"] == HOSTNAME


async def test_a_non_default_port_survives_in_the_host_header(monkeypatch):
    resolver = Resolver(PUBLIC_V4)
    resolver.install(monkeypatch)
    seen: list[httpx.Request] = []

    class Capturing(GuardedTransport):
        async def handle_async_request(self, request):
            with contextlib.suppress(httpx.HTTPError):
                await super().handle_async_request(request)
            seen.append(request)
            return httpx.Response(200)

    async with httpx.AsyncClient(transport=Capturing(), timeout=httpx.Timeout(0.2)) as client:
        await client.get(f"https://{HOSTNAME}:8443/x")

    assert seen[0].headers["Host"] == f"{HOSTNAME}:8443"


# --------------------------------------------------------- literals and edges


@pytest.mark.parametrize("address", PRIVATE_ANSWERS)
def test_an_address_literal_is_judged_without_being_resolved(address, monkeypatch):
    """A redirect to `https://169.254.169.254/` has no name to look up.

    Handing it to a resolver would be asking a question with an obvious answer,
    and on some platforms the resolver would helpfully normalise a decimal or
    octal spelling on the way. Literals are judged as written.
    """
    called: list[str] = []
    real = socket.getaddrinfo

    def counting(host, port, *args: Any, **kwargs: Any):
        called.append(host)
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    bracketed = f"[{address}]" if ":" in address else address

    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(f"https://{bracketed}/x")

    assert called == [], "a literal address was sent to a resolver"


def test_a_public_address_literal_is_allowed_and_returned():
    """The control. A guard that refused every literal would break a provider
    that redirects to one."""
    assert resolve_public_host(PUBLIC_V4, 443) == [PUBLIC_V4]
    assert resolve_public_host(PUBLIC_V6, 443) == [PUBLIC_V6]


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("2130706433", id="decimal-loopback"),
        pytest.param("0x7f000001", id="hex-loopback"),
        pytest.param("127.1", id="short-form-loopback"),
    ],
)
def test_an_obfuscated_loopback_spelling_does_not_slip_through(spelling, monkeypatch):
    """These are not IP literals to `ipaddress`, so they take the resolver path.

    Whatever the platform resolver makes of them, the *resolved* address is what
    is judged - so a spelling that reaches loopback is refused, and one that
    reaches nothing is refused for not resolving. Either way the answer is no.
    """
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(f"https://{spelling}/x")


async def test_an_ipv6_pin_is_bracketed_in_the_url(monkeypatch):
    """An unbracketed IPv6 authority is a malformed URL, and getting this wrong
    would turn a security control into a connection error nobody understands."""
    resolver = Resolver(PUBLIC_V6)
    resolver.install(monkeypatch)
    seen: list[httpx.Request] = []

    class Capturing(GuardedTransport):
        async def handle_async_request(self, request):
            with contextlib.suppress(httpx.HTTPError):
                await super().handle_async_request(request)
            seen.append(request)
            return httpx.Response(200)

    async with httpx.AsyncClient(transport=Capturing(), timeout=httpx.Timeout(0.2)) as client:
        await client.get(f"https://{HOSTNAME}/x")

    assert seen[0].url.host == PUBLIC_V6
    assert f"[{PUBLIC_V6}]" in str(seen[0].url)


# ------------------------------------------------------------- scheme and hops


@pytest.mark.parametrize("scheme", ["http", "ftp"])
async def test_the_transport_refuses_a_non_https_scheme(scheme):
    """The scheme rule is enforced at the transport too, not only in the
    validator a caller might not have called."""
    async with httpx.AsyncClient(
        transport=GuardedTransport(), timeout=httpx.Timeout(0.2)
    ) as client:
        with pytest.raises(UnsafeUrlError):
            await client.get(f"{scheme}://example.com/x")


async def test_every_hop_of_a_redirect_chain_is_resolved_and_judged(monkeypatch):
    """Redirects are followed by hand by the clients that need them, so each hop
    is a separate request through the transport - and therefore a separate
    resolution, judgement and pin.

    Asserted here at the transport level: three requests, three validations, and
    the private hop refused rather than followed.
    """
    hops = ["hop-one.example", "hop-two.example", "hop-three.example"]
    answers = {hops[0]: PUBLIC_V4, hops[1]: PUBLIC_V4, hops[2]: "10.0.0.9"}
    validated: list[str] = []
    real = socket.getaddrinfo

    def scripted(host, port, *args: Any, **kwargs: Any):
        if host not in answers:
            return real(host, port, *args, **kwargs)
        validated.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", scripted)

    refused = None
    for host in hops:
        try:
            validate_outbound_url(f"https://{host}/x")
        except UnsafeUrlError:
            refused = host
            break

    assert validated == hops
    assert refused == hops[2]


# ------------------------------------------- every integration, not a list


def _integration_clients() -> dict[str, httpx.AsyncClient]:
    """One entry per outbound client this application builds.

    A list is exactly the thing that goes stale, which is why the assertion
    below is not "these four are guarded" but "every client any integration
    hands out is guarded" - and why the Paymob entry constructs the provider
    the way `build_checkout_provider` does, with no transport, rather than
    reaching for an attribute.

    Paymob was the stale entry (SEC-08). Every other integration went through
    `build_guarded_client`; this one built a bare `httpx.AsyncClient`, and it is
    the one request that carries a payment secret key.
    """
    return {
        "openai.responses": openai_client.build_http_client(),
        "openai.embeddings": embeddings.build_http_client(),
        "whatsapp": whatsapp_client.build_http_client(),
        "paymob": PaymobProvider(
            secret_key="sk_test_notreal000000000000",
            public_key="pk_test_notreal000000000000",
            hmac_secret="a-test-hmac-secret",
            integration_ids=[1],
        )._client(),
    }


@pytest.mark.parametrize("name", sorted(_integration_clients()))
async def test_every_integration_client_is_guarded(name):
    """The invariant `openai/client.py` states in prose, asserted.

    "The guard is used here although the URL is a constant, so that the answer
    to which clients are guarded? is all of them rather than a list that goes
    stale." This is the test that keeps that sentence true.
    """
    clients = _integration_clients()
    try:
        transport = clients[name]._transport
        assert isinstance(transport, GuardedTransport), f"{name} is not guarded"
    finally:
        for client in clients.values():
            await client.aclose()


async def test_no_integration_client_follows_redirects_by_itself():
    """`GuardedTransport` judges one hop, so a client must not follow the next.

    A client with `follow_redirects=True` would take the second hop inside httpx,
    below the transport - which is the one place the guard cannot see. The
    clients that genuinely need redirects follow them by hand, one guarded
    request per hop.
    """
    clients = _integration_clients()
    try:
        for name, client in clients.items():
            assert client.follow_redirects is False, f"{name} follows redirects itself"
    finally:
        for client in clients.values():
            await client.aclose()
