"""Constant-time comparison of a secret the caller supplied.

`hmac.compare_digest(str, str)` raises `TypeError` when either string contains
a non-ASCII character (SEC-03). Every public authenticity check in this
application compares a value an unauthenticated caller chose - a signature
header, a query parameter, a verify token - so a single `é` turned a refused
forgery into an unhandled 500: the request was still refused, but as a crash
that paged operators through the 5xx alert instead of as a counted
`invalid_signature`.

So the comparison happens on **bytes**, where `compare_digest` accepts any
content. Both sides are UTF-8 encoded; a value that cannot be encoded at all (a
lone surrogate) simply does not match. Nothing about the constant-time property
changes: it still depends only on the lengths, never on where the first
difference is.

This is the one place authenticity comparisons go through.
`tests/unit/test_constant_time_comparison.py` fails if an authenticity module
compares a secret any other way.
"""

from __future__ import annotations

import hmac


def _encoded(value: str) -> bytes | None:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        return None


def secrets_match(expected: str, supplied: str | None) -> bool:
    """Whether `supplied` equals `expected`, in constant time, never raising.

    `expected` is ours - a digest we computed or a token we configured.
    `supplied` is the caller's and may be anything, including absent.
    """
    if supplied is None:
        return False
    ours = _encoded(expected)
    theirs = _encoded(supplied)
    if ours is None or theirs is None:
        return False
    return hmac.compare_digest(ours, theirs)
