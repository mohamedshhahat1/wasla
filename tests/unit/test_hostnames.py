"""Hostname rules for deciding who may be sent a credential (MEDIA-01).

The matcher is the whole policy, so its edge cases are pinned directly rather
than only through the client: a suffix mistaken for a subdomain, a trailing dot,
an internationalised spelling, an address in disguise.
"""

from __future__ import annotations

import pytest

from app.core.config import DEFAULT_META_MEDIA_HOST_ROOTS, Settings
from app.core.hostnames import (
    HostNotAuthorizedError,
    credential_host,
    normalize_hostname,
    normalize_roots,
    within,
)


@pytest.mark.parametrize(
    ("host", "root", "expected"),
    [
        ("fbcdn.net", "fbcdn.net", True),
        ("scontent.xx.fbcdn.net", "fbcdn.net", True),
        ("evilfbcdn.net", "fbcdn.net", False),
        ("fbcdn.net.attacker.example", "fbcdn.net", False),
        ("graph.facebook.com", "graph.facebook.com", True),
        ("facebook.com", "graph.facebook.com", False),
        ("notgraph.facebook.com", "graph.facebook.com", False),
    ],
)
def test_matching_is_on_a_label_boundary(host: str, root: str, expected: bool) -> None:
    assert within(host, root) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Lookaside.FBSBX.com", "lookaside.fbsbx.com"),
        ("lookaside.fbsbx.com.", "lookaside.fbsbx.com"),
        ("bücher.example", "xn--bcher-kva.example"),
    ],
)
def test_names_are_normalised_to_one_spelling(value: str, expected: str) -> None:
    assert normalize_hostname(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "fbcdn.net..",
        "127.0.0.1",
        "::1",
        "[::1]",
        "localhost",
        "2130706433",
        "0x7f.1",
        "a_b.example",
        "-bad.example",
        "x" * 64 + ".example",
    ],
)
def test_anything_that_is_not_plainly_a_dns_name_is_refused(value: str) -> None:
    with pytest.raises(HostNotAuthorizedError):
        normalize_hostname(value)


@pytest.mark.parametrize(
    "url",
    [
        "http://lookaside.fbsbx.com/x",
        "https://lookaside.fbsbx.com@evil.example/x",
        "https://user:pass@lookaside.fbsbx.com/x",
        "https://lookaside.fbsbx.com:8443/x",
        "https://[::1]/x",
        "https:///x",
    ],
)
def test_urls_that_are_not_credential_shaped_are_refused(url: str) -> None:
    with pytest.raises(HostNotAuthorizedError):
        credential_host(url)


def test_the_https_port_spelled_out_is_the_same_host() -> None:
    assert credential_host("https://lookaside.fbsbx.com:443/x?y=1") == "lookaside.fbsbx.com"


def test_configured_roots_are_normalised_and_deduplicated() -> None:
    assert normalize_roots(["FBCDN.net", "fbcdn.net.", "graph.facebook.com"]) == (
        "fbcdn.net",
        "graph.facebook.com",
    )


@pytest.mark.parametrize("root", ["*.fbcdn.net", "https://fbcdn.net", "fbcdn.net:443", "10.0.0.1"])
def test_a_root_that_is_not_a_bare_name_is_refused(root: str) -> None:
    with pytest.raises(ValueError):
        normalize_roots([root])


def test_the_setting_defaults_to_the_documented_meta_families() -> None:
    assert tuple(Settings(environment="test").meta_media_host_roots) == (
        DEFAULT_META_MEDIA_HOST_ROOTS
    )


def test_the_setting_accepts_a_comma_separated_environment_value() -> None:
    settings = Settings(environment="test", meta_media_host_roots="FBCDN.net, fbsbx.com")
    assert settings.meta_media_host_roots == ["fbcdn.net", "fbsbx.com"]


@pytest.mark.parametrize("value", ["", "*.fbcdn.net", "fbcdn.net,https://evil.example"])
def test_the_setting_refuses_to_start_on_an_unusable_list(value: str) -> None:
    with pytest.raises(ValueError):
        Settings(environment="test", meta_media_host_roots=value)
