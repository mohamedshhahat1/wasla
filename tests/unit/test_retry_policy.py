"""Which failures earn another attempt, and how far apart the attempts land.

The policy is a pure function of the category, the attempt number and a jitter
fraction the caller supplies, which is what lets these tests pin exact delays
without patching `random` or watching a clock. Production draws the fraction in
`handle_failure`; nothing here does.
"""

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.exceptions import (
    ConflictError,
    DependencyUnavailableError,
    ExternalServiceError,
    NotFoundError,
    PermissionDeniedError,
    PlanLimitExceededError,
    RateLimitedError,
    TenantIsolationError,
    ValidationError,
)
from app.workers.retry import (
    IDEMPOTENT_RETRY,
    NO_RETRY,
    RETRYABLE,
    FailureCategory,
    RetryPolicy,
    classify,
)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            DependencyUnavailableError("no"),
            FailureCategory.DEPENDENCY_UNAVAILABLE,
            id="dependency",
        ),
        pytest.param(
            RedisConnectionError("no"), FailureCategory.DEPENDENCY_UNAVAILABLE, id="redis"
        ),
        pytest.param(
            OperationalError("SELECT 1", {}, Exception("gone")),
            FailureCategory.DEPENDENCY_UNAVAILABLE,
            id="database-connection",
        ),
        pytest.param(
            httpx.ConnectError("refused"),
            FailureCategory.DEPENDENCY_UNAVAILABLE,
            id="connect",
        ),
        pytest.param(RateLimitedError("slow down"), FailureCategory.RATE_LIMITED, id="429"),
        pytest.param(ExternalServiceError("502"), FailureCategory.PROVIDER_ERROR, id="provider"),
        pytest.param(TimeoutError(), FailureCategory.TIMEOUT, id="timeout"),
        pytest.param(TimeoutError(), FailureCategory.TIMEOUT, id="asyncio-timeout"),
        pytest.param(httpx.ReadTimeout("slow"), FailureCategory.TIMEOUT, id="httpx-timeout"),
        pytest.param(NotFoundError("gone"), FailureCategory.NOT_FOUND, id="not-found"),
        pytest.param(
            TenantIsolationError("elsewhere"), FailureCategory.NOT_FOUND, id="cross-tenant"
        ),
        pytest.param(ValidationError("bad"), FailureCategory.INVALID_REQUEST, id="invalid"),
        pytest.param(PermissionDeniedError("no"), FailureCategory.PERMISSION_DENIED, id="denied"),
        pytest.param(ConflictError("clash"), FailureCategory.CONFLICT, id="conflict"),
        pytest.param(PlanLimitExceededError("upgrade"), FailureCategory.PLAN_LIMIT, id="plan"),
        pytest.param(ZeroDivisionError(), FailureCategory.UNKNOWN, id="a-bug"),
        pytest.param(
            IntegrityError("INSERT", {}, Exception("duplicate")),
            FailureCategory.UNKNOWN,
            id="constraint",
        ),
    ],
)
def test_a_failure_is_named(error, expected):
    assert classify(error) is expected


def test_an_unrecognised_failure_is_not_retried():
    """An exception nobody has argued about is one nobody has argued is safe.

    A bug that raises `AttributeError` would fail identically on every attempt,
    and spending the budget on it is the budget a real transient needs.
    """
    assert FailureCategory.UNKNOWN not in RETRYABLE
    assert not IDEMPOTENT_RETRY.should_retry(FailureCategory.UNKNOWN, attempt=1)


def test_a_malformed_payload_is_never_retried():
    assert FailureCategory.MALFORMED not in RETRYABLE


@pytest.mark.parametrize("category", sorted(RETRYABLE))
def test_every_retryable_category_is_retried_while_budget_remains(category):
    assert IDEMPOTENT_RETRY.should_retry(category, attempt=1)


@pytest.mark.parametrize(
    "category",
    sorted(set(FailureCategory) - RETRYABLE),
)
def test_a_terminal_category_is_never_retried_however_much_budget_remains(category):
    assert not IDEMPOTENT_RETRY.should_retry(category, attempt=1)


def test_attempts_are_bounded():
    """The gap this whole module exists to close: a poison job must stop."""
    policy = RetryPolicy(max_attempts=3, base_seconds=1.0, max_seconds=10.0)

    assert policy.should_retry(FailureCategory.TIMEOUT, attempt=2)
    assert not policy.should_retry(FailureCategory.TIMEOUT, attempt=3)
    assert not policy.should_retry(FailureCategory.TIMEOUT, attempt=99)


def test_no_retry_refuses_the_very_first_attempt():
    assert not NO_RETRY.should_retry(FailureCategory.TIMEOUT, attempt=1)


def test_the_backoff_doubles():
    policy = RetryPolicy(max_attempts=6, base_seconds=2.0, max_seconds=1000.0, jitter_ratio=0.0)

    delays = [policy.delay_for(attempt, jitter=0.0) for attempt in range(1, 5)]

    assert delays == [2.0, 4.0, 8.0, 16.0]


def test_the_backoff_stops_growing():
    """A Friday outage must not push a retry into next week."""
    policy = RetryPolicy(max_attempts=20, base_seconds=2.0, max_seconds=60.0, jitter_ratio=0.0)

    assert policy.delay_for(20, jitter=0.0) == 60.0


def test_jitter_only_ever_adds():
    """A retry landing earlier than the backoff intended defeats the backoff."""
    policy = RetryPolicy(max_attempts=5, base_seconds=10.0, max_seconds=100.0, jitter_ratio=0.25)

    assert policy.delay_for(1, jitter=0.0) == 10.0
    assert policy.delay_for(1, jitter=0.999) == pytest.approx(12.4975)
    assert policy.delay_for(1, jitter=0.5) > policy.delay_for(1, jitter=0.0)


def test_jitter_is_bounded_by_the_ratio():
    policy = RetryPolicy(max_attempts=5, base_seconds=8.0, max_seconds=100.0, jitter_ratio=0.25)

    for step in range(100):
        delay = policy.delay_for(1, jitter=step / 100)
        assert 8.0 <= delay <= 10.0


def test_a_jitter_outside_the_unit_interval_is_refused():
    with pytest.raises(ValueError, match="jitter"):
        IDEMPOTENT_RETRY.delay_for(1, jitter=1.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"max_attempts": 0, "base_seconds": 1.0, "max_seconds": 2.0}, id="no-attempt"),
        pytest.param({"max_attempts": 3, "base_seconds": 0.0, "max_seconds": 2.0}, id="zero-base"),
        pytest.param(
            {"max_attempts": 3, "base_seconds": 10.0, "max_seconds": 2.0}, id="ceiling-below-floor"
        ),
        pytest.param(
            {"max_attempts": 3, "base_seconds": 1.0, "max_seconds": 2.0, "jitter_ratio": 1.0},
            id="jitter-out-of-range",
        ),
    ],
)
def test_an_unusable_policy_is_refused_at_construction(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_the_shipped_policies_are_bounded():
    """Stated as a property, so raising a limit is a deliberate edit here too."""
    for policy in (IDEMPOTENT_RETRY, NO_RETRY):
        assert 1 <= policy.max_attempts <= 10
        assert policy.max_seconds <= 3600.0
