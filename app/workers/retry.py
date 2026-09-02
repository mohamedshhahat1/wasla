"""What a failed job is, and whether trying it again is safe.

Until now a Redis-queue job had two outcomes: it worked, or it was
dead-lettered. That is the correct behaviour for a document nobody can parse
and the wrong behaviour for a document whose embedding call met a 502, and the
queues could not tell the two apart because nothing classified the failure and
nothing counted the attempt.

Two ideas here, and the second is the one that matters.

**A failure has a category, and the category is a bounded vocabulary.** The
categories below are the whole set. They are written into dead-letter records
and used as metric labels, which is why they are an enum rather than an
exception message: an exception's text can carry a customer's phone number, a
provider's error prose or a fragment of the request that produced it, and none
of those belong in a label or in an operational record that outlives the
incident.

**Retryability is a property of the failure *and* of the operation.** A
category says whether the failure might pass; it cannot say whether repeating
the work is safe. `search_knowledge` reaching a rate limit and a WhatsApp reply
reaching one are the same category and opposite decisions, because re-running
ingestion replaces chunks and re-running an agent turn sends a customer a
second answer. So `RetryPolicy` is chosen per queue by the worker, and the
agent worker narrows its own to `NO_RETRY` the moment a turn engages the
provider (ADR-068).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import httpx
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.core.exceptions import (
    ConflictError,
    DependencyUnavailableError,
    ExternalServiceError,
    NotFoundError,
    PermissionDeniedError,
    PlanLimitExceededError,
    RateLimitedError,
    ValidationError,
)


class FailureCategory(StrEnum):
    """Why a job did not finish, in words safe to publish.

    Deliberately coarse. A finer vocabulary would be more informative in a
    dead-letter record and worse everywhere else: these values are metric
    labels, and a label whose domain grows with the provider's error catalogue
    is a cardinality leak waiting for a bad afternoon.
    """

    MALFORMED = "malformed"
    # The worker holding this job stopped without acknowledging it. Not an
    # error the job raised - nothing raised - which is why it is its own
    # category rather than `unknown`: an operator reading a dead-letter list
    # needs to tell "this job is broken" from "the machine running it went
    # away", because only the second one is worth trying again.
    WORKER_CRASHED = "worker_crashed"
    # A worker died after a turn had engaged the provider, so the outbound
    # message may or may not have been sent. Terminal by construction: the one
    # thing that must not follow is another send (ADR-074).
    UNCERTAIN_DELIVERY = "uncertain_delivery"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    PROVIDER_ERROR = "provider_error"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    INVALID_REQUEST = "invalid_request"
    PERMISSION_DENIED = "permission_denied"
    CONFLICT = "conflict"
    PLAN_LIMIT = "plan_limit"
    UNKNOWN = "unknown"


# Categories a retry could plausibly get past. Everything absent from this set
# fails identically on the next attempt, and retrying it spends the budget that
# a genuinely transient failure needs.
#
# `UNKNOWN` is deliberately *not* here. An exception this module does not
# recognise is one whose safety nobody has argued, and the honest response to
# an unargued failure is to stop and show an operator, not to run it four more
# times. Making a new failure retryable is therefore a deliberate line in the
# map below rather than a default that nobody chose.
RETRYABLE: Final[frozenset[FailureCategory]] = frozenset(
    {
        FailureCategory.DEPENDENCY_UNAVAILABLE,
        FailureCategory.PROVIDER_ERROR,
        FailureCategory.RATE_LIMITED,
        FailureCategory.TIMEOUT,
        # A crash is a machine problem rather than a job problem, so the job
        # deserves another attempt - but it *spends* one, because a crash is
        # still an execution attempt and hiding that would let a job loop
        # through crashes for ever without ever exhausting its budget.
        FailureCategory.WORKER_CRASHED,
    }
)

# Categories that mean "somebody has to look at this", as opposed to "this
# failed". Both are terminal; the difference is what an operator does next,
# and `uncertain_delivery` is the one where the *safe* action may be to do
# nothing at all.
NEEDS_OPERATOR: Final[frozenset[FailureCategory]] = frozenset({FailureCategory.UNCERTAIN_DELIVERY})


def classify(error: BaseException) -> FailureCategory:
    """Name the failure.

    Ordered most specific first, because the application's exception
    hierarchy has subclasses that must not be swallowed by their parents -
    `TenantIsolationError` is a `NotFoundError`, and both are terminal, but a
    reader should be able to see that decided rather than inferred.
    """
    if isinstance(error, DependencyUnavailableError | RedisError):
        return FailureCategory.DEPENDENCY_UNAVAILABLE
    if isinstance(error, RateLimitedError):
        return FailureCategory.RATE_LIMITED
    if isinstance(error, ExternalServiceError):
        return FailureCategory.PROVIDER_ERROR
    if isinstance(error, asyncio.TimeoutError | TimeoutError | httpx.TimeoutException):
        return FailureCategory.TIMEOUT
    if isinstance(error, httpx.TransportError):
        # Connect errors, read errors, pool exhaustion: the network, not the
        # request. Placed after the timeout branch because `TimeoutException`
        # is itself a `TransportError`.
        return FailureCategory.DEPENDENCY_UNAVAILABLE
    if isinstance(error, OperationalError | InterfaceError):
        # A dropped connection, a database restarting, a pool that could not
        # hand one out. SQLAlchemy raises these for the infrastructure rather
        # than for the statement.
        return FailureCategory.DEPENDENCY_UNAVAILABLE
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return FailureCategory.DEPENDENCY_UNAVAILABLE
    if isinstance(error, PlanLimitExceededError):
        return FailureCategory.PLAN_LIMIT
    if isinstance(error, PermissionDeniedError):
        return FailureCategory.PERMISSION_DENIED
    if isinstance(error, ConflictError):
        return FailureCategory.CONFLICT
    if isinstance(error, NotFoundError):
        return FailureCategory.NOT_FOUND
    if isinstance(error, ValidationError):
        return FailureCategory.INVALID_REQUEST
    return FailureCategory.UNKNOWN


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How many times, and how far apart.

    Bounded on both axes, and the bounds are the point. An unbounded attempt
    count turns one poisonous job into a worker that never gets to the next
    one; an unbounded delay turns a Friday outage into retries that land on
    Monday.
    """

    max_attempts: int
    base_seconds: float
    max_seconds: float
    # Added on top of the delay, never subtracted from it: two workers that
    # failed together must not converge on the same next attempt, and a retry
    # that lands *earlier* than the backoff intended is the one thing the
    # backoff was there to prevent.
    jitter_ratio: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_seconds <= 0 or self.max_seconds <= 0:
            raise ValueError("backoff bounds must be positive")
        if self.max_seconds < self.base_seconds:
            raise ValueError("max_seconds must not be below base_seconds")
        if not 0.0 <= self.jitter_ratio < 1.0:
            raise ValueError("jitter_ratio must be in [0, 1)")

    def should_retry(self, category: FailureCategory, *, attempt: int) -> bool:
        """Whether attempt `attempt` may be followed by another one."""
        return category in RETRYABLE and attempt < self.max_attempts

    def delay_for(self, attempt: int, *, jitter: float) -> float:
        """Seconds to wait before attempt `attempt + 1`.

        `jitter` is a fraction in [0, 1) supplied by the caller rather than
        drawn here, so the formula is a pure function and a test can pin it
        without patching `random` or watching a clock.
        """
        if not 0.0 <= jitter < 1.0:
            raise ValueError("jitter must be in [0, 1)")
        delay = float(min(self.base_seconds * (2 ** (attempt - 1)), self.max_seconds))
        return delay + delay * self.jitter_ratio * jitter


# A budget spent quickly, because both queues that use it are in front of a
# customer: a document nobody can search and a photograph nobody has read are
# both somebody waiting. Five attempts starting at two seconds gives roughly
# half a minute of transient tolerance, which covers a provider blip and a
# database failover without covering a provider outage - that is what the
# dead-letter list is for.
IDEMPOTENT_RETRY: Final = RetryPolicy(max_attempts=5, base_seconds=2.0, max_seconds=60.0)

# Refuses to retry at all: attempt 1 is already the last one. Used where the
# work has a side effect that repeating would duplicate.
NO_RETRY: Final = RetryPolicy(max_attempts=1, base_seconds=1.0, max_seconds=1.0)


__all__ = [
    "IDEMPOTENT_RETRY",
    "NEEDS_OPERATOR",
    "NO_RETRY",
    "RETRYABLE",
    "FailureCategory",
    "RetryPolicy",
    "classify",
]
