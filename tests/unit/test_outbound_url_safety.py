"""Server-side request forgery: the URLs this application refuses to fetch.

One caller has a URL it did not construct — the WhatsApp media download, whose
location arrives in a provider response. That is the shape SSRF takes: a fetch
whose destination somebody else chose.

The earlier audit described this as "media fetch follows redirects while
carrying a bearer token", and **that half is wrong** — httpx strips
`Authorization` when a redirect leaves the origin, which is asserted below so
the correction stays true. What is real is that the request happens at all: the
worker sits inside the deployment network, where `169.254.169.254` answers with
cloud instance credentials and `127.0.0.1:6379` is the Redis holding the
refresh-token denylist and the agent queue. The fetched body is stored as media
and can be read back through the API, which turns a blind request into a read.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.net import UnsafeUrlError, _is_public, validate_outbound_url

REFUSED_ADDRESSES = [
    pytest.param("169.254.169.254", id="cloud-metadata"),
    pytest.param("127.0.0.1", id="loopback"),
    pytest.param("10.0.0.5", id="rfc1918-10"),
    pytest.param("192.168.1.1", id="rfc1918-192"),
    pytest.param("172.16.0.1", id="rfc1918-172"),
    pytest.param("100.64.0.1", id="carrier-grade-nat"),
    # An address to refuse, not one to bind to.
    pytest.param("0.0.0.0", id="unspecified"),  # noqa: S104
    pytest.param("::1", id="ipv6-loopback"),
    pytest.param("fd00::1", id="ipv6-unique-local"),
    pytest.param("fe80::1", id="ipv6-link-local"),
    pytest.param("::ffff:127.0.0.1", id="ipv4-mapped-loopback"),
    pytest.param("::ffff:169.254.169.254", id="ipv4-mapped-metadata"),
]


@pytest.mark.parametrize("address", REFUSED_ADDRESSES)
def test_a_non_public_address_is_not_public(address: str) -> None:
    """The IPv4-mapped forms are the ones a hand-written range list forgets:
    `::ffff:169.254.169.254` reaches the same metadata endpoint."""
    assert _is_public(address) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_a_public_address_is_public(address: str) -> None:
    """The control. A checker that refused everything would pass the tests
    above while breaking every real download."""
    assert _is_public(address) is True


@pytest.mark.parametrize("address", REFUSED_ADDRESSES)
def test_a_url_pointing_at_a_non_public_address_is_refused(address: str) -> None:
    host = f"[{address}]" if ":" in address else address

    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(f"https://{host}/media/file.jpg")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/f.jpg",
        "file:///etc/passwd",
        "ftp://example.com/f.jpg",
        "gopher://example.com/",
        "//example.com/f.jpg",
    ],
)
def test_only_https_is_permitted(url: str) -> None:
    """Refusing by scheme removes `file://` and friends as a class rather than
    by enumerating them."""
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url(url)


def test_a_url_with_no_host_is_refused() -> None:
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url("https:///media/file.jpg")


def test_a_name_that_does_not_resolve_is_refused() -> None:
    """Fails closed. A name nobody can resolve is not a name to fetch from."""
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url("https://this-name-does-not-exist.invalid/f.jpg")


def test_a_public_https_url_is_accepted() -> None:
    """The control for the whole module: a real provider URL must pass, or the
    guard has broken media rather than secured it."""
    validate_outbound_url("https://graph.facebook.com/v21.0/1234")


def test_httpx_strips_authorization_across_origins() -> None:
    """Pins the correction to the earlier audit.

    It claimed the media fetch carries a bearer token across redirects. httpx
    removes `Authorization` when the origin changes, and this asserts that
    directly against the installed version so the claim cannot quietly become
    true again after an upgrade.
    """
    client = httpx.Client()
    request = httpx.Request(
        "GET",
        "https://graph.facebook.com/file",
        headers={"Authorization": "Bearer secret-token"},
    )

    crossed = client._redirect_headers(request, httpx.URL("https://evil.example/f"), "GET")
    same = client._redirect_headers(request, httpx.URL("https://graph.facebook.com/other"), "GET")

    assert "authorization" not in {k.lower() for k in crossed}
    assert "authorization" in {k.lower() for k in same}
