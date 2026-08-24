"""Resend webhook signatures (Svix scheme).

Resend signs through Svix, which is a different shape from Meta's
`X-Hub-Signature-256` and has to be treated as such rather than by analogy.
Three headers arrive - `svix-id`, `svix-timestamp` and `svix-signature` - and
the signed content is the three-part string `{id}.{timestamp}.{body}`, not the
body alone. Signing the body by itself would prove nothing about *which*
delivery this is, which is precisely what makes a captured request replayable.

The secret is `whsec_` followed by base64. The bytes it decodes to are the
HMAC key; the string itself is not. Using the string produces a signature that
never matches - the kind of bug that gets "fixed" by turning verification off.

Two things are checked and neither is optional: the signature is valid, and the
timestamp is recent. A signature stays valid forever, so without the window a
request captured once could be replayed for as long as the secret lives.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from typing import Final

ID_HEADER: Final = "svix-id"
TIMESTAMP_HEADER: Final = "svix-timestamp"
SIGNATURE_HEADER: Final = "svix-signature"

SECRET_PREFIX: Final = "whsec_"  # noqa: S105 - a prefix, not a credential
# The only signature version Svix emits today. An unrecognised version is
# skipped rather than trusted: a scheme this code cannot verify is not a
# scheme it accepts.
SIGNATURE_VERSION: Final = "v1"
# How far out of date a delivery may be, in seconds. Svix's own recommendation,
# and the width of the window in which a captured request can be replayed - so
# it is deliberately short rather than generous.
DEFAULT_TOLERANCE_SECONDS: Final = 300


def _hmac_key(secret: str) -> bytes | None:
    """The raw HMAC key behind a `whsec_` secret, or None if unusable.

    A malformed secret answers None rather than raising, so a misconfiguration
    becomes a refused delivery instead of a 500 that leaks a stack trace to an
    unauthenticated caller.
    """
    raw = secret.strip()
    if not raw:
        return None
    try:
        return base64.b64decode(raw.removeprefix(SECRET_PREFIX), validate=True)
    except (binascii.Error, ValueError):
        return None


def compute_signature(
    *,
    payload: bytes,
    message_id: str,
    timestamp: str,
    secret: str,
) -> str | None:
    """The signature Svix should have sent for this delivery.

    Over the exact bytes that arrived. Re-serialising the JSON and signing
    that would verify our own serialiser rather than the provider's.
    """
    key = _hmac_key(secret)
    if key is None:
        return None
    signed = b".".join((message_id.encode("utf-8"), timestamp.encode("utf-8"), payload))
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")


def _is_fresh(timestamp: str, *, tolerance_seconds: int, now: float) -> bool:
    """Whether a delivery's timestamp is close enough to now to be believed.

    Both directions matter. Far in the past is a replay; far in the future is
    a forged timestamp buying an attacker a longer replay window later.
    """
    try:
        sent_at = int(timestamp.strip())
    except ValueError:
        return False
    return abs(now - sent_at) <= tolerance_seconds


def verify_signature(
    *,
    payload: bytes,
    message_id: str | None,
    timestamp: str | None,
    signature_header: str | None,
    secret: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Whether this delivery is genuinely the provider's, and recent.

    A missing header, an unparseable secret, a stale timestamp or an unknown
    signature version all answer False. The default answer to "is this request
    signed?" is no, and every caller-supplied part of it stays hostile until
    the HMAC says otherwise.

    `now` is injectable so the suite can drive the expiry boundary without
    sleeping, the same way the rest of this codebase passes a moment in.
    """
    if not message_id or not timestamp or not signature_header:
        return False

    moment = time.time() if now is None else now
    if not _is_fresh(timestamp, tolerance_seconds=tolerance_seconds, now=moment):
        return False

    expected = compute_signature(
        payload=payload,
        message_id=message_id,
        timestamp=timestamp,
        secret=secret,
    )
    if expected is None:
        return False

    # The header carries a space-separated list so a secret can be rotated
    # without dropping deliveries: any one entry matching is a valid request.
    matched = False
    for candidate in signature_header.split(" "):
        version, _, value = candidate.strip().partition(",")
        if version != SIGNATURE_VERSION or not value:
            continue
        # Deliberately no early return. Every candidate is compared, so the
        # time this takes does not reveal which entry matched or how many
        # were offered.
        if hmac.compare_digest(expected, value):
            matched = True
    return matched


__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "ID_HEADER",
    "SECRET_PREFIX",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "compute_signature",
    "verify_signature",
]
