"""Request rate limiting, backed by Redis.

A fixed window counted with `INCR` and an `EXPIRE` on first use. Not a sliding
window and not a token bucket, and the reason is worth stating: both of those
are meaningfully better at smoothing bursts, and both need either a sorted set
per caller or a Lua script to stay atomic. A fixed window is two commands, is
obviously correct under concurrency, and its worst case - twice the limit across
a window boundary - is not a failure mode that matters for the things being
limited here.

Three properties are load-bearing.

**A Redis failure never becomes an application failure.** Nothing here lets a
`RedisError` escape. A limiter that turned a cache outage into a 500 would
convert a degraded dependency into a total one, and the thing it is protecting
is already having a bad day.

**What happens instead depends on what the policy is protecting, and the
distinction is the whole of ADR-040.** A limit on how much shared capacity a
signed-in workspace may consume is an *availability* control: when Redis is
gone, allowing the request is right, because refusing a paying customer's
colleagues to protect capacity that is not currently contended is the outage,
not the defence. A limit in front of a credential is a *security* control:
allowing it means unlimited password attempts for as long as the outage lasts,
and that is the only anti-automation control on `/auth/login`.

So the security policies carry a **process-local fallback**. It is not a
distributed limit and does not pretend to be - with N API processes an attacker
gets N budgets - but it turns "unlimited for the duration of the outage" into a
small bounded number, and it cannot cause an outage of its own because it never
refuses more than the policy already would. Fail-closed on Redis was considered
and rejected: it makes signing in impossible whenever the cache is down, which
is both worse for customers and *attacker-triggerable* by anyone who can degrade
Redis.

**It is never applied to the WhatsApp webhook.** Meta retries anything that is
not a 2xx and eventually disables a subscription that keeps failing, so a 429
there does not shed load - it loses customer messages and then the integration
(ADR-032).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Final

from redis.exceptions import RedisError

from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger
from app.core.redis import RedisClient

logger = get_logger(__name__)

# Every key is namespaced, so a limiter can never collide with a queue or a
# token denylist sharing the same Redis.
KEY_PREFIX: Final = "ratelimit"

# Login attempts counted against one account, whatever address they arrive from.
# Lives here rather than in the API layer because the service applies it: the
# identity is inside the request body, and a route dependency that read the body
# to find it would consume the stream before the handler saw it.
LOGIN_ACCOUNT_POLICY: Final = "auth:account"


def account_identity(email: str) -> str:
    """The bucket a login attempt is counted in, derived from the account named.

    Hashed rather than stored raw. The key lives in Redis, which is shared
    infrastructure and shows up in slow-log output, `KEYS` dumps and support
    screenshots; an email address is personal data and does not need to be
    legible there for a counter to work. Lower-cased first, so `A@b.com` and
    `a@b.com` cannot be spent as two budgets against one account.
    """
    normalised = email.strip().lower().encode("utf-8")
    return hashlib.sha256(normalised).hexdigest()[:32]


# How many identities the process-local fallback will track. Sized so the worst
# case is on the order of a megabyte rather than chosen from a threat model:
# what bounds the *attack* is the per-account policy, whose identities are the
# accounts being targeted rather than the addresses doing the targeting.
LOCAL_FALLBACK_CAPACITY: Final = 10_000


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """How many requests, over how many seconds, for one kind of caller.

    `name` is part of the key, so two policies over the same identity - a login
    attempt and a campaign send by the same workspace - count separately.

    `local_fallback` says what to do when Redis cannot answer. Left off, the
    request is allowed: that is right for a limit protecting shared capacity,
    where refusing during a cache outage is the outage. Turned on, the process
    falls back to counting in memory, which is what a limit in front of a
    credential needs - see the module docstring and ADR-040.
    """

    name: str
    limit: int
    window_seconds: int
    local_fallback: bool = False

    def key(self, identity: str) -> str:
        return f"{KEY_PREFIX}:{self.name}:{identity}"


@dataclass
class _LocalWindows:
    """Fixed-window counters held in this process, for use only when Redis is down.

    Deliberately tiny and deliberately not a general-purpose limiter. It exists
    for one situation - the credential endpoints while the cache is unreachable
    - and its only job is to stop "unlimited" from being the answer.

    Bounded by `capacity`. Expired windows are pruned first; if that is not
    enough the oldest is evicted, which an attacker rotating source addresses
    could force. That is not a weakening: rotating addresses already defeats a
    per-address limit, and the per-account policy - the one that does not care
    where the traffic comes from - is keyed by the account under attack rather
    than by anything the attacker can rotate.
    """

    capacity: int = LOCAL_FALLBACK_CAPACITY
    _windows: dict[str, tuple[float, int]] = field(default_factory=dict)

    def hit(self, key: str, window_seconds: int, *, now: float | None = None) -> int:
        """Count one request and return how many this window has seen."""
        moment = time.monotonic() if now is None else now
        started, used = self._windows.get(key, (moment, 0))
        if moment - started >= window_seconds:
            started, used = moment, 0
        used += 1
        self._windows[key] = (started, used)
        self._prune(moment, window_seconds)
        return used

    def _prune(self, now: float, window_seconds: int) -> None:
        if len(self._windows) <= self.capacity:
            return
        expired = [
            key for key, (started, _) in self._windows.items() if now - started >= window_seconds
        ]
        for key in expired:
            del self._windows[key]
        while len(self._windows) > self.capacity:
            oldest = min(self._windows, key=lambda key: self._windows[key][0])
            del self._windows[oldest]


# One per process, shared by every limiter instance. A limiter is constructed
# per request, so holding the counters on it would make the fallback a no-op.
_LOCAL_WINDOWS = _LocalWindows()


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """What the limiter concluded, and what to tell the caller."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int

    @property
    def headers(self) -> dict[str, str]:
        """The headers a client needs to back off intelligently.

        `Retry-After` is only meaningful on a refusal, and sending it on an
        allowed request would tell a well-behaved client to wait when it does
        not have to.
        """
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers


class RateLimiter:
    """Counts requests per identity per policy."""

    def __init__(self, redis: RedisClient) -> None:
        self._redis = redis

    async def check(self, policy: RateLimitPolicy, identity: str) -> RateLimitDecision:
        """Count this request and say whether it is allowed.

        The `INCR` happens before the comparison, so a caller that is refused is
        still counted: hammering a limited endpoint keeps the window full rather
        than resetting it, which is the behaviour that makes a limit worth
        having against something automated.
        """
        key = policy.key(identity)
        try:
            commands = self._redis.client
            used = int(await commands.incr(key))
            if used == 1:
                # Set only on the first request of a window. Refreshing it on
                # every request would turn a fixed window into a sliding
                # expiry that never ends for a caller who keeps trying.
                await commands.expire(key, policy.window_seconds)
                ttl = policy.window_seconds
            else:
                ttl = int(await commands.ttl(key))
                if ttl < 0:
                    # A key with no expiry is a bug or a manual edit; give it
                    # one rather than letting it count forever.
                    await commands.expire(key, policy.window_seconds)
                    ttl = policy.window_seconds
        except RedisError:
            # Never an application failure. What happens instead depends on
            # what this policy protects - see the module docstring and ADR-040.
            logger.warning(
                "ratelimit.unavailable",
                extra={
                    "event": "ratelimit.unavailable",
                    "policy": policy.name,
                    "fallback": policy.local_fallback,
                },
            )
            return self._without_redis(policy, identity)

        allowed = used <= policy.limit
        if not allowed:
            logger.info(
                "ratelimit.refused",
                extra={
                    "event": "ratelimit.refused",
                    "policy": policy.name,
                    # The identity is not logged: for the authentication
                    # limiter it is a client address, and for the workspace
                    # limiter it is a tenant id that belongs in the request
                    # context rather than duplicated here.
                    "used": used,
                },
            )
        return RateLimitDecision(
            allowed=allowed,
            limit=policy.limit,
            remaining=policy.limit - used,
            retry_after_seconds=max(ttl, 1),
        )

    def _without_redis(self, policy: RateLimitPolicy, identity: str) -> RateLimitDecision:
        """The decision when the counter cannot be reached.

        Availability policies allow. Security policies fall back to counting in
        this process, which is weaker than a shared counter and enormously
        stronger than nothing: an attacker gets one budget per API process for
        the duration of the outage instead of an unbounded number of attempts.
        """
        if not policy.local_fallback:
            return RateLimitDecision(
                allowed=True,
                limit=policy.limit,
                remaining=policy.limit,
                retry_after_seconds=0,
            )

        used = _LOCAL_WINDOWS.hit(policy.key(identity), policy.window_seconds)
        allowed = used <= policy.limit
        if not allowed:
            logger.warning(
                "ratelimit.refused_locally",
                extra={
                    "event": "ratelimit.refused_locally",
                    "policy": policy.name,
                    "used": used,
                },
            )
        return RateLimitDecision(
            allowed=allowed,
            limit=policy.limit,
            remaining=policy.limit - used,
            retry_after_seconds=policy.window_seconds,
        )

    async def enforce(self, policy: RateLimitPolicy, identity: str) -> RateLimitDecision:
        """Refuse the request if the policy is exhausted.

        The refusal carries `Retry-After`, because a client told to back off
        without being told for how long simply retries immediately - which is
        the traffic the limit exists to stop.
        """
        decision = await self.check(policy, identity)
        if not decision.allowed:
            raise RateLimitedError(
                f"Too many requests. Try again in {decision.retry_after_seconds} seconds.",
                headers=decision.headers,
            )
        return decision
