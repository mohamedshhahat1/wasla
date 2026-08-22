"""Request rate limiting, backed by Redis.

A fixed window counted with `INCR` and an `EXPIRE` on first use. Not a sliding
window and not a token bucket, and the reason is worth stating: both of those
are meaningfully better at smoothing bursts, and both need either a sorted set
per caller or a Lua script to stay atomic. A fixed window is two commands, is
obviously correct under concurrency, and its worst case - twice the limit across
a window boundary - is not a failure mode that matters for the things being
limited here.

Two properties are load-bearing.

**It fails open.** If Redis is unreachable the request is allowed and a warning
is logged. A limiter that fails closed converts a cache outage into a total
outage, and the thing it would be protecting is already having a bad day. The
exception is deliberate and it is why this module never lets a `RedisError`
escape.

**It is never applied to the WhatsApp webhook.** Meta retries anything that is
not a 2xx and eventually disables a subscription that keeps failing, so a 429
there does not shed load - it loses customer messages and then the integration
(ADR-032).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from redis.exceptions import RedisError

from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger
from app.core.redis import RedisClient

logger = get_logger(__name__)

# Every key is namespaced, so a limiter can never collide with a queue or a
# token denylist sharing the same Redis.
KEY_PREFIX: Final = "ratelimit"


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """How many requests, over how many seconds, for one kind of caller.

    `name` is part of the key, so two policies over the same identity - a login
    attempt and a campaign send by the same workspace - count separately.
    """

    name: str
    limit: int
    window_seconds: int

    def key(self, identity: str) -> str:
        return f"{KEY_PREFIX}:{self.name}:{identity}"


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
            # Fails open, deliberately. See the module docstring.
            logger.warning(
                "ratelimit.unavailable",
                extra={"event": "ratelimit.unavailable", "policy": policy.name},
            )
            return RateLimitDecision(
                allowed=True,
                limit=policy.limit,
                remaining=policy.limit,
                retry_after_seconds=0,
            )

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
