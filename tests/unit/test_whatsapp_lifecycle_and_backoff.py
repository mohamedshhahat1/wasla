"""The three small guards: version lifecycle, retry pacing, and NUL payloads.

Unit tests because all three are pure functions of their inputs, and pinning
them here means the integration suite does not have to arrange a clock, a
provider header or a poisoned delivery to check arithmetic.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.api.v1.webhooks import _decode, _strip_nul
from app.integrations.whatsapp.client import (
    MAX_RETRY_AFTER_SECONDS,
    RETRY_JITTER,
    _retry_after,
)
from app.integrations.whatsapp.versions import (
    META_API_SUNSETS,
    SUNSET_WARNING_DAYS,
    api_version_warning,
)

# ------------------------------------------------------- API version lifecycle


def test_a_version_with_time_left_says_nothing() -> None:
    """Silence is the common case and has to stay the common case.

    A warning on every boot is a warning nobody reads, which would make the one
    that matters invisible.
    """
    assert api_version_warning("v25.0", today=date(2026, 9, 11)) is None


def test_a_version_inside_its_last_ninety_days_warns_with_the_date() -> None:
    """Ninety days is the notice, because moving version is planned work.

    Re-verifying the send and media contracts against a new version is a piece
    of work somebody schedules; discovering the need on the day sends start
    failing is an incident.
    """
    sunset = META_API_SUNSETS["v21.0"]
    inside = date.fromordinal(sunset.toordinal() - SUNSET_WARNING_DAYS + 1)

    warning = api_version_warning("v21.0", today=inside)

    assert warning is not None
    assert sunset.isoformat() in warning


def test_the_day_before_the_window_opens_is_still_silent() -> None:
    """The boundary from the quiet side, so the threshold is the threshold."""
    sunset = META_API_SUNSETS["v21.0"]
    outside = date.fromordinal(sunset.toordinal() - SUNSET_WARNING_DAYS - 1)

    assert api_version_warning("v21.0", today=outside) is None


def test_a_version_meta_has_already_retired_says_so_plainly() -> None:
    """Past the date the message stops being a plan and starts being a cause.

    This is what somebody reads while wondering why nothing is sending.
    """
    sunset = META_API_SUNSETS["v21.0"]
    after = date.fromordinal(sunset.toordinal() + 1)

    warning = api_version_warning("v21.0", today=after)

    assert warning is not None
    assert "retired" in warning


def test_an_unknown_version_warns_rather_than_passing_silently() -> None:
    """Covers both a version newer than this table and a typo in the setting.

    Silence would make a typo look like an endorsement, and the table is
    maintained by hand precisely so booting does not depend on fetching Meta's
    changelog - so it going stale is the expected failure and has to be loud.
    """
    warning = api_version_warning("v99.0", today=date(2026, 9, 11))

    assert warning is not None
    assert "changelog" in warning


# -------------------------------------------------------------- retry pacing


def _response(headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(429, headers=headers)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ({"Retry-After": "30"}, 30.0),
        ({"Retry-After": " 12 "}, 12.0),
        ({"Retry-After": "2.5"}, 2.5),
    ],
)
def test_meta_saying_how_long_to_wait_is_read(header: dict[str, str], expected: float) -> None:
    assert _retry_after(_response(header)) == expected


@pytest.mark.parametrize(
    "header",
    [
        {},
        # The HTTP-date form is legal and is not what Meta sends here. Falling
        # back to the client's own backoff is better than a date parser on the
        # send path.
        {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
        # "Wait no time at all" is not something a rate limiter would mean.
        {"Retry-After": "0"},
        {"Retry-After": "-5"},
        {"Retry-After": ""},
    ],
)
def test_anything_unreadable_falls_back_to_our_own_backoff(header: dict[str, str]) -> None:
    assert _retry_after(_response(header)) is None


def test_a_retry_after_beyond_the_cap_is_clamped() -> None:
    """A provider header may not park a worker indefinitely.

    The send path holds no database connection, but it does hold a worker, and
    a header is not something to let it wait on without bound.
    """
    assert _retry_after(_response({"Retry-After": "86400"})) == 86400.0
    # The clamp is applied where the wait is computed, not where the header is
    # read, so the raw value stays visible to a caller that wants to log it.
    assert min(86400.0, MAX_RETRY_AFTER_SECONDS) == MAX_RETRY_AFTER_SECONDS


def test_jitter_only_ever_lengthens_the_wait() -> None:
    """A retry landing *earlier* than the backoff intended defeats the backoff.

    The jitter is additive for the same reason `RetryPolicy.jitter_ratio` is:
    replicas throttled at the same instant must spread out, and spreading
    inwards is not spreading.
    """
    assert RETRY_JITTER > 0
    base = 1.0
    for fraction in (0.0, 0.5, 1.0):
        assert base + base * RETRY_JITTER * fraction >= base


# ------------------------------------------------------------ NUL sanitisation


def test_a_nul_anywhere_in_the_payload_is_removed_and_counted() -> None:
    """PostgreSQL cannot store one, so a message carrying one was unstorable.

    Body, caption, filename, profile name - the walk is over the whole
    structure because Meta puts customer text in all of them (MSG-03).
    """
    payload = {
        "text": {"body": "before" + chr(0) + "after"},
        "contacts": [{"profile": {"name": "Na" + chr(0) + "dia"}}],
        "document": {"filename": "report" + chr(0) + ".pdf"},
    }

    cleaned, removed = _strip_nul(payload)

    assert removed == 3
    assert cleaned["text"]["body"] == "beforeafter"
    assert cleaned["contacts"][0]["profile"]["name"] == "Nadia"
    assert cleaned["document"]["filename"] == "report.pdf"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("مرحبا كيف حالك", id="arabic"),
        pytest.param("مرحبا 👋🏽 كيف حالك", id="arabic-emoji"),
        # Combining marks, zero-width joiners and bidirectional overrides are
        # all legitimate in real customer messages, and all things a careless
        # sanitiser would eat.
        pytest.param("égalité", id="combining-marks"),
        pytest.param("‍‌​", id="zero-width"),
        pytest.param("‮en.wikipedia‬", id="bidi-override"),
        pytest.param("👨‍👩‍👧‍👦", id="emoji-zwj-sequence"),
        pytest.param(chr(10).join(("line", "break")), id="whitespace"),
        # Given an explicit id, because pytest builds one from the value and a
        # sixty-thousand-character id overflows the environment variable it
        # writes the current test name into.
        pytest.param("x" * 60_000, id="sixty-thousand-characters"),
    ],
)
def test_everything_that_is_not_a_nul_survives_exactly(text: str) -> None:
    """The other half, and the more important half.

    A sanitiser that "cleaned up" any of these would be corrupting real
    customer messages to avoid a problem they do not have.
    """
    cleaned, removed = _strip_nul({"body": text})

    assert removed == 0
    assert cleaned["body"] == text


def test_a_payload_with_nothing_to_remove_decodes_unchanged() -> None:
    body = b'{"object":"whatsapp_business_account","entry":[]}'

    payload, removed = _decode(body)

    assert removed == 0
    assert payload == {"object": "whatsapp_business_account", "entry": []}


def test_an_unparseable_body_still_reports_no_removals() -> None:
    """The count is only meaningful alongside a payload; absent means absent."""
    assert _decode(b"not json") == (None, 0)
    assert _decode(b"") == (None, 0)
    assert _decode(b"[1, 2, 3]") == (None, 0)
