"""MediaAdapterContract: where a provider says a file is, fetched only where it may be.

The shared media pipeline is the same for every channel; what an adapter adds
is the fetch. This pins the two fetchers the foundation ships (OMNI-009,
OMNI-024): `UrlMediaFetcher`, which a URL-locating channel reuses rather than
copies, and the WhatsApp handle fetcher behind the WhatsApp adapter.

Hermetic by construction: no request leaves the process (an `httpx`
`MockTransport` answers) and no name is resolved by the network (a static
table answers, judged by exactly the rule a real answer is). A URL from a
payload is a stranger's choice, so the properties are refusals: a host outside
the provider's own, a redirect that leaves them, a name that resolves to a
private address, an expired link, a body that passes the cap mid-stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest

from app.channels.inbound import AttachmentLocator
from app.channels.media import (
    MalformedMediaDescriptorError,
    MediaCredentialRefusedError,
    MediaHostRefusedError,
    MediaTooLargeError,
    MediaUnavailableError,
    UrlMediaFetcher,
)
from app.core.exceptions import ExternalServiceError
from app.core.net import static_resolver
from app.db.models.media import MediaLocatorKind
from app.integrations.whatsapp.adapter import WhatsAppMediaFetcher
from app.integrations.whatsapp.client import WhatsAppClient

ROOT = "files.provider.example"
CDN = f"https://cdn.{ROOT}"
# Public addresses from the documentation ranges are judged non-public, so a
# real-looking public address stands in; nothing ever connects to it.
PUBLIC = "93.184.216.34"
RESOLVER = static_resolver(
    {
        f"cdn.{ROOT}": [PUBLIC],
        f"edge.{ROOT}": [PUBLIC],
        "cdn.elsewhere.example": [PUBLIC],
        f"internal.{ROOT}": ["10.0.0.5"],
    }
)
CAP = 1_024
# A fixture value, not a credential: what the fetcher must send to the
# provider's own hosts and to nobody else.
PAGE_TOKEN = "page-token-fixture"


def _url(locator: str, *, expires_at: datetime | None = None) -> AttachmentLocator:
    return AttachmentLocator(
        locator_kind=MediaLocatorKind.URL,
        locator=locator,
        media_kind="image",
        mime_type="image/jpeg",
        expires_at=expires_at,
    )


class Provider:
    """The provider's CDN: answers by path, and remembers every request."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.url.host}{request.url.path}"
        response = self.routes.get(key)
        if response is None:
            raise AssertionError(f"nothing should have asked for {key}")
        return response


def _fetcher(provider: Provider) -> UrlMediaFetcher:
    return UrlMediaFetcher(
        http=httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)),
        host_roots=[ROOT],
        token=PAGE_TOKEN,
        resolver=RESOLVER,
    )


def _jpeg(body: bytes = b"\xff\xd8\xff" + b"0" * 64) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "image/jpeg"})


async def test_a_file_on_the_providers_own_host_is_fetched_with_its_token() -> None:
    provider = Provider({f"cdn.{ROOT}/a.jpg": _jpeg()})

    fetched = await _fetcher(provider).fetch(_url(f"{CDN}/a.jpg"), max_bytes=CAP)

    assert fetched.content.startswith(b"\xff\xd8\xff")
    assert fetched.mime_type == "image/jpeg"
    (request,) = provider.requests
    assert request.headers["Authorization"] == f"Bearer {PAGE_TOKEN}"


async def test_a_signed_link_longer_than_any_handle_is_fetched_whole() -> None:
    """Instagram and Messenger CDN links carry long signed queries (OMNI-009)."""
    signature = "s" * 1_500
    provider = Provider({f"cdn.{ROOT}/b.jpg": _jpeg()})

    await _fetcher(provider).fetch(_url(f"{CDN}/b.jpg?sig={signature}"), max_bytes=CAP)

    (request,) = provider.requests
    assert request.url.params["sig"] == signature


async def test_a_host_outside_the_providers_is_refused_before_any_request() -> None:
    provider = Provider({})

    with pytest.raises(MediaHostRefusedError):
        await _fetcher(provider).fetch(_url("https://cdn.elsewhere.example/a.jpg"), max_bytes=CAP)
    assert provider.requests == []


async def test_a_lookalike_host_is_not_the_providers() -> None:
    """`evil-files.provider.example.attacker` and `xfiles.provider.example` are not `ROOT`."""
    resolver = static_resolver({f"x{ROOT}": [PUBLIC]})
    provider = Provider({})
    fetcher = UrlMediaFetcher(
        http=httpx.AsyncClient(transport=httpx.MockTransport(provider.handle)),
        host_roots=[ROOT],
        resolver=resolver,
    )

    with pytest.raises(MediaHostRefusedError):
        await fetcher.fetch(_url(f"https://x{ROOT}/a.jpg"), max_bytes=CAP)
    assert provider.requests == []


async def test_a_redirect_off_the_providers_hosts_is_refused() -> None:
    provider = Provider(
        {
            f"cdn.{ROOT}/c.jpg": httpx.Response(
                302, headers={"Location": "https://cdn.elsewhere.example/c.jpg"}
            )
        }
    )

    with pytest.raises(MediaHostRefusedError):
        await _fetcher(provider).fetch(_url(f"{CDN}/c.jpg"), max_bytes=CAP)
    # The first hop was asked; the foreign one never was, so the token never left.
    assert [request.url.host for request in provider.requests] == [f"cdn.{ROOT}"]


async def test_a_redirect_within_the_providers_hosts_is_followed() -> None:
    provider = Provider(
        {
            f"cdn.{ROOT}/d.jpg": httpx.Response(
                302, headers={"Location": f"https://edge.{ROOT}/d"}
            ),
            f"edge.{ROOT}/d": _jpeg(),
        }
    )

    fetched = await _fetcher(provider).fetch(_url(f"{CDN}/d.jpg"), max_bytes=CAP)

    assert fetched.content.startswith(b"\xff\xd8\xff")
    assert len(provider.requests) == 2


async def test_a_name_that_resolves_privately_is_refused_whoever_resolved_it() -> None:
    """The resolver is injectable for tests; the judgement of its answer is not."""
    provider = Provider({})

    with pytest.raises(ExternalServiceError) as refused:
        await _fetcher(provider).fetch(_url(f"https://internal.{ROOT}/x.jpg"), max_bytes=CAP)
    assert not isinstance(refused.value, MediaHostRefusedError)
    assert provider.requests == []


async def test_a_plain_http_link_is_refused_before_any_request() -> None:
    """Refused by the address guard before the host list is even consulted."""
    provider = Provider({})

    with pytest.raises(ExternalServiceError):
        await _fetcher(provider).fetch(_url(f"http://cdn.{ROOT}/a.jpg"), max_bytes=CAP)
    assert provider.requests == []


async def test_an_expired_link_is_unavailable_without_asking() -> None:
    provider = Provider({})
    expired = _url(f"{CDN}/e.jpg", expires_at=datetime.now(UTC) - timedelta(seconds=1))

    with pytest.raises(MediaUnavailableError):
        await _fetcher(provider).fetch(expired, max_bytes=CAP)
    assert provider.requests == []


async def test_a_body_past_the_cap_is_abandoned_mid_stream() -> None:
    """No Content-Length to trust: the cap is enforced on the bytes as they arrive."""

    async def endless() -> AsyncIterator[bytes]:
        for _ in range(64):
            yield b"0" * 256

    provider = Provider({f"cdn.{ROOT}/big.jpg": httpx.Response(200, content=endless())})

    with pytest.raises(MediaTooLargeError):
        await _fetcher(provider).fetch(_url(f"{CDN}/big.jpg"), max_bytes=CAP)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (401, MediaCredentialRefusedError),
        (403, MediaCredentialRefusedError),
        (404, MediaUnavailableError),
        (410, MediaUnavailableError),
    ],
)
async def test_a_permanent_refusal_is_classified_by_its_remedy(
    status: int, outcome: type[Exception]
) -> None:
    provider = Provider({f"cdn.{ROOT}/f.jpg": httpx.Response(status)})

    with pytest.raises(outcome):
        await _fetcher(provider).fetch(_url(f"{CDN}/f.jpg"), max_bytes=CAP)


async def test_a_server_failure_is_retryable_not_a_decision() -> None:
    provider = Provider({f"cdn.{ROOT}/g.jpg": httpx.Response(503)})

    with pytest.raises(ExternalServiceError) as failed:
        await _fetcher(provider).fetch(_url(f"{CDN}/g.jpg"), max_bytes=CAP)
    assert not isinstance(
        failed.value, MediaUnavailableError | MediaCredentialRefusedError | MediaTooLargeError
    )


async def test_too_many_redirects_end_the_fetch() -> None:
    provider = Provider(
        {
            f"cdn.{ROOT}/loop": httpx.Response(302, headers={"Location": f"https://edge.{ROOT}/l"}),
            f"edge.{ROOT}/l": httpx.Response(302, headers={"Location": f"{CDN}/loop"}),
        }
    )

    with pytest.raises(ExternalServiceError):
        await _fetcher(provider).fetch(_url(f"{CDN}/loop"), max_bytes=CAP)
    assert len(provider.requests) <= 4


async def test_a_handle_is_not_a_url() -> None:
    handle = AttachmentLocator(
        locator_kind=MediaLocatorKind.HANDLE, locator="1234567890", media_kind="image"
    )

    with pytest.raises(MalformedMediaDescriptorError):
        await _fetcher(Provider({})).fetch(handle, max_bytes=CAP)


async def test_a_url_fetch_declares_only_what_the_payload_said() -> None:
    probe = await _fetcher(Provider({})).probe(_url(f"{CDN}/h.jpg"))

    assert (probe.mime_type, probe.byte_size) == ("image/jpeg", None)


def test_an_empty_allow_list_is_a_misconfiguration() -> None:
    with pytest.raises(ValueError):
        UrlMediaFetcher(http=httpx.AsyncClient(), host_roots=[])


# ------------------------------------------------------------- WhatsApp


class FakeGraph:
    """The two Graph calls a handle takes, recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def probe_media(self, handle: str) -> Any:
        self.calls.append(("probe", handle))
        return type("Descriptor", (), {"mime_type": "image/png", "byte_size": 12})()

    async def fetch_media(self, handle: str, *, max_bytes: int) -> Any:
        self.calls.append(("fetch", handle))
        return type(
            "Downloaded",
            (),
            {"content": b"\x89PNG\r\n\x1a\n0000", "mime_type": "image/png", "declared_size": 12},
        )()


async def test_whatsapp_fetches_a_handle_in_metas_two_steps() -> None:
    graph = FakeGraph()
    fetcher = WhatsAppMediaFetcher(client=cast(WhatsAppClient, graph))
    handle = AttachmentLocator(
        locator_kind=MediaLocatorKind.HANDLE, locator="wa-handle-1", media_kind="image"
    )

    probe = await fetcher.probe(handle)
    fetched = await fetcher.fetch(handle, max_bytes=CAP)

    assert (probe.mime_type, probe.byte_size) == ("image/png", 12)
    assert fetched.content.startswith(b"\x89PNG")
    assert graph.calls == [("probe", "wa-handle-1"), ("fetch", "wa-handle-1")]


async def test_whatsapp_never_fetches_a_url_it_was_handed() -> None:
    """A URL locator on a WhatsApp file is not Meta's handle: refused, not fetched."""
    graph = FakeGraph()
    fetcher = WhatsAppMediaFetcher(client=cast(WhatsAppClient, graph))

    with pytest.raises(MalformedMediaDescriptorError):
        await fetcher.fetch(_url(f"{CDN}/a.jpg"), max_bytes=CAP)
    assert graph.calls == []
