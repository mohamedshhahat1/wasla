"""Where a Paymob request is allowed to go.

`test_paymob_http.py` runs the provider against a mock transport, which proves
what it sends and how it reads the answer. It cannot prove anything about the
socket, because there isn't one. This file is about the socket.

The finding (SEC-08): every other integration built its client with
`build_guarded_client`, and this one built a bare `httpx.AsyncClient`. The
exposure was narrow, because Paymob's URLs are constants from `REGIONS` rather
than anything a caller supplies - it takes a hijacked or poisoned resolver for
`accept.paymob.com` to matter. It was also the single outbound request that
carries the Paymob secret key, and a worker sitting inside the deployment
network can reach Redis, PostgreSQL and the cloud metadata endpoint that the
internet cannot.

**No transport is injected in this file.** That is the point: every test below
builds the provider the way `build_checkout_provider` does, with nothing
substituted, and then scripts the *resolver* - which is the layer the guard
actually acts on. A test that handed the provider a mock transport would be
testing the mock.

Two things are deliberately **not** changed and are asserted as such. Redirects
stay off, because `GuardedTransport` judges one hop and a client that followed
the next would follow it below the guard. And no retry is added anywhere: this
client creates payment intentions and charges saved cards, so an automatic
second attempt after a timeout would turn one payment request into two
financial operations. Retryability remains a label on `ProviderError` for a
caller with idempotency to act on.
"""

from __future__ import annotations

import socket
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.core.net import GuardedTransport, UnsafeUrlError
from app.integrations.billing.base import ProviderError
from app.integrations.billing.checkout import CheckoutRequest
from app.integrations.billing.paymob import REGIONS, PaymobProvider

SECRET_KEY = "sk_test_notreal000000000000"
PUBLIC_KEY = "pk_test_notreal000000000000"
HMAC_SECRET = "a-test-hmac-secret"

PUBLIC_V4 = "93.184.216.34"

# The ranges the shared guard refuses, named individually so a change to
# `_is_public` that quietly narrowed one would fail here rather than pass.
PRIVATE_ANSWERS = [
    pytest.param("127.0.0.1", id="loopback-v4"),
    pytest.param("10.0.0.5", id="rfc1918-10"),
    pytest.param("172.16.4.4", id="rfc1918-172"),
    pytest.param("192.168.1.9", id="rfc1918-192"),
    pytest.param("169.254.169.254", id="cloud-metadata"),
    pytest.param("100.64.0.1", id="carrier-grade-nat"),
    pytest.param("::1", id="loopback-v6"),
    pytest.param("fd00::1", id="unique-local-v6"),
    pytest.param("fe80::1", id="link-local-v6"),
    pytest.param("::ffff:127.0.0.1", id="ipv4-mapped-loopback"),
]


def _provider(**overrides: Any) -> PaymobProvider:
    """The provider as a deployment builds it: no transport, real client."""
    settings: dict[str, Any] = {
        "secret_key": SECRET_KEY,
        "public_key": PUBLIC_KEY,
        "hmac_secret": HMAC_SECRET,
        "integration_ids": [4242],
        "timeout_seconds": 0.5,
    }
    settings.update(overrides)
    return PaymobProvider(**settings)


def _request() -> CheckoutRequest:
    return CheckoutRequest(
        reference="wasla-invoice-1",
        amount=Decimal("499.00"),
        currency="EGP",
        description="Pro plan",
        customer_email="person@example.com",
        customer_name="A Person",
    )


class _Resolver:
    """A scripted `getaddrinfo` for one host, leaving every other name alone.

    Patched at `socket.getaddrinfo`, which is where `app.core.net` resolves. The
    guard rewrites the request to the address it judged, so nothing downstream
    ever asks a resolver again - that is the property `test_outbound_pinning.py`
    pins, and it is why scripting this one function is enough.
    """

    def __init__(self, host: str, *answers: str) -> None:
        self.host = host
        self._answers = list(answers)
        self.calls: list[str] = []

    def _next(self) -> str:
        index = min(len(self.calls) - 1, len(self._answers) - 1)
        return self._answers[max(index, 0)]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = socket.getaddrinfo

        def scripted(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
            if host != self.host:
                return real(host, port, *args, **kwargs)
            self.calls.append(host)
            address = self._next()
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (
                (address, port or 443, 0, 0)
                if family == socket.AF_INET6
                else (address, port or 443)
            )
            return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

        monkeypatch.setattr(socket, "getaddrinfo", scripted)


def _host_of(region: str) -> str:
    return httpx.URL(REGIONS[region][0]).host


# --- the client itself -------------------------------------------------------


async def test_the_provider_builds_the_shared_guarded_client() -> None:
    """One shared HTTP security policy, not a second one written here.

    The guard is `build_guarded_client`'s transport - the same object OpenAI,
    WhatsApp and Google get - rather than DNS and address checks duplicated
    inside this module. A duplicate would be a second policy to keep in step.
    """
    async with _provider()._client() as client:
        assert isinstance(client._transport, GuardedTransport)


async def test_the_client_does_not_follow_redirects_by_itself() -> None:
    """The guard judges one hop.

    Paymob's API endpoints are JSON POSTs and do not redirect. If that ever
    changed, the correct answer is a hand-written loop of guarded requests, not
    `follow_redirects=True` - which would take the next hop inside httpx, below
    the transport, unresolved and unjudged.
    """
    async with _provider()._client() as client:
        assert client.follow_redirects is False


async def test_the_timeout_covers_every_phase_of_the_request() -> None:
    """A provider that accepts a connection and then stalls is the failure that
    matters, so the read timeout has to be set and not only the connect one."""
    async with _provider(timeout_seconds=7.5)._client() as client:
        timeout = client.timeout
        assert timeout.connect == 7.5
        assert timeout.read == 7.5
        assert timeout.write == 7.5
        assert timeout.pool == 7.5


@pytest.mark.parametrize("region", sorted(REGIONS))
async def test_every_region_gets_the_same_guarded_client(region: str) -> None:
    """Regional hosts are configuration, and the guard is not per-host.

    `PAYMOB_REGION` selects between four documented API hosts. None of them is
    special-cased or allow-listed: the guard judges the address each one
    resolves to, at the time of the request, which is what an allow-list of
    names cannot do.
    """
    async with _provider(region=region)._client() as client:
        assert isinstance(client._transport, GuardedTransport)


# --- where the request may go ------------------------------------------------


@pytest.mark.parametrize("address", PRIVATE_ANSWERS)
async def test_a_paymob_host_resolving_inward_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    """The inherited protection, exercised through the real provider.

    Paymob's own host, answered by a hostile or misconfigured resolver with an
    address inside the deployment network. Before this change the request was
    made; now the transport refuses it before a socket is opened.
    """
    host = _host_of("egypt")
    _Resolver(host, address).install(monkeypatch)

    with pytest.raises(ProviderError) as refused:
        await _provider().create_checkout(_request())

    # It leaves as the domain error every other failure to reach Paymob leaves
    # as, so neither `UnsafeUrlError` - which is not a `WaslaError` and would
    # therefore have become a 500 - nor a raw httpx exception escapes to a
    # route.
    assert isinstance(refused.value, ProviderError)
    assert isinstance(refused.value.__cause__, UnsafeUrlError)
    # And not retryable, unlike a timeout: nothing was sent, and pointing the
    # same request at the same refused destination produces the same answer.
    # A caller looping on this would be looping on its own configuration.
    assert refused.value.retryable is False


async def test_a_host_that_turns_private_after_validation_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS rebinding, inherited.

    Public to the first lookup and loopback to every one after. The guard
    resolves once and connects to the address it judged, so there is no second
    answer to poison - which is why this is refused rather than merely
    detected.
    """
    host = _host_of("egypt")
    _Resolver(host, PUBLIC_V4, "127.0.0.1").install(monkeypatch)

    with pytest.raises(ProviderError):
        await _provider().create_checkout(_request())


async def test_a_refusal_names_no_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is the thing being probed for, so it must not describe itself.

    A message naming the address that was refused would turn the error into the
    oracle the refusal exists to prevent - the same reasoning `app/core/net.py`
    gives for keeping the address out of its own log line.
    """
    host = _host_of("egypt")
    _Resolver(host, "169.254.169.254").install(monkeypatch)

    with pytest.raises(ProviderError) as refused:
        await _provider().create_checkout(_request())

    message = str(refused.value)
    assert "169.254.169.254" not in message
    assert host not in message
    assert SECRET_KEY not in message


async def test_a_misconfigured_plaintext_base_url_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheme is enforced at the transport, not only where a URL is built.

    `REGIONS` holds https constants, so this is reachable only by editing them -
    and that is exactly the change that should fail loudly rather than start
    sending a secret key over plaintext.
    """
    monkeypatch.setitem(REGIONS, "egypt", ("http://accept.paymob.com", REGIONS["egypt"][1]))

    with pytest.raises(ProviderError):
        await _provider().create_checkout(_request())


# --- what must not have changed ----------------------------------------------


async def test_no_retry_was_added_along_with_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guarded client and a retrying client are different concerns.

    `_post` creates payment intentions and charges saved cards. One request must
    remain one request: an automatic second attempt after a timeout would be a
    second financial operation, and the caller - which holds the idempotency
    key - is the only layer that can decide whether repeating is safe.
    """
    attempts: list[httpx.Request] = []

    def counting(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectTimeout("no answer", request=request)

    provider = _provider(transport=httpx.MockTransport(counting))

    with pytest.raises(ProviderError) as failure:
        await provider.create_checkout(_request())

    assert len(attempts) == 1
    # Retryable is a *label* for a caller to act on, not something acted on here.
    assert failure.value.retryable is True


async def test_a_timeout_still_becomes_the_provider_error_it_always_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No raw httpx exception escapes to a route, guarded or not."""

    def stalling(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("stalled", request=request)

    provider = _provider(transport=httpx.MockTransport(stalling))

    with pytest.raises(ProviderError) as failure:
        await provider.create_checkout(_request())

    assert failure.value.retryable is True
    assert "Paymob" in str(failure.value)


async def test_a_normal_request_still_works_through_the_guarded_construction() -> None:
    """The control. Every refusal above has to be the guard firing, not the
    provider being broken."""
    captured: list[httpx.Request] = []

    def answering(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            201,
            json={"client_secret": "egy_csk_test_0123456789abcdef", "id": 99},
        )

    provider = _provider(transport=httpx.MockTransport(answering))

    session = await provider.create_checkout(_request())

    assert session.redirect_url.startswith(REGIONS["egypt"][1])
    assert captured[0].url.host == _host_of("egypt")
    assert captured[0].headers["Authorization"] == f"Token {SECRET_KEY}"


async def test_a_redirect_toward_a_private_host_is_not_followed() -> None:
    """The escape a redirect would be, closed by not following one at all.

    `GuardedTransport` judges the request it is given. If the client followed
    redirects itself, httpx would take the second hop internally - below the
    transport, with no resolution and no judgement - and a public Paymob URL
    answering `302 Location: http://169.254.169.254/` would become a request to
    the metadata endpoint.

    Asserted as a count rather than as a refusal: exactly one request leaves,
    and the private location is never asked for. A test that only checked "an
    error was raised" would pass against a client that followed the hop and
    failed for some other reason.
    """
    seen: list[str] = []

    def redirecting(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    provider = _provider(transport=httpx.MockTransport(redirecting))

    with pytest.raises(ProviderError):
        await provider.create_checkout(_request())

    assert len(seen) == 1
    assert "169.254.169.254" not in seen[0]
