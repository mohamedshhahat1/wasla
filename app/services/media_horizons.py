"""How long a media attempt can legitimately take, derived from configuration.

Two numbers decide when an unresolved file stops being "in progress" and starts
being "stranded" (MEDIA-03), and both are computed from the settings that bound
the work rather than chosen beside them. A horizon guessed independently drifts
the first time somebody lengthens a timeout, and then the recovery sweep either
fires on work that is still running or waits for ever on work that is not.

**The claim lease** is the longest one attempt can hold a file: the whole of a
download under its deadline, the object write, the read back, and understanding
under its own deadline, plus a margin for the database round trips between
them. A claim older than this belongs to an attempt that is not coming back.

**The unclaimed horizon** is how long a file can wait for its first claim while
its job is still alive somewhere in the queue: every attempt the queue's retry
policy allows, each reclaimed from a dead worker one visibility timeout (and one
recovery pass) after its lease lapsed, each followed by the longest backoff.
A file unclaimed for longer than that has a job nobody is holding.

The download deadline is also held below the visibility timeout at start-up
(`Settings._validate_media_deadlines`), so one download cannot outlast the lease
its job was reserved under.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from app.core.config import Settings
from app.workers.queue import LEASE_RENEWAL_FRACTION
from app.workers.retry import IDEMPOTENT_RETRY

# Database round trips, scheduling and the few statements between network
# calls. Generous on purpose: the cost of a lease slightly too long is a
# stranded file noticed a minute later; the cost of one too short is a live
# attempt treated as dead.
CLAIM_MARGIN_SECONDS: Final = 60.0


def claim_lease(settings: Settings) -> timedelta:
    """The longest one attempt at a file may hold its claim."""
    seconds = (
        settings.media_download_deadline_seconds
        # The object write, then the read back before understanding.
        + settings.media_s3_timeout_seconds * 2
        + settings.media_understanding_deadline_seconds
        + CLAIM_MARGIN_SECONDS
    )
    return timedelta(seconds=seconds)


def unclaimed_horizon(settings: Settings) -> timedelta:
    """How long a file may wait for a claim before its job is presumed lost."""
    visibility = settings.queue_visibility_timeout_seconds
    reclaim = visibility * (1 + LEASE_RENEWAL_FRACTION)
    longest_backoff = IDEMPOTENT_RETRY.max_seconds * (1 + IDEMPOTENT_RETRY.jitter_ratio)
    return timedelta(seconds=(reclaim + longest_backoff) * IDEMPOTENT_RETRY.max_attempts)


__all__ = ["CLAIM_MARGIN_SECONDS", "claim_lease", "unclaimed_horizon"]
