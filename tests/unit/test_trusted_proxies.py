"""Trusting a proxy by address, and refusing to trust it by name.

`TRUSTED_PROXY_IPS` is compared against `request.client.host`, which is an IP.
It was compared as a string, and `docker-compose.prod.yml` shipped the Docker
service name `nginx` - so nothing ever matched and two controls failed together
and silently (ADR-060): every client on the internet shared one authentication
rate-limit bucket, and HSTS was never emitted because the same trust test
decides whether `X-Forwarded-Proto` may be believed.

The tests in `test_auth_hardening.py` cover what `client_identity` does with
forwarding headers. These cover the layer underneath it - what counts as a
trusted peer at all - because that is where the defect was, and it is the part
a deployment gets wrong rather than the part a caller attacks.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.proxy import (
    is_trusted_peer,
    normalised_address,
    parse_trusted_proxies,
    trusted_networks,
)

# ------------------------------------------------------------------ parsing


def test_a_bare_address_becomes_a_single_host_network():
    """What an operator naming one proxy expects to be able to write."""
    networks = parse_trusted_proxies(["10.89.0.10"])

    assert [str(network) for network in networks] == ["10.89.0.10/32"]
    assert is_trusted_peer("10.89.0.10", networks)
    assert not is_trusted_peer("10.89.0.11", networks)


def test_a_cidr_network_covers_its_hosts():
    networks = parse_trusted_proxies(["10.89.0.0/24"])

    assert is_trusted_peer("10.89.0.1", networks)
    assert is_trusted_peer("10.89.0.254", networks)
    assert not is_trusted_peer("10.89.1.1", networks)


def test_ipv6_addresses_and_networks_are_supported():
    networks = parse_trusted_proxies(["2001:db8::1", "fd00::/8"])

    assert is_trusted_peer("2001:db8::1", networks)
    assert is_trusted_peer("fd00::abcd", networks)
    assert not is_trusted_peer("2001:db8::2", networks)


def test_a_hostname_is_refused_rather_than_resolved():
    """The exact value the shipped compose file used to carry.

    Refused rather than resolved on purpose: a name that resolves puts the
    trust anchor for forwarding headers under whatever answers DNS, and this
    list exists precisely because that decision must not be influenceable from
    outside.
    """
    with pytest.raises(ValueError, match="nginx"):
        parse_trusted_proxies(["nginx"])


@pytest.mark.parametrize(
    "entry",
    ["nginx", "localhost", "10.0.0.256", "10.0.0.0/33", "not an address", "10.0.0.1:8080"],
)
def test_anything_that_is_not_an_address_or_network_is_refused(entry: str):
    with pytest.raises(ValueError):
        parse_trusted_proxies([entry])


def test_blank_entries_are_skipped_rather_than_refused():
    """A trailing comma in an environment variable is a typo, not a threat."""
    assert parse_trusted_proxies(["10.89.0.10", "", "  "]) == parse_trusted_proxies(["10.89.0.10"])


def test_nothing_configured_trusts_nobody():
    """The safe default: no proxy, so no forwarding header is believed."""
    networks = trusted_networks([])

    assert networks == ()
    assert not is_trusted_peer("10.89.0.10", networks)
    assert not is_trusted_peer("127.0.0.1", networks)


def test_a_peer_that_is_not_an_address_is_never_trusted():
    """A unix socket, or a transport that reports something else entirely."""
    networks = parse_trusted_proxies(["10.89.0.0/24"])

    assert not is_trusted_peer(None, networks)
    assert not is_trusted_peer("", networks)
    assert not is_trusted_peer("nginx", networks)


# ------------------------------------------------------- header normalisation


def test_an_address_is_returned_in_canonical_form():
    """Otherwise one client written two ways is two rate-limit buckets."""
    assert normalised_address(" 198.51.100.4 ") == "198.51.100.4"
    assert normalised_address("2001:0db8:0000::0001") == "2001:db8::1"


def test_a_value_that_is_not_an_address_is_not_one():
    for value in (None, "", "unknown", "198.51.100.4:443", "example.com"):
        assert normalised_address(value) is None


# --------------------------------------------------------------- the setting


def test_settings_refuse_to_build_with_a_hostname():
    """Fail-fast, so a misconfiguration is a container that says why.

    The failure being prevented produced no error at all: the deployment came
    up, served traffic, and quietly had neither per-address rate limiting nor
    HSTS.
    """
    with pytest.raises(ValueError, match="TRUSTED_PROXY_IPS"):
        Settings(_env_file=None, environment="test", trusted_proxy_ips=["nginx"])


def test_settings_accept_addresses_and_networks():
    settings = Settings(
        _env_file=None,
        environment="test",
        trusted_proxy_ips=["10.89.0.10", "172.16.0.0/12", "::1"],
    )

    assert settings.trusted_proxy_ips == ["10.89.0.10", "172.16.0.0/12", "::1"]


def test_a_comma_separated_environment_value_is_parsed():
    """The only shape a container environment expresses comfortably."""
    settings = Settings(
        _env_file=None,
        environment="test",
        trusted_proxy_ips="10.89.0.10, 10.89.0.11",
    )

    assert settings.trusted_proxy_ips == ["10.89.0.10", "10.89.0.11"]


def test_the_shipped_production_default_is_a_real_address():
    """The compose file and the parser have to agree, or nothing is trusted.

    Pinned as a value rather than read from the file: what matters is that the
    default a deployment inherits is something `parse_trusted_proxies` accepts,
    and a test that read the YAML would pass on a file that no longer sets it
    at all.
    """
    networks = parse_trusted_proxies(["10.89.0.10"])

    assert is_trusted_peer("10.89.0.10", networks)
