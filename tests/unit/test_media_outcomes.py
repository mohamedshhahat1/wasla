"""The media reason vocabulary and the horizons derived from configuration.

Both are small and both are load-bearing. A reason sentence that grew past the
column would be the write that fails and strands a conversation (MEDIA-04); a
horizon that drifted from the timeouts it is meant to cover would have the
recovery sweep either fire on live work or never fire at all (MEDIA-03).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import Settings
from app.core.exceptions import WaslaError
from app.db.models.media import MediaStatus
from app.services.extraction import UnreadableDocumentError
from app.services.media_horizons import CLAIM_MARGIN_SECONDS, claim_lease, unclaimed_horizon
from app.services.media_outcomes import (
    MAX_REASON_LENGTH,
    REASON_TEXT,
    SKIPPED_REASONS,
    MediaReason,
    status_for,
    text_for,
)
from app.services.media_reader import (
    DocumentBeyondLimitsError,
    ScannedDocumentError,
    SilentRecordingError,
)
from app.workers.queue import LEASE_RENEWAL_FRACTION
from app.workers.retry import IDEMPOTENT_RETRY

# `message_media.last_error`.
LAST_ERROR_COLUMN = 500


@pytest.mark.parametrize("reason", list(MediaReason))
def test_every_reason_has_a_short_wasla_sentence(reason: MediaReason) -> None:
    sentence = text_for(reason)
    assert sentence
    assert len(sentence) <= MAX_REASON_LENGTH < LAST_ERROR_COLUMN


def test_every_reason_but_a_reader_decision_has_its_own_sentence() -> None:
    assert set(REASON_TEXT) == set(MediaReason) - {MediaReason.UNREADABLE}


@pytest.mark.parametrize(
    "decision",
    [
        SilentRecordingError,
        ScannedDocumentError,
        UnreadableDocumentError,
        DocumentBeyondLimitsError,
    ],
)
def test_a_reader_decision_sentence_fits_as_well(decision: type[WaslaError]) -> None:
    message = decision.message
    assert isinstance(message, str)
    assert len(message) <= MAX_REASON_LENGTH


def test_decisions_are_skipped_and_breakages_are_failed() -> None:
    assert status_for(MediaReason.UNAVAILABLE) is MediaStatus.SKIPPED
    assert status_for(MediaReason.WORKSPACE_SUSPENDED) is MediaStatus.SKIPPED
    assert status_for(MediaReason.TIMEOUT) is MediaStatus.FAILED
    assert status_for(MediaReason.ABANDONED) is MediaStatus.FAILED
    assert all(status_for(reason) is MediaStatus.SKIPPED for reason in SKIPPED_REASONS)


def _defaults() -> Settings:
    return Settings(_env_file=None, environment="test")


def test_the_claim_lease_covers_every_step_of_one_attempt() -> None:
    settings = _defaults()
    steps = (
        settings.media_download_deadline_seconds
        + 2 * settings.media_s3_timeout_seconds
        + settings.media_understanding_deadline_seconds
    )
    assert claim_lease(settings) == timedelta(seconds=steps + CLAIM_MARGIN_SECONDS)
    # 90 + 60 + 120 + 60 at the defaults: five and a half minutes.
    assert claim_lease(settings) == timedelta(seconds=330)


def test_the_claim_lease_moves_with_the_timeouts_it_covers() -> None:
    longer = Settings(
        _env_file=None,
        environment="test",
        media_understanding_deadline_seconds=300.0,
    )
    assert claim_lease(longer) - claim_lease(_defaults()) == timedelta(seconds=180)


def test_the_unclaimed_horizon_is_the_whole_queue_retry_budget() -> None:
    settings = _defaults()
    per_attempt = settings.queue_visibility_timeout_seconds * (
        1 + LEASE_RENEWAL_FRACTION
    ) + IDEMPOTENT_RETRY.max_seconds * (1 + IDEMPOTENT_RETRY.jitter_ratio)
    assert unclaimed_horizon(settings) == timedelta(
        seconds=per_attempt * IDEMPOTENT_RETRY.max_attempts
    )
    assert unclaimed_horizon(settings) > claim_lease(settings)


def test_one_download_ends_inside_the_lease_its_job_was_reserved_under() -> None:
    """The relationship MEDIA-10 needs, at the defaults: 90 s < 120 s."""
    settings = _defaults()
    assert settings.media_download_deadline_seconds < settings.queue_visibility_timeout_seconds
