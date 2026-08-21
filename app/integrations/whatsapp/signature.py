"""Meta webhook payload signatures.

The signature is computed over the exact bytes Meta sent. Re-serialising the
JSON first and signing that would prove nothing, since the re-serialisation is
ours and not theirs.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="


def compute_signature(*, payload: bytes, app_secret: str) -> str:
    """Return the header value Meta should have sent for this body."""
    digest = hmac.new(app_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(*, payload: bytes, header: str | None, app_secret: str) -> bool:
    """Whether `header` is a valid signature of `payload`.

    A missing header or an unconfigured secret is a failure, never a pass: the
    caller decides what to do about misconfiguration, and the default answer to
    "is this request signed?" must be no.

    The comparison is constant-time so that response timing cannot be used to
    discover the expected signature one character at a time.
    """
    if not header or not app_secret:
        return False

    expected = compute_signature(payload=payload, app_secret=app_secret)
    return hmac.compare_digest(expected, header.strip())
