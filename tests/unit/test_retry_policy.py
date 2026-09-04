"""Which failures earn another attempt, and how far apart the attempts land.

The policy is a pure function of the category, the attempt number and a jitter
fraction the caller supplies, which is what lets these tests pin exact delays
without patching `random` or watching a clock. Production draws the fraction in
`handle_failure`; nothing here does.
"""

from typing import Any

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
from app.workers.ai_worker import AGENT_RETRY
from app.workers.retry import (
    FIRST_ATTEMPT_TRANSIENT,
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
def test_a_failure_is_named(error: Exception, expected: FailureCategory) -> None:
    assert classify(error) is expected


def test_an_unrecognised_failure_is_not_retried() -> None:
    """An exception nobody has argued about is one nobody has argued is safe.

    A bug that raises `AttributeError` would fail identically on every attempt,
    and spending the budget on it is the budget a real transient needs.
    """
    assert FailureCategory.UNKNOWN not in RETRYABLE
    assert not IDEMPOTENT_RETRY.should_retry(FailureCategory.UNKNOWN, attempt=1)


def test_a_malformed_payload_is_never_retried() -> None:
    assert FailureCategory.MALFORMED not in RETRYABLE


@pytest.mark.parametrize("category", sorted(RETRYABLE))
def test_every_retryable_category_is_retried_while_budget_remains(
    category: FailureCategory,
) -> None:
    assert IDEMPOTENT_RETRY.should_retry(category, attempt=1)


@pytest.mark.parametrize(
    "category",
    sorted(set(FailureCategory) - RETRYABLE - FIRST_ATTEMPT_TRANSIENT),
)
def test_a_terminal_category_is_never_retried_however_much_budget_remains(
    category: FailureCategory,
) -> None:
    assert not IDEMPOTENT_RETRY.should_retry(category, attempt=1)


# ------------------------------------------------- the one-shot door, WSL-02


def test_not_found_is_not_globally_retryable() -> None:
    """The whole point of scoping it to a policy and to one attempt.

    A `not_found` that is genuinely not found fails identically forever, and
    putting it in `RETRYABLE` would spend a transient failure's budget on a row
    that is never coming back.
    """
    assert FailureCategory.NOT_FOUND not in RETRYABLE


@pytest.mark.parametrize("policy", [IDEMPOTENT_RETRY, AGENT_RETRY])
def test_a_row_that_may_be_committing_earns_exactly_one_more_look(
    policy: RetryPolicy,
) -> None:
    """Attempt one asks again; attempt two is the answer.

    Both queues are enqueued from inside the transaction that created the row
    the job names, so the first miss may be a commit that has not landed. The
    second miss cannot be - two seconds is three orders of magnitude wider than
    the window - so it stops there.
    """
    assert policy.should_retry(FailureCategory.NOT_FOUND, attempt=1)
    assert not policy.should_retry(FailureCategory.NOT_FOUND, attempt=2)
    assert not policy.should_retry(FailureCategory.NOT_FOUND, attempt=3)


def test_the_one_shot_door_is_shut_unless_a_policy_opens_it() -> None:
    """Default-empty, so a policy has to ask rather than inherit."""
    plain = RetryPolicy(max_attempts=5, base_seconds=1.0, max_seconds=10.0)

    assert plain.first_attempt_transient == frozenset()
    assert not plain.should_retry(FailureCategory.NOT_FOUND, attempt=1)


def test_the_refusing_policy_never_acquires_the_door() -> None:
    """`NO_RETRY` is what a turn that already engaged a provider gets.

    A second attempt there is a second reply on a customer's phone, and no
    lookup answering "not found" afterwards changes that (ADR-068).
    """
    assert NO_RETRY.first_attempt_transient == frozenset()
    assert not NO_RETRY.should_retry(FailureCategory.NOT_FOUND, attempt=1)


def test_the_door_is_one_category_wide() -> None:
    """Stated as a set, so widening it is a deliberate edit with a test to fix."""
    assert frozenset({FailureCategory.NOT_FOUND}) == FIRST_ATTEMPT_TRANSIENT


def test_the_budget_still_bounds_the_one_shot_door() -> None:
    """A policy of one attempt offers nothing, whatever doors it carries."""
    single = RetryPolicy(
        max_attempts=1,
        base_seconds=1.0,
        max_seconds=10.0,
        first_attempt_transient=FIRST_ATTEMPT_TRANSIENT,
    )

    assert not single.should_retry(FailureCategory.NOT_FOUND, attempt=1)


def test_attempts_are_bounded() -> None:
    """The gap this whole module exists to close: a poison job must stop."""
    policy = RetryPolicy(max_attempts=3, base_seconds=1.0, max_seconds=10.0)

    assert policy.should_retry(FailureCategory.TIMEOUT, attempt=2)
    assert not policy.should_retry(FailureCategory.TIMEOUT, attempt=3)
    assert not policy.should_retry(FailureCategory.TIMEOUT, attempt=99)


def test_no_retry_refuses_the_very_first_attempt() -> None:
    assert not NO_RETRY.should_retry(FailureCategory.TIMEOUT, attempt=1)


def test_the_backoff_doubles() -> None:
    policy = RetryPolicy(max_attempts=6, base_seconds=2.0, max_seconds=1000.0, jitter_ratio=0.0)

    delays = [policy.delay_for(attempt, jitter=0.0) for attempt in range(1, 5)]

    assert delays == [2.0, 4.0, 8.0, 16.0]


def test_the_backoff_stops_growing() -> None:
    """A Friday outage must not push a retry into next week."""
    policy = RetryPolicy(max_attempts=20, base_seconds=2.0, max_seconds=60.0, jitter_ratio=0.0)

    assert policy.delay_for(20, jitter=0.0) == 60.0


def test_jitter_only_ever_adds() -> None:
    """A retry landing earlier than the backoff intended defeats the backoff."""
    policy = RetryPolicy(max_attempts=5, base_seconds=10.0, max_seconds=100.0, jitter_ratio=0.25)

    assert policy.delay_for(1, jitter=0.0) == 10.0
    assert policy.delay_for(1, jitter=0.999) == pytest.approx(12.4975)
    assert policy.delay_for(1, jitter=0.5) > policy.delay_for(1, jitter=0.0)


def test_jitter_is_bounded_by_the_ratio() -> None:
    policy = RetryPolicy(max_attempts=5, base_seconds=8.0, max_seconds=100.0, jitter_ratio=0.25)

    for step in range(100):
        delay = policy.delay_for(1, jitter=step / 100)
        assert 8.0 <= delay <= 10.0


def test_a_jitter_outside_the_unit_interval_is_refused() -> None:
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
def test_an_unusable_policy_is_refused_at_construction(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_the_shipped_policies_are_bounded() -> None:
    """Stated as a property, so raising a limit is a deliberate edit here too."""
    for policy in (IDEMPOTENT_RETRY, NO_RETRY, AGENT_RETRY):
        assert 1 <= policy.max_attempts <= 10
        assert policy.max_seconds <= 3600.0


def test_one_retry_covers_the_commit_window_by_three_orders_of_magnitude() -> None:
    """Why *one* retry is enough, stated as an arithmetic fact.

    The producer's commit is one round trip and one fsync after the enqueue -
    measured at 25-75 ms against a containerised PostgreSQL in
    `test_commit_boundary.py`. The shortest possible retry delay on either
    policy is two seconds, which is more than twenty-five times the widest
    figure ever measured. A row still missing then was never committing.
    """
    for policy in (IDEMPOTENT_RETRY, AGENT_RETRY):
        assert policy.delay_for(1, jitter=0.0) >= 2.0
