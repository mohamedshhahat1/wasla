"""The boundary of an inbound media download: which hosts get the token, and how
much any of them can make this process read (MEDIA-01, MEDIA-09).

Every request here goes to a fake transport, and every name is answered by a
fixed DNS map, so the real `validate_outbound_url` judges real addresses and the
only variable is the client's own policy. The token is a sentinel: a test
asserting it never reached a host is asserting about a string that cannot occur
anywhere else.

Two controls are held apart on purpose, because they are different controls.
The SSRF check decides whether an address is safe to reach, and a public
attacker host passes it. The host policy decides whether a host is Meta's, and
an allow-listed name resolving somewhere private passes *that*. Each has a test
that only it can fail, so neither can quietly stand in for the other.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from app.core.exceptions import ExternalServiceError, RateLimitedError
from app.core.logging import JsonFormatter
from app.integrations.whatsapp.client import (
    MAX_DESCRIPTOR_BYTES,
    MAX_ERROR_BODY_BYTES,
    MalformedMediaDescriptorError,
    MediaCredentialRefusedError,
    MediaHostRefusedError,
    MediaUnavailableError,
    WhatsAppClient,
)

TOKEN = "SENTINEL-META-TOKEN-7f3a91c2"
# A signed CDN query of the shape Meta hands out. It must never be logged.
SIGNATURE = "oh=SENTINEL-CDN-SIGNATURE-44b1&oe=6700FFFF"
MEDIA_ID = "media-1"
GRAPH = "graph.facebook.com"
LOOKASIDE = "lookaside.fbsbx.com"
SCONTENT = "scontent.xx.fbcdn.net"
ATTACKER = "cdn.attacker-controlled.example"
JPEG = b"\xff\xd8\xff\xe0" + b"J" * 60
CAP = 1_000_000

FAKE_DNS = {
    GRAPH: "157.240.1.35",
    LOOKASIDE: "157.240.1.36",
    SCONTENT: "157.240.1.37",
    ATTACKER: "1.1.1.1",
    "evilfbcdn.net": "1.0.0.1",
    "fbcdn.net.attacker.example": "1.0.0.2",
    # Allow-listed names that resolve somewhere they must never be fetched
    # from. Only the SSRF check can refuse these.
    "internal.fbcdn.net": "10.0.0.5",
    "metadata.fbsbx.com": "169.254.169.254",
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.getaddrinfo

    def answer(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if host in FAKE_DNS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (FAKE_DNS[host], port or 443))]
        if isinstance(host, str) and host.endswith(".example"):
            raise socket.gaierror("no such host")
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", answer)


class Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]

    def hosts_given_token(self) -> list[str]:
        return [
            request.url.host
            for request in self.requests
            if request.headers.get("Authorization") == f"Bearer {TOKEN}"
        ]


def _client(recorder: Recorder) -> WhatsAppClient:
    async def no_sleep(_: float) -> None:
        return None

    return WhatsAppClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        access_token=TOKEN,
        sleep=no_sleep,
        jitter=lambda: 0.0,
    )


def _descriptor(url: str) -> httpx.Response:
    return httpx.Response(200, json={"url": url, "mime_type": "image/jpeg", "file_size": 64})


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"Location": location})


# ----------------------------------------------------------- where the token goes


async def test_the_token_reaches_graph_and_the_meta_cdn_and_nothing_else() -> None:
    """The positive control: without it, every refusal below could be a client
    that sends the token nowhere at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor(f"https://{LOOKASIDE}/whatsapp_business/attachments/?mid=1")
        if request.url.host == LOOKASIDE:
            return _redirect(f"https://{SCONTENT}/v/t1/abc.jpg?{SIGNATURE}")
        return httpx.Response(200, content=JPEG)

    recorder = Recorder(handler)
    downloaded = await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)

    assert downloaded.content == JPEG
    assert recorder.hosts() == [GRAPH, LOOKASIDE, SCONTENT]
    assert recorder.hosts_given_token() == [GRAPH, LOOKASIDE, SCONTENT]


async def test_a_redirect_to_a_public_host_outside_meta_is_refused_before_it_is_sent() -> None:
    """P1-01, reversed. The attacker host is public, so the SSRF check passes it;
    only the host policy stands between it and the platform token."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor(f"https://{LOOKASIDE}/whatsapp_business/attachments/?mid=1")
        if request.url.host == LOOKASIDE:
            return _redirect(f"https://{ATTACKER}/steal")
        return httpx.Response(200, content=JPEG)

    recorder = Recorder(handler)
    with pytest.raises(MediaHostRefusedError):
        await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)

    # Presence of the Meta hops first, so the absence below is not a fetch that
    # never started.
    assert recorder.hosts_given_token() == [GRAPH, LOOKASIDE]
    # Refused, not stripped: the attacker host is not asked anything at all.
    assert ATTACKER not in recorder.hosts()


async def test_a_descriptor_naming_a_foreign_host_directly_is_refused() -> None:
    """P1-02, reversed: the first hop is a hop like any other."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor(f"https://{ATTACKER}/first-hop")
        return httpx.Response(200, content=JPEG)

    recorder = Recorder(handler)
    with pytest.raises(MediaHostRefusedError):
        await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)

    assert recorder.hosts() == [GRAPH]
    assert recorder.hosts_given_token() == [GRAPH]


async def test_a_graph_redirect_to_a_foreign_host_does_not_carry_the_token() -> None:
    """The buffered descriptor path follows redirects by hand too."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _redirect(f"https://{ATTACKER}/descriptor")
        return httpx.Response(200, json={"url": f"https://{LOOKASIDE}/x"})

    recorder = Recorder(handler)
    with pytest.raises(MediaHostRefusedError):
        await _client(recorder).probe_media(MEDIA_ID)

    assert recorder.hosts() == [GRAPH]


@pytest.mark.parametrize(
    "url",
    [
        # A suffix, not a subdomain. `endswith("fbcdn.net")` would take it.
        "https://evilfbcdn.net/x.jpg",
        # The root as a label inside somebody else's name.
        "https://fbcdn.net.attacker.example/x.jpg",
        # Userinfo: the host is the part after the `@`.
        f"https://{LOOKASIDE}@{ATTACKER}/x.jpg",
        # An address literal, even Meta's own, never carries a credential.
        "https://157.240.1.36/x.jpg",
        # An allow-listed host on a port that is not https's.
        f"https://{LOOKASIDE}:8443/x.jpg",
    ],
)
async def test_names_that_only_look_like_meta_are_refused(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor(url)
        return httpx.Response(200, content=JPEG)

    recorder = Recorder(handler)
    with pytest.raises(ExternalServiceError):
        await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)

    assert recorder.hosts() == [GRAPH]
    assert recorder.hosts_given_token() == [GRAPH]


async def test_a_trailing_dot_is_the_same_meta_host_not_a_way_round_the_policy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor(f"https://{LOOKASIDE}./x.jpg")
        return httpx.Response(200, content=JPEG)

    recorder = Recorder(handler)
    downloaded = await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)

    assert downloaded.content == JPEG


async def test_the_allowed_hosts_are_configuration_not_code() -> None:
    """A deployment verification that finds another Meta family (DV-1) is a
    configuration change, and the client must honour it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor(f"https://{LOOKASIDE}/x.jpg")
        return httpx.Response(200, content=JPEG)

    recorder = Recorder(handler)
    client = WhatsAppClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        access_token=TOKEN,
        media_host_roots=("graph.facebook.com", "fbcdn.net"),
    )

    with pytest.raises(MediaHostRefusedError):
        await client.fetch_media(MEDIA_ID, max_bytes=CAP)
    assert recorder.hosts() == [GRAPH]


# --------------------------------------------- the SSRF check, which is separate


async def test_an_allowed_name_resolving_to_a_private_address_is_refused_on_the_first_hop() -> None:
    """M07. The host policy passes `internal.fbcdn.net`; the address does not."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor("https://internal.fbcdn.net/x.jpg")
        return httpx.Response(200, content=JPEG)

    recorder = Recorder(handler)
    with pytest.raises(ExternalServiceError):
        await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)

    assert "internal.fbcdn.net" not in recorder.hosts()


async def test_an_allowed_name_resolving_to_a_private_address_is_refused_on_a_redirect() -> None:
    """M06. The same, one hop later, and aimed at the metadata endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor(f"https://{LOOKASIDE}/x")
        if request.url.host == LOOKASIDE:
            return _redirect("https://metadata.fbsbx.com/latest/meta-data/")
        return httpx.Response(200, content=b"instance-credentials")

    recorder = Recorder(handler)
    with pytest.raises(ExternalServiceError):
        await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)

    assert recorder.hosts() == [GRAPH, LOOKASIDE]


# --------------------------------------------------- how much can be made to read


class CountingStream(httpx.AsyncByteStream):
    """A body of `total` bytes that counts how much of it was ever pulled."""

    def __init__(self, total: int, *, chunk: int = 64 * 1024, prefix: bytes = b"") -> None:
        self.total = total
        self.chunk = chunk
        self.prefix = prefix
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.prefix:
            self.pulled += len(self.prefix)
            yield self.prefix
        remaining = self.total - len(self.prefix)
        while remaining > 0:
            size = min(self.chunk, remaining)
            remaining -= size
            self.pulled += size
            yield b"e" * size


HUGE = 200 * 1024 * 1024


@pytest.mark.parametrize("status", [400, 403, 404, 410])
async def test_a_huge_error_body_from_the_file_host_is_read_only_to_the_error_cap(
    status: int,
) -> None:
    """P1-05, reversed: 200 MB offered, a few kilobytes read."""
    body = CountingStream(HUGE)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor(f"https://{LOOKASIDE}/x")
        return httpx.Response(status, stream=body)

    recorder = Recorder(handler)
    with pytest.raises(ExternalServiceError):
        await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)

    assert 0 < body.pulled <= MAX_ERROR_BODY_BYTES + body.chunk
    # Less than a successful download would have been allowed to read.
    assert body.pulled < CAP


@pytest.mark.parametrize("status", [400, 403, 404, 410])
async def test_a_huge_error_body_from_graph_is_read_only_to_the_error_cap(status: int) -> None:
    body = CountingStream(HUGE)
    recorder = Recorder(lambda request: httpx.Response(status, stream=body))

    with pytest.raises(ExternalServiceError):
        await _client(recorder).probe_media(MEDIA_ID)

    assert 0 < body.pulled <= MAX_ERROR_BODY_BYTES + body.chunk


async def test_an_enormous_descriptor_is_refused_as_malformed_within_its_bound() -> None:
    body = CountingStream(64 * 1024 * 1024, prefix=b'{"url": "https://lookaside.fbsbx.com/')
    recorder = Recorder(lambda request: httpx.Response(200, stream=body))

    with pytest.raises(MalformedMediaDescriptorError):
        await _client(recorder).probe_media(MEDIA_ID)

    assert body.pulled <= MAX_DESCRIPTOR_BYTES + body.chunk
    # Not retried: an oversized descriptor is an answer, not a blip.
    assert len(recorder.requests) == 1


# ------------------------------------------------ what a refusal means (MEDIA-04)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404, json={"error": {"code": 100}}), MediaUnavailableError),
        (httpx.Response(410), MediaUnavailableError),
        (httpx.Response(400, json={"error": {"code": 100}}), MediaUnavailableError),
        (httpx.Response(401), MediaCredentialRefusedError),
        (httpx.Response(403), MediaCredentialRefusedError),
        (httpx.Response(400, json={"error": {"code": 190}}), MediaCredentialRefusedError),
        (httpx.Response(200, content=b"<html>not json</html>"), MalformedMediaDescriptorError),
        (httpx.Response(200, json=["not", "an", "object"]), MalformedMediaDescriptorError),
    ],
)
async def test_a_permanent_descriptor_refusal_is_classified_and_asked_once(
    response: httpx.Response, expected: type[Exception]
) -> None:
    recorder = Recorder(lambda request: response)

    with pytest.raises(expected):
        await _client(recorder).probe_media(MEDIA_ID)

    assert len(recorder.requests) == 1


async def test_a_descriptor_without_a_location_is_malformed() -> None:
    recorder = Recorder(lambda request: httpx.Response(200, json={"mime_type": "image/jpeg"}))

    with pytest.raises(MalformedMediaDescriptorError):
        await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)


@pytest.mark.parametrize("status", [500, 503])
async def test_a_failing_graph_is_retried_within_the_client_budget_then_transient(
    status: int,
) -> None:
    recorder = Recorder(lambda request: httpx.Response(status))

    with pytest.raises(ExternalServiceError) as raised:
        await _client(recorder).probe_media(MEDIA_ID)

    assert type(raised.value) is ExternalServiceError
    assert len(recorder.requests) == 3


async def test_throttling_is_retried_within_the_client_budget_then_reported() -> None:
    recorder = Recorder(lambda request: httpx.Response(429))

    with pytest.raises(RateLimitedError):
        await _client(recorder).probe_media(MEDIA_ID)

    assert len(recorder.requests) == 3


# ------------------------------------------------------------- nothing in the logs


def _rendered(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(JsonFormatter().format(record) for record in caplog.records)


async def test_a_successful_fetch_logs_neither_the_token_nor_the_signature(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M30, success half. The token demonstrably went out - and nowhere else."""
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GRAPH:
            return _descriptor(f"https://{SCONTENT}/v/t1/abc.jpg?{SIGNATURE}")
        return httpx.Response(200, content=JPEG)

    recorder = Recorder(handler)
    await _client(recorder).fetch_media(MEDIA_ID, max_bytes=CAP)

    assert recorder.hosts_given_token() == [GRAPH, SCONTENT]
    assert SIGNATURE in str(recorder.requests[1].url)
    rendered = _rendered(caplog)
    assert TOKEN not in rendered
    assert "SENTINEL-CDN-SIGNATURE" not in rendered


async def test_failed_fetches_log_neither_the_token_nor_the_signature(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M30, failure half: every refusal path this module has, one after another."""
    caplog.set_level(logging.DEBUG)
    echo = json.dumps({"error": {"message": f"Bad token {TOKEN} for {SIGNATURE}", "code": 190}})
    scripts: list[Callable[[httpx.Request], httpx.Response]] = [
        lambda request: httpx.Response(401, content=echo.encode()),
        lambda request: (
            _descriptor(f"https://{ATTACKER}/x?{SIGNATURE}")
            if request.url.host == GRAPH
            else httpx.Response(200, content=JPEG)
        ),
        lambda request: (
            _descriptor(f"https://{SCONTENT}/x?{SIGNATURE}")
            if request.url.host == GRAPH
            else httpx.Response(404, content=echo.encode())
        ),
        lambda request: httpx.Response(503, content=echo.encode()),
    ]
    for script in scripts:
        with pytest.raises(ExternalServiceError) as raised:
            await _client(Recorder(script)).fetch_media(MEDIA_ID, max_bytes=CAP)
        assert TOKEN not in str(raised.value)
        assert "SENTINEL-CDN-SIGNATURE" not in str(raised.value)

    rendered = _rendered(caplog)
    assert "whatsapp.media_host_refused" in rendered
    assert TOKEN not in rendered
    assert "SENTINEL-CDN-SIGNATURE" not in rendered
