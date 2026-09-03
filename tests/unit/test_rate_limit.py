"""The counting rules, against a fake Redis.

Two of these matter more than the arithmetic: a refused request is still
counted, so hammering a limited endpoint keeps the window full rather than
resetting it; and a Redis that is down allows the request, because a limiter
that fails closed turns a cache outage into a total outage.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import RedisError

from app.core.exceptions import RateLimitedError
from app.core.rate_limit import RateLimiter, RateLimitPolicy
from tests.fakes import as_redis_client

POLICY = RateLimitPolicy(name="auth", limit=3, window_seconds=60)


class FakeCommands:
    """Counts and expiries, with the two failure modes worth simulating."""

    def __init__(self, *, broken: bool = False, no_expiry: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.broken = broken
        self.no_expiry = no_expiry
        self.expire_calls = 0

    async def incr(self, key: str) -> int:
        if self.broken:
            raise RedisError("redis is not there")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expire_calls += 1
        self.expiries[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        if self.no_expiry:
            return -1
        return self.expiries.get(key, -1)


class FakeRedis:
    def __init__(self, commands: FakeCommands) -> None:
        self._commands = commands

    @property
    def client(self) -> FakeCommands:
        return self._commands


def _limiter(**kwargs: Any) -> tuple[RateLimiter, FakeCommands]:
    commands = FakeCommands(**kwargs)
    return RateLimiter(as_redis_client(FakeRedis(commands))), commands


async def test_requests_under_the_limit_are_allowed() -> None:
    limiter, _ = _limiter()

    for expected_remaining in (2, 1, 0):
        decision = await limiter.check(POLICY, "1.2.3.4")
        assert decision.allowed is True
        assert decision.remaining == expected_remaining


async def test_the_request_after_the_limit_is_refused() -> None:
    limiter, _ = _limiter()
    for _ in range(3):
        await limiter.check(POLICY, "1.2.3.4")

    decision = await limiter.check(POLICY, "1.2.3.4")

    assert decision.allowed is False
    assert decision.retry_after_seconds > 0


async def test_a_refused_request_still_counts() -> None:
    """Hammering keeps the window full rather than resetting it, which is what
    makes the limit worth having against something automated."""
    limiter, commands = _limiter()
    for _ in range(10):
        await limiter.check(POLICY, "1.2.3.4")

    assert commands.counts[POLICY.key("1.2.3.4")] == 10


async def test_the_window_is_set_once_and_not_refreshed() -> None:
    """Refreshing on every request turns a fixed window into a sliding expiry
    that never ends for a caller who keeps trying."""
    limiter, commands = _limiter()
    for _ in range(5):
        await limiter.check(POLICY, "1.2.3.4")

    assert commands.expire_calls == 1


async def test_a_key_with_no_expiry_is_given_one() -> None:
    """A bug or a manual edit must not leave a caller counted forever."""
    limiter, commands = _limiter(no_expiry=True)

    await limiter.check(POLICY, "1.2.3.4")
    await limiter.check(POLICY, "1.2.3.4")

    assert commands.expire_calls == 2


async def test_two_callers_are_counted_separately() -> None:
    limiter, _ = _limiter()
    for _ in range(3):
        await limiter.check(POLICY, "1.2.3.4")

    decision = await limiter.check(POLICY, "5.6.7.8")

    assert decision.allowed is True


async def test_two_policies_over_one_identity_are_counted_separately() -> None:
    """A workspace's general traffic and its campaign budget are different
    questions about the same tenant."""
    limiter, _ = _limiter()
    other = RateLimitPolicy(name="campaign", limit=3, window_seconds=60)
    for _ in range(3):
        await limiter.check(POLICY, "tenant-1")

    assert (await limiter.check(other, "tenant-1")).allowed is True


async def test_a_redis_outage_allows_the_request() -> None:
    """A limiter that fails closed converts a cache outage into a total outage,
    and the thing it protects is already having a bad day."""
    limiter, _ = _limiter(broken=True)

    decision = await limiter.check(POLICY, "1.2.3.4")

    assert decision.allowed is True
    assert decision.remaining == POLICY.limit


async def test_enforcing_raises_with_a_retry_after_header() -> None:
    """A client told to back off without being told for how long retries
    immediately, which is the traffic the limit exists to stop."""
    limiter, _ = _limiter()
    for _ in range(3):
        await limiter.enforce(POLICY, "1.2.3.4")

    with pytest.raises(RateLimitedError) as raised:
        await limiter.enforce(POLICY, "1.2.3.4")

    assert raised.value.status_code == 429
    assert "Retry-After" in (raised.value.headers or {})


async def test_an_allowed_response_carries_no_retry_after() -> None:
    """Telling a well-behaved client to wait when it need not is worse than
    saying nothing."""
    limiter, _ = _limiter()

    decision = await limiter.check(POLICY, "1.2.3.4")

    assert "Retry-After" not in decision.headers
    assert decision.headers["X-RateLimit-Limit"] == "3"


async def test_keys_are_namespaced() -> None:
    """A limiter must never collide with a queue or a token denylist sharing
    the same Redis."""
    assert POLICY.key("1.2.3.4").startswith("ratelimit:auth:")
