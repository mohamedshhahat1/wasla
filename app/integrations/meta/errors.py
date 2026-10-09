"""What a Meta Graph API error means, by Meta's own code first (OMNI-035, ADR-130).

Every Meta product Wasla sends through - WhatsApp Cloud API today, Instagram and
Messenger next - answers a failed request with the same Graph error envelope,
`{"error": {"code", "type", "error_subcode", ...}}`. The WhatsApp client used to
classify by HTTP status alone: only a 429 was a rate limit and only a 401 or code
190 a credential failure, so Meta's documented throttling codes and
connection-level refusals - which Meta does not promise to send with any
particular status - became per-message declines. A campaign then marked each
recipient failed instead of backing off, or burned its whole audience one
recipient at a time on a revoked permission with the connection reading healthy.

The table follows Meta's error-code reference (WhatsApp Cloud API "Error codes"
and "Throughput", read 2026-10-02; OMNICHANNEL_READINESS_AUDIT_FINAL.md section 4a,
M21/M22). The HTTP status is the fallback for a body that names no code - except a
401, which is a refused credential whatever code accompanies it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class MetaErrorClass(StrEnum):
    """What a refused request tells the sender to do. Bounded: it is a metric label."""

    #: Slow down and try later: nothing was sent.
    THROTTLED = "throttled"
    #: The credential itself is refused: stop until it is replaced.
    CREDENTIAL = "credential"
    #: The connection cannot send at all - a permission revoked, an account
    #: restricted, a number not registered: stop until somebody fixes it.
    CONNECTION = "connection"
    #: This one message was declined; the next one may well go.
    PER_MESSAGE = "per_message"


#: Rate limits and throughput: 4 (application), 80007 (WhatsApp Business
#: Account), 130429 (Cloud API throughput, "the API will return error code
#: 130429"), 131056 (pair rate limit) and 131057 (account in maintenance while a
#: throughput upgrade completes - a wait, not a refusal).
THROTTLING_CODES: Final[frozenset[int]] = frozenset({4, 80007, 130429, 131056, 131057})

#: The access token is refused: 0 (`AuthException`, "unable to authenticate"
#: - an expired or invalidated token) and 190 (access token invalid or expired).
CREDENTIAL_CODES: Final[frozenset[int]] = frozenset({0, 190})

#: Refusals of the connection rather than the message: 3 (capability), 10
#: (permission denied), 200-299 (API permissions), 131005 (access denied), 368
#: (temporarily blocked for policy violations), 131031 (account locked or
#: restricted), 133010 (phone number not registered).
CONNECTION_CODES: Final[frozenset[int]] = frozenset(
    {3, 10, 131005, 368, 131031, 133010, *range(200, 300)}
)

TOO_MANY_REQUESTS: Final = 429
UNAUTHORIZED: Final = 401


def classify_meta_error(status_code: int, code: int | None) -> MetaErrorClass:
    """Meta's code first; the HTTP status only for a body that names none."""
    if code is not None and code in THROTTLING_CODES:
        return MetaErrorClass.THROTTLED
    if (code is not None and code in CREDENTIAL_CODES) or status_code == UNAUTHORIZED:
        # A 401 is unambiguous on its own. A bare 403 is not: Meta uses it for
        # permission problems that are sometimes about one message.
        return MetaErrorClass.CREDENTIAL
    if code is not None and code in CONNECTION_CODES:
        return MetaErrorClass.CONNECTION
    if status_code == TOO_MANY_REQUESTS:
        return MetaErrorClass.THROTTLED
    return MetaErrorClass.PER_MESSAGE


__all__ = [
    "CONNECTION_CODES",
    "CREDENTIAL_CODES",
    "THROTTLING_CODES",
    "MetaErrorClass",
    "classify_meta_error",
]
