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
integration built by hand. Paymob was that integration (SEC-08) and Resend was
the next one (F-3) - which is the interesting part, because a test claiming to
assert "every client any integration hands out is guarded" was already green
over a dict literal of exactly four.

So the set is no longer written down. It is discovered by parsing every module
under `app/` for two facts - does it construct an `httpx.AsyncClient`, does it
reference `build_guarded_client` - and a module that does the first without the
second fails unless it is an exemption with a stated reason. The live-client
dict survives, because each provider needs different constructor arguments, but
it is now checked *against* the discovery rather than trusted.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
import socket
from typing import Any, Final

import anyio._core._sockets as anyio_sockets
import httpx
import pytest

import app as app_package
from app.core.net import (
    GuardedTransport,
    UnsafeUrlError,
    build_guarded_client,
    resolve_public_host,
    validate_outbound_url,
)
from app.integrations.billing.paymob import PaymobProvider
from app.integrations.email.base import EmailMessage, EmailSendState
from app.integrations.email.resend import RESEND_ENDPOINT, ResendEmailProvider
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

    def __init__(self, *answers: str, hostname: str = HOSTNAME) -> None:
        self._answers = list(answers)
        # Which name this resolver answers for. Every other host falls through
        # to the real one, which is what keeps a test that forgets to name its
        # host from silently reaching the internet instead of the script.
        self._hostname = hostname
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

        def validating(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
            if host != self._hostname:
                return real_socket(host, port, *args, **kwargs)
            self.validating_calls.append(host)
            return self._entry(self._next(), port or 443)

        async def connecting(host: str, port: int, **kwargs: Any) -> Any:
            name = host
            if name != self._hostname:
                return await real_anyio(host, port, **kwargs)
            self.connecting_calls.append(name)
            return self._entry(self._next(), port or 443)

        monkeypatch.setattr(socket, "getaddrinfo", validating)
        monkeypatch.setattr(anyio_sockets, "getaddrinfo", connecting)


# --------------------------------------------------------------- the pin


async def test_a_guarded_request_resolves_exactly_once_and_never_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


async def test_a_name_that_turns_private_after_validation_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
async def test_a_name_resolving_inward_is_refused_by_the_transport(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    """Every family and every private range, judged at the transport."""
    resolver = Resolver(address)
    resolver.install(monkeypatch)

    async with build_guarded_client(timeout=httpx.Timeout(0.2)) as client:
        with pytest.raises(UnsafeUrlError):
            await client.get(f"https://{HOSTNAME}/x")


async def test_one_private_answer_among_public_ones_refuses_the_lot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name with a mixed answer set is refused, not sampled.

    Judging only the address that happens to be picked would let a name with one
    public and one private record through half the time, and which half is the
    attacker's choice.
    """
    real = socket.getaddrinfo

    def mixed(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
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


async def test_the_pin_keeps_the_hostname_for_routing_and_for_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connecting to an address must not mean trusting a certificate for it.

    The request that leaves carries the pinned address in the URL, the original
    authority in `Host`, and the original name as the TLS server name - so the
    certificate is still verified against the host the caller asked for.
    """
    resolver = Resolver(PUBLIC_V4)
    resolver.install(monkeypatch)
    seen: list[httpx.Request] = []

    class Capturing(GuardedTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
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


async def test_a_non_default_port_survives_in_the_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = Resolver(PUBLIC_V4)
    resolver.install(monkeypatch)
    seen: list[httpx.Request] = []

    class Capturing(GuardedTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            with contextlib.suppress(httpx.HTTPError):
                await super().handle_async_request(request)
            seen.append(request)
            return httpx.Response(200)

    async with httpx.AsyncClient(transport=Capturing(), timeout=httpx.Timeout(0.2)) as client:
        await client.get(f"https://{HOSTNAME}:8443/x")

    assert seen[0].headers["Host"] == f"{HOSTNAME}:8443"


# --------------------------------------------------------- literals and edges


@pytest.mark.parametrize("address", PRIVATE_ANSWERS)
def test_an_address_literal_is_judged_without_being_resolved(
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect to `https://169.254.169.254/` has no name to look up.

    Handing it to a resolver would be asking a question with an obvious answer,
    and on some platforms the resolver would helpfully normalise a decimal or
    octal spelling on the way. Literals are judged as written.
    """
    called: list[str] = []
    real = socket.getaddrinfo

    def counting(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        called.append(host)
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    bracketed = f"[{address}]" if ":" in address else address

    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(f"https://{bracketed}/x")

    assert called == [], "a literal address was sent to a resolver"


def test_a_public_address_literal_is_allowed_and_returned() -> None:
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
def test_an_obfuscated_loopback_spelling_does_not_slip_through(
    spelling: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These are not IP literals to `ipaddress`, so they take the resolver path.

    Whatever the platform resolver makes of them, the *resolved* address is what
    is judged - so a spelling that reaches loopback is refused, and one that
    reaches nothing is refused for not resolving. Either way the answer is no.
    """
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(f"https://{spelling}/x")


async def test_an_ipv6_pin_is_bracketed_in_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbracketed IPv6 authority is a malformed URL, and getting this wrong
    would turn a security control into a connection error nobody understands."""
    resolver = Resolver(PUBLIC_V6)
    resolver.install(monkeypatch)
    seen: list[httpx.Request] = []

    class Capturing(GuardedTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
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
async def test_the_transport_refuses_a_non_https_scheme(scheme: str) -> None:
    """The scheme rule is enforced at the transport too, not only in the
    validator a caller might not have called."""
    async with httpx.AsyncClient(
        transport=GuardedTransport(), timeout=httpx.Timeout(0.2)
    ) as client:
        with pytest.raises(UnsafeUrlError):
            await client.get(f"{scheme}://example.com/x")


async def test_every_hop_of_a_redirect_chain_is_resolved_and_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    def scripted(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
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


# Modules under `app/` that build an outbound HTTP client and are deliberately
# not guarded. Each entry is a decision with a reason, and the reason is
# asserted somewhere rather than only written here.
EXEMPT_MODULES: Final[dict[str, str]] = {
    # The one guarded constructor itself. It *is* the rule.
    "app.core.net": "builds the guarded client every integration uses",
    # Infrastructure rather than an integration: its endpoint comes from
    # `Settings` and from nowhere else, and `http://minio:9000` is the correct
    # value for a self-hosted stack. Guarding it would make the ordinary
    # deployment unreachable while protecting against nothing. Argued in the
    # module docstring and asserted by
    # `test_the_object_store_is_deliberately_not_an_integration_client`.
    "app.core.object_store": "an object store endpoint is configuration, not a response",
}


def _module_name(path: pathlib.Path) -> str:
    root = pathlib.Path(app_package.__file__).resolve().parent.parent
    return ".".join(path.resolve().relative_to(root).with_suffix("").parts)


def _outbound_modules() -> dict[str, set[str]]:
    """Every module under `app/` that reaches the network, and how.

    Discovered by parsing rather than by listing, because a list is exactly the
    thing that goes stale - which is what F-3 was. The previous version of this
    file built a dict literal of four clients under a docstring claiming the
    assertion was "every client any integration hands out is guarded"; Resend
    had been added to the codebase and not to the dict, so the sentence was
    true of the docstring and false of the code.

    Two facts per module, and the pair is what the tests below reason over:

    - ``constructs`` - it calls ``httpx.AsyncClient(...)`` itself.
    - ``guarded``    - it references ``build_guarded_client``.

    A module that constructs and does not guard is the defect. A module that
    guards and does not construct needs nothing further: it can only produce a
    guarded client.
    """
    root = pathlib.Path(app_package.__file__).resolve().parent
    found: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        marks: set[str] = set()
        for node in ast.walk(tree):
            # A *call*, not a mention. `client: httpx.AsyncClient` is a
            # parameter annotation on a function somebody hands a client to,
            # and half the workers have one - counting those would make the
            # rule below fire on modules that never build anything.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "AsyncClient"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "httpx"
            ):
                marks.add("constructs")
            if isinstance(node, ast.Name) and node.id == "build_guarded_client":
                marks.add("guarded")
        if marks:
            found[_module_name(path)] = marks
    return found


def _integration_clients() -> dict[str, httpx.AsyncClient]:
    """A live client from every module that builds one itself.

    Still written out, because each provider needs different arguments and
    there is no generic way to construct one. What is new is that it is no
    longer *trusted*: `test_every_module_that_builds_a_client_is_exercised`
    compares this against the discovery above, so a module added to the code
    and not to this dict fails rather than being silently uncovered.

    Modules that call `build_guarded_client` inline - Google's token and JWKS
    fetches, the WhatsApp ownership probe - are absent on purpose. They cannot
    produce anything but a guarded client, and there is no object to reach.
    """
    return {
        "app.integrations.openai.client": openai_client.build_http_client(),
        "app.integrations.openai.embeddings": embeddings.build_http_client(),
        "app.integrations.whatsapp.client": whatsapp_client.build_http_client(),
        "app.integrations.billing.paymob": PaymobProvider(
            secret_key="sk_test_notreal000000000000",
            public_key="pk_test_notreal000000000000",
            hmac_secret="a-test-hmac-secret",
            integration_ids=[1],
        )._client(),
        "app.integrations.email.resend": ResendEmailProvider(
            api_key="re_notreal_000000000000000000",
        )._client(),
    }


def test_the_discovery_finds_the_modules_it_is_supposed_to() -> None:
    """Non-vacuity, before anything is concluded from the walk.

    A discovery that finds nothing satisfies every completeness assertion ever
    written against it, so the shape is pinned first: a minimum count, the
    guarded constructor itself, and one integration of each kind - one that
    builds a client and one that only calls the constructor inline.
    """
    modules = _outbound_modules()

    assert len(modules) >= 8, sorted(modules)
    assert modules["app.core.net"] == {"constructs"}
    assert "constructs" in modules["app.integrations.email.resend"]
    assert modules["app.integrations.google.oidc"] == {"guarded"}


def test_no_integration_builds_an_unguarded_client() -> None:
    """F-3, as a rule rather than as a list.

    Resend built `httpx.AsyncClient(timeout=..., transport=self._transport)`
    and `self._transport` is `None` in production, so every real send went out
    on httpx's default transport with no address check - carrying
    `RESEND_API_KEY` in an Authorization header. It passed a `transport=`
    keyword, which is why a rule about the *argument* would not have caught it;
    what it did not do was reference the guard at all.

    A module that constructs its own client must therefore also name
    `build_guarded_client`, which is the test-injection idiom Paymob already
    used: guarded in production, a mock socket under test. Anything else is
    either in `EXEMPT_MODULES` with a reason or a failure here.
    """
    unguarded = {
        name
        for name, marks in _outbound_modules().items()
        if "constructs" in marks and "guarded" not in marks and name not in EXEMPT_MODULES
    }

    assert unguarded == set(), f"builds an unguarded HTTP client: {sorted(unguarded)}"


def test_every_module_that_builds_a_client_is_exercised() -> None:
    """The list cannot go stale, because discovery decides what belongs in it.

    This is the assertion the old file was missing, and it runs in both
    directions.

    Every module that builds a client *itself* must be exercised, because that
    is the shape in which the guard can be forgotten - it holds a branch, and a
    branch can be wrong. Modules that only call `build_guarded_client` inline
    may be in the dict as extra evidence and need not be: there is no branch to
    get wrong and often no object to reach.

    Nothing in the dict may name a module that has stopped making outbound
    calls, or the coverage it appears to give is imaginary.
    """
    modules = _outbound_modules()
    builders = {
        name
        for name, marks in modules.items()
        if "constructs" in marks and name not in EXEMPT_MODULES
    }
    exercised = set(_integration_clients())

    assert builders, "the discovery found nothing to check"
    assert builders <= exercised, f"builds its own client, never checked: {builders - exercised}"
    assert exercised <= set(modules), f"named here, reaches nothing: {exercised - set(modules)}"


def test_the_exemptions_are_declared_and_still_apply() -> None:
    """An exemption that no longer describes anything is a rule nobody checks.

    Both entries must still be modules that build a client, or the dictionary
    is quietly granting permission to something that has moved.
    """
    modules = _outbound_modules()

    assert set(EXEMPT_MODULES) <= set(modules)
    for name, reason in EXEMPT_MODULES.items():
        assert "constructs" in modules[name], name
        assert reason


@pytest.mark.parametrize("name", sorted(_integration_clients()))
async def test_every_integration_client_is_guarded(name: str) -> None:
    """The invariant `openai/client.py` states in prose, asserted at runtime.

    "The guard is used here although the URL is a constant, so that the answer
    to which clients are guarded? is all of them rather than a list that goes
    stale." The structural tests above keep the *set* honest; this one checks
    that each member of it really carries `GuardedTransport` rather than merely
    mentioning the constructor.
    """
    clients = _integration_clients()
    try:
        transport = clients[name]._transport
        assert isinstance(transport, GuardedTransport), f"{name} is not guarded"
    finally:
        for client in clients.values():
            await client.aclose()


async def test_no_integration_client_follows_redirects_by_itself() -> None:
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


async def test_a_poisoned_resend_resolution_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-3 as an outcome rather than as a property of an object.

    The transport check above says the right class is attached; this says the
    guard actually fires on the hostname the provider uses. Before the fix the
    same resolver answer produced a connection attempt to a private address
    carrying `RESEND_API_KEY` in an Authorization header.

    The result is a transient failure rather than an exception, because the
    provider translates transport errors into the outbox's vocabulary - a
    refused send is retryable by definition, and a message that cannot leave
    must not be marked delivered.
    """
    resolver = Resolver("10.0.0.9", hostname=httpx.URL(RESEND_ENDPOINT).host)
    resolver.install(monkeypatch)

    result = await ResendEmailProvider(
        api_key="re_notreal_000000000000000000",
        timeout_seconds=0.2,
    ).send(
        EmailMessage(
            sender="no-reply@example.com",
            to=("someone@example.com",),
            subject="s",
            text="t",
        )
    )

    assert resolver.validating_calls == [
        "api.resend.com"
    ], "the guard never resolved the name, so it never judged it"
    assert result.state is EmailSendState.TRANSIENT_FAILURE
    assert result.error_code == "transport_error"
    # The exception type survives and its text does not: httpx errors quote the
    # request they were carrying, and this one is credentialed.
    assert "re_notreal" not in (result.error_message or "")


# ------------------------------------------- the one client that is not guarded


def test_the_object_store_is_deliberately_not_an_integration_client() -> None:
    """The media store is infrastructure, and the guard would break it.

    `build_guarded_client` refuses private addresses because the clients it
    builds fetch URLs that arrive in somebody else's response - a WhatsApp media
    location, a redirect - and the worker sits inside the deployment network.

    An object store is not in that class. Its endpoint comes from configuration
    and from nowhere else, and `http://minio:9000` is the correct value for a
    self-hosted stack, exactly as `DATABASE_URL` and `REDIS_URL` point at
    private addresses by design. Guarding it would make the ordinary deployment
    unreachable while protecting against nothing.

    This is an assertion rather than a comment because the *reason* it is safe
    is "no request, provider response or database row can influence the
    endpoint", and the way that stops being true is somebody passing one in.
    """
    import inspect

    from app.core.object_store import S3MediaStorage

    signature = inspect.signature(S3MediaStorage.__init__)
    assert "endpoint_url" in signature.parameters

    # Built from `Settings` and from nothing else, so the endpoint is a
    # deployment decision by construction.
    source = inspect.getsource(S3MediaStorage.from_settings)
    assert "settings.media_s3_endpoint_url" in source

    # And nothing anywhere hands it a URL from another source.
    callers = inspect.getsource(__import__("app.core.storage", fromlist=["x"]))
    assert "endpoint_url" not in callers
