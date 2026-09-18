"""Deciding whether a URL names a host that may be handed a credential.

A different question from the one `app.core.net` answers. That module asks
whether a destination is *reachable safely* - a public address, over https,
pinned at connect time - and every address on the internet passes it. This one
asks whether a destination is *entitled to our token*, and almost nothing is.
Both are asked on every credential-bearing hop, and neither can stand in for the
other: `metadata.example.com` can be an allowed name that resolves somewhere
private, and `cdn.attacker.example` is a public address that must never see a
bearer (MEDIA-01).

Standard library only, so configuration can validate its host lists with the
same code the client enforces them with.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Final
from urllib.parse import urlsplit

HTTPS: Final = "https"
DEFAULT_HTTPS_PORT: Final = 443
# One DNS label after IDNA encoding: letters, digits and inner hyphens, at most
# 63 characters. Checked on the encoded form, so an internationalised name is
# judged as the `xn--` spelling the resolver will actually be asked for.
_LABEL: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
MAX_HOSTNAME_LENGTH: Final = 253


class HostNotAuthorizedError(Exception):
    """A URL whose host may not receive a credential.

    Not a `WaslaError`, like `UnsafeUrlError`: it is caught by the integration
    that raised it and reported as a failed fetch, so nothing learns which rule
    refused it.
    """


def normalize_hostname(value: str) -> str:
    """A hostname in the one spelling every comparison here uses.

    Lower case, IDNA-encoded, with at most one trailing dot removed. Refuses
    anything that is not plainly a DNS name: an address literal, an empty
    label, a label too long or carrying characters DNS would not, and a name
    whose internationalised form will not encode. A refusal is always the safe
    answer here, because the caller is deciding whether to attach a token.
    """
    host = value.strip()
    if host.endswith("."):
        # `graph.facebook.com.` is the same name as `graph.facebook.com`, and
        # treating them as different would let the dotted spelling slip past an
        # exact-match root. Only one dot: `name..` is not a spelling of anything.
        host = host[:-1]
    if not host or host.endswith("."):
        raise HostNotAuthorizedError("empty or malformed host")
    if host.startswith("[") or _is_address(host):
        raise HostNotAuthorizedError("address literals never carry a credential")
    try:
        encoded = host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise HostNotAuthorizedError("host is not a valid internationalised name") from error
    if len(encoded) > MAX_HOSTNAME_LENGTH:
        raise HostNotAuthorizedError("host is too long")
    labels = encoded.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise HostNotAuthorizedError("host is not a DNS name")
    if labels[-1].isdigit():
        # A numeric top-level label is an address in disguise (`0x7f.1`,
        # `2130706433`), which a resolver may well answer.
        raise HostNotAuthorizedError("numeric top-level label")
    return encoded


def credential_host(url: str) -> str:
    """The normalised host of a URL that is about to be sent a credential.

    Refuses a non-https scheme, embedded userinfo (`https://graph.facebook.com@
    evil.example/`, where the host is the part after the `@`), and any port
    other than https's own. A URL that carries any of those is not one Meta
    handed out, whatever its host turns out to be.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise HostNotAuthorizedError("unparseable URL") from error
    if parts.scheme.lower() != HTTPS:
        raise HostNotAuthorizedError("only https URLs carry a credential")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise HostNotAuthorizedError("a URL with userinfo never carries a credential")
    if port is not None and port != DEFAULT_HTTPS_PORT:
        raise HostNotAuthorizedError("only the https port carries a credential")
    host = parts.hostname
    if not host:
        raise HostNotAuthorizedError("the URL names no host")
    return normalize_hostname(host)


def within(host: str, root: str) -> bool:
    """Whether `host` is `root` or a subdomain of it, on a label boundary.

    `host.endswith(root)` is the bug this exists to avoid: `evilfbcdn.net`
    ends with `fbcdn.net`. Both arguments must already be normalised.
    """
    return host == root or host.endswith("." + root)


def normalize_roots(roots: Iterable[str]) -> tuple[str, ...]:
    """A configured host list, normalised and de-duplicated, in order.

    Raises `ValueError` on any entry that is not a DNS name, so a typo in the
    environment is a container that will not start rather than a policy that
    quietly matches nothing - or, worse, a wildcard somebody meant literally.
    """
    normalised: list[str] = []
    for root in roots:
        if "*" in root or "/" in root or ":" in root:
            raise ValueError(f"{root!r} is not a host name; list bare names such as fbcdn.net")
        try:
            value = normalize_hostname(root)
        except HostNotAuthorizedError as error:
            raise ValueError(f"{root!r} is not a valid host name") from error
        if value not in normalised:
            normalised.append(value)
    return tuple(normalised)


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


__all__ = [
    "HostNotAuthorizedError",
    "credential_host",
    "normalize_hostname",
    "normalize_roots",
    "within",
]
