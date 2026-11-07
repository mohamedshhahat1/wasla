"""The browser's address, as signed by the frontend tier (FEB-01, ADR-190).

The customer app and the console sign people in through a handful of route
handlers on their own origin (ADR-189). Those handlers call this API
server-to-server, so every login, refresh, logout and Google callback arrives
from the frontend tier's address - and the authentication buckets count by
address. Without this module every person signing in would share the tier's
ten-a-minute budget: a sign-in outage on launch day.

So the tier says which browser it is speaking for, in three headers:

- ``X-Wasla-Client-IP``   the address the tier received the request from
- ``X-Wasla-Forward-Ts``  when it signed, in integer Unix seconds
- ``X-Wasla-Forward-Sig`` lowercase hex HMAC-SHA256 over the canonical string

and the canonical string binds the claim to one request::

    "v1\\n" + ts + "\\n" + client_ip + "\\n" + METHOD + "\\n" + path

The method and path are in it so that a signature taken from one route cannot
assert an address on another; the timestamp so that it cannot be kept. There is
deliberately no nonce store: a replay inside the window asserts the *same*
address on the *same* route, so it cannot choose a different bucket - and the
bucket itself bounds it.

**The one property that must never regress: an unsigned claim changes
nothing.** A browser can send all three headers - CORS allows any request
header - and so can anybody with curl. What it cannot do is produce the HMAC
without the secret. Every failure - absent, unsigned, wrongly signed, stale,
malformed, or the feature switched off - is silent to the caller and leaves the
identity exactly what it was before this module existed. A misconfigured tier
therefore degrades to the old per-tier bucket, never to an outage, and never to
a 4xx that would tell a prober which check it failed.

The verifier is a pure function of (headers, method, path, now, secret), so it
is tested without a clock, a server or a request.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)

FORWARD_IP_HEADER: Final = "X-Wasla-Client-IP"
FORWARD_TS_HEADER: Final = "X-Wasla-Forward-Ts"
FORWARD_SIG_HEADER: Final = "X-Wasla-Forward-Sig"
FORWARD_HEADERS: Final = (FORWARD_IP_HEADER, FORWARD_TS_HEADER, FORWARD_SIG_HEADER)

#: The only canonical-string format accepted. A later format gets a new prefix
#: and can coexist with this one during a rollout.
FORWARD_VERSION: Final = "v1"

#: How far a signing time may be from this process's clock, either way. Long
#: enough for clock skew between the tier and the API and a slow hop; short
#: enough that a signature lifted from a log is useless within a minute.
FRESHNESS_SECONDS: Final = 60

#: The longest textual IPv6 address (an IPv4-mapped one written in full).
MAX_ADDRESS_LENGTH: Final = 45

_SIGNATURE = re.compile(r"[0-9a-fA-F]{64}")
_TIMESTAMP = re.compile(r"[0-9]{1,12}")


class ForwardResult(StrEnum):
    """Why a forward was, or was not, believed. A closed metric label domain."""

    VALID = "valid"
    ABSENT = "absent"
    DISABLED = "disabled"
    BAD_SIGNATURE = "bad_signature"
    STALE = "stale"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class ForwardVerdict:
    """The verifier's answer: an address only when the result is `VALID`."""

    result: ForwardResult
    address: str | None = None


class HeaderSource(Protocol):
    """What the verifier reads: Starlette's `Headers` satisfies it."""

    def getlist(self, key: str) -> list[str]: ...


def canonical_string(ts: str, client_ip: str, method: str, path: str) -> bytes:
    """The exact bytes the frontend tier signs (FW-01)."""
    return "\n".join((FORWARD_VERSION, ts, client_ip, method.upper(), path)).encode("utf-8")


def sign_forward(secret: str, *, ts: str, client_ip: str, method: str, path: str) -> str:
    """The signature the tier sends. Used by the tests, the e2e smoke and the vectors."""
    return hmac.new(
        secret.encode("utf-8"), canonical_string(ts, client_ip, method, path), hashlib.sha256
    ).hexdigest()


def _single(headers: HeaderSource, name: str) -> str | None | bool:
    """One value, ``None`` when absent, ``False`` when repeated or a list."""
    values = headers.getlist(name)
    if not values:
        return None
    if len(values) > 1 or "," in values[0]:
        return False
    return values[0]


def _normalised(raw: str) -> str | None:
    """The identity text of a signed address, or ``None`` if it is not one (FW-05)."""
    candidate = raw.strip()
    # No port, no brackets, no zone id: `ipaddress` would accept a zone id, and
    # the same host with two spellings must not get two buckets.
    if (
        not candidate
        or any(mark in candidate for mark in "[]%/")
        or len(candidate) > MAX_ADDRESS_LENGTH
    ):
        return None
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def verify_forward(
    headers: HeaderSource,
    *,
    method: str,
    path: str,
    now: float,
    secret: str | None,
) -> ForwardVerdict:
    """Decide whether the request carries a valid v1 forward, and for which address."""
    present = [name for name in FORWARD_HEADERS if headers.getlist(name)]
    if not secret:
        return ForwardVerdict(ForwardResult.DISABLED if present else ForwardResult.ABSENT)
    if not present:
        return ForwardVerdict(ForwardResult.ABSENT)
    if len(present) != len(FORWARD_HEADERS):
        return ForwardVerdict(ForwardResult.MALFORMED)

    client_ip = _single(headers, FORWARD_IP_HEADER)
    ts = _single(headers, FORWARD_TS_HEADER)
    signature = _single(headers, FORWARD_SIG_HEADER)
    if not isinstance(client_ip, str) or not isinstance(ts, str) or not isinstance(signature, str):
        return ForwardVerdict(ForwardResult.MALFORMED)
    if not _TIMESTAMP.fullmatch(ts) or len(client_ip) > MAX_ADDRESS_LENGTH:
        return ForwardVerdict(ForwardResult.MALFORMED)
    if not _SIGNATURE.fullmatch(signature):
        # Nothing further is computed for a value that cannot be a signature.
        return ForwardVerdict(ForwardResult.BAD_SIGNATURE)

    # Over the raw header value: the address is normalised only once the
    # signature says the tier really sent this text.
    expected = sign_forward(secret, ts=ts, client_ip=client_ip, method=method, path=path)
    if not hmac.compare_digest(expected, signature.lower()):
        return ForwardVerdict(ForwardResult.BAD_SIGNATURE)
    if abs(now - int(ts)) > FRESHNESS_SECONDS:
        return ForwardVerdict(ForwardResult.STALE)

    address = _normalised(client_ip)
    if address is None:
        return ForwardVerdict(ForwardResult.MALFORMED)
    return ForwardVerdict(ForwardResult.VALID, address)


#: The clock `client_identity` reads. A seam, so a test can move it instead of
#: sleeping or comparing against the wall clock.
forward_clock: Callable[[], float] = time.time

_WARN_INTERVAL_SECONDS: Final = 60.0
_last_warned: dict[ForwardResult, float] = {}


def record_forward(verdict: ForwardVerdict) -> None:
    """Count the outcome, and warn about a refused forward at most once a minute per kind.

    Neither the counter nor the log carries the address, the signature, the
    timestamp's value or the secret - only the closed result word. A forward
    that was never attempted is not counted when the feature is off, so a
    deployment without a frontend does not count every request.
    """
    from app.core.telemetry import observe_client_ip_forward

    observe_client_ip_forward(verdict.result.value)
    if verdict.result in (ForwardResult.VALID, ForwardResult.ABSENT):
        return
    moment = time.monotonic()
    last = _last_warned.get(verdict.result)
    if last is not None and moment - last < _WARN_INTERVAL_SECONDS:
        return
    _last_warned[verdict.result] = moment
    logger.warning(
        "client_ip_forward.ignored",
        extra={"event": "client_ip_forward.ignored", "result": verdict.result.value},
    )


__all__ = [
    "FORWARD_HEADERS",
    "FORWARD_IP_HEADER",
    "FORWARD_SIG_HEADER",
    "FORWARD_TS_HEADER",
    "FORWARD_VERSION",
    "FRESHNESS_SECONDS",
    "ForwardResult",
    "ForwardVerdict",
    "canonical_string",
    "forward_clock",
    "record_forward",
    "sign_forward",
    "verify_forward",
]
