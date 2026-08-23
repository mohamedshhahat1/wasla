"""What each rate limit does when Redis cannot answer.

The policy (ADR-040) turns on the distinction between two kinds of limit that
look identical in code.

A limit on how much shared capacity a signed-in workspace may consume is an
**availability** control. When Redis is gone, allowing the request is right:
refusing a paying customer's colleagues in order to protect capacity that is not
currently contended *is* the outage.

A limit in front of a credential is a **security** control. Allowing it means
unlimited password attempts for as long as the outage lasts, and it is the only
anti-automation control on `/auth/login`. Those policies fall back to counting
in this process - weaker than a shared counter, and enormously stronger than
nothing.

Failing closed on Redis was considered and rejected. It makes signing in
impossible whenever the cache is down, which is worse for customers *and*
attacker-triggerable by anyone who can degrade Redis: it would convert a
denial-of-service against a cache into a denial-of-service against
authentication.
"""

from __future__ import annotations

import uuid

import pytest
from redis.exceptions import AuthenticationError as RedisAuthError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeout

from app.core.rate_limit import (
    LOGIN_ACCOUNT_POLICY,
    RateLimiter,
    RateLimitPolicy,
    _LocalWindows,
)
from app.core.token_store import RefreshTokenStore

FAILURES = [
    pytest.param(RedisConnectionError("refused"), id="connection-refused"),
    pytest.param(RedisTimeout("slow"), id="timeout"),
    pytest.param(RedisAuthError("NOAUTH Authentication required."), id="auth-failure"),
]


class BrokenCommands:
    """Every command this application issues, all of them failing."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def incr(self, *args: object, **kwargs: object) -> int:
        raise self._error

    async def expire(self, *args: object, **kwargs: object) -> bool:
        raise self._error

    async def ttl(self, *args: object, **kwargs: object) -> int:
        raise self._error

    async def set(self, *args: object, **kwargs: object) -> bool:
        raise self._error

    async def exists(self, *args: object, **kwargs: object) -> int:
        raise self._error


class BrokenRedis:
    def __init__(self, error: Exception) -> None:
        self.client = BrokenCommands(error)


@pytest.fixture(autouse=True)
def _isolated_windows(monkeypatch):
    """A fresh process-local counter per test.

    The real one is module-level on purpose - a limiter is constructed per
    request, so counters held on the instance would never accumulate - which
    means tests have to reset it or they leak into each other.
    """
    monkeypatch.setattr("app.core.rate_limit._LOCAL_WINDOWS", _LocalWindows())


def _security_policy(limit: int = 3) -> RateLimitPolicy:
    return RateLimitPolicy(
        name=LOGIN_ACCOUNT_POLICY, limit=limit, window_seconds=60, local_fallback=True
    )


def _availability_policy(limit: int = 3) -> RateLimitPolicy:
    return RateLimitPolicy(name="workspace", limit=limit, window_seconds=60)


# ------------------------------------------------------- availability controls


@pytest.mark.parametrize("error", FAILURES)
async def test_a_capacity_limit_allows_when_redis_is_gone(error):
    """Shedding a signed-in colleague's request to protect uncontended capacity
    is the outage, not the defence."""
    limiter = RateLimiter(BrokenRedis(error))
    policy = _availability_policy(limit=1)

    for _ in range(20):
        decision = await limiter.check(policy, "tenant-a")
        assert decision.allowed is True


# ---------------------------------------------------------- security controls


@pytest.mark.parametrize("error", FAILURES)
async def test_a_credential_limit_still_bounds_attempts_when_redis_is_gone(error):
    """The gap this closes: before the fallback, every one of these was allowed.

    A limiter is constructed per request in production, so the counter has to
    outlive the instance - which is why the fallback windows are module-level
    and why this test builds a new limiter each time round.
    """
    policy = _security_policy(limit=3)
    outcomes = [
        (await RateLimiter(BrokenRedis(error)).check(policy, "victim@example.com")).allowed
        for _ in range(6)
    ]

    assert outcomes == [True, True, True, False, False, False]


async def test_the_local_fallback_does_not_leak_between_identities():
    """One attacker exhausting their own budget must not lock anybody else out."""
    limiter = RateLimiter(BrokenRedis(RedisConnectionError("down")))
    policy = _security_policy(limit=2)

    for _ in range(4):
        await limiter.check(policy, "attacker")
    for_victim = await limiter.check(policy, "victim")

    assert for_victim.allowed is True


async def test_the_local_fallback_does_not_leak_between_policies():
    """The policy name is part of the key, so a spent login budget must not
    also refuse a refresh."""
    limiter = RateLimiter(BrokenRedis(RedisConnectionError("down")))
    login = RateLimitPolicy(name="auth", limit=1, window_seconds=60, local_fallback=True)
    account = _security_policy(limit=1)

    await limiter.check(login, "1.2.3.4")
    await limiter.check(login, "1.2.3.4")

    assert (await limiter.check(account, "1.2.3.4")).allowed is True


async def test_a_refusal_tells_the_caller_when_to_come_back():
    """A client refused without a `Retry-After` simply retries immediately,
    which is the traffic the limit exists to stop."""
    limiter = RateLimiter(BrokenRedis(RedisConnectionError("down")))
    policy = _security_policy(limit=1)

    await limiter.check(policy, "someone")
    refused = await limiter.check(policy, "someone")

    assert refused.allowed is False
    assert refused.headers["Retry-After"] == "60"


# ------------------------------------------------------------ the window itself


def test_the_window_resets_once_it_has_elapsed():
    """A fixed window, so a caller refused now is served again later. Without
    this the fallback would be a permanent lockout after a burst."""
    windows = _LocalWindows()

    assert windows.hit("k", 60, now=1000.0) == 1
    assert windows.hit("k", 60, now=1030.0) == 2
    assert windows.hit("k", 60, now=1061.0) == 1


def test_the_window_store_stays_bounded():
    """Unbounded, this would be a memory exhaustion primitive: an attacker
    rotating source addresses during an outage decides how much it holds."""
    windows = _LocalWindows(capacity=50)

    for index in range(500):
        windows.hit(f"identity-{index}", 60, now=1000.0 + index)

    assert len(windows._windows) <= 50


def test_pruning_prefers_expired_windows_over_live_ones():
    """Evicting a live window hands somebody a fresh budget. Expired entries are
    dead weight and go first."""
    windows = _LocalWindows(capacity=2)
    windows.hit("old", 60, now=1000.0)
    windows.hit("live-a", 60, now=2000.0)
    windows.hit("live-b", 60, now=2001.0)
    windows.hit("live-c", 60, now=2002.0)

    assert "old" not in windows._windows


# ---------------------------------------------- what must not fail open at all


@pytest.mark.parametrize("error", FAILURES)
async def test_spending_a_refresh_token_fails_closed(error):
    """The control that must never degrade.

    Reuse detection *is* the atomic write. If Redis cannot answer, whether this
    token has been spent is unknowable - and issuing a fresh pair on an unknown
    is exactly the case the mechanism exists to catch. It raises, the request
    becomes a 5xx, and nothing is issued.
    """
    store = RefreshTokenStore(BrokenRedis(error))

    with pytest.raises(type(error)):
        await store.spend(uuid.uuid4(), ttl_seconds=60)
