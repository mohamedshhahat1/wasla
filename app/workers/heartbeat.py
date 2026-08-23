"""Proof that a worker loop is still alive.

The worker container has had no health check since it existed. The image's
`HEALTHCHECK` curls the API's liveness endpoint, and the worker serves no HTTP,
so it inherited a probe it could never pass and reported unhealthy for its whole
life — which is worse than no signal at all: it makes `docker ps` lie, hangs
anything waiting on `service_healthy`, and trains an operator to ignore the
health column. Both compose files disabled it rather than fake it, and this is
what makes a real one possible.

The process writes one key per worker kind it is running, with a short expiry,
refreshed on a timer. Liveness is then "does the key exist" — no timestamps to
compare and no clock skew between the container and Redis to reason about.

**What this proves, precisely.** The process is running and its event loop is
responsive: the beat is an ordinary task, so anything that stops the loop
scheduling — a crash, a hang, a blocking call in async code, a container stuck
before start-up finished — stops the beat and the key expires. That is exactly
what a container liveness probe should assert.

**What it does not prove.** That any particular loop is making progress. A
worker waiting on a query that never returns keeps beating, because the beat
comes from a different task. Proving progress needs a queue depth and a notion
of expected throughput that this system does not have, and pretending otherwise
would put a green health column in front of a stalled queue — which is the
failure this whole item exists to stop happening again.

The in-flight reaper phase 8 wants reads these same keys, which is why the
identity is per kind rather than one per process.
"""

from __future__ import annotations

from typing import Final

from redis.exceptions import RedisError

from app.core.logging import get_logger
from app.core.redis import RedisClient

logger = get_logger(__name__)

KEY_PREFIX: Final = "worker:heartbeat"

# How long a beat stays valid: three intervals, so two missed beats are a blip
# and three are a death. Deliberately unrelated to how often a loop finds work -
# the billing sweep runs every ten minutes, and a liveness signal on that
# schedule would report a dead container as healthy for nine of them.
DEFAULT_TTL_SECONDS: Final = 90

# How often the beat is refreshed.
DEFAULT_INTERVAL_SECONDS: Final = 30


def heartbeat_key(kind: str) -> str:
    return f"{KEY_PREFIX}:{kind}"


class Heartbeat:
    """Writes and reads the liveness key for one worker kind."""

    def __init__(
        self,
        redis: RedisClient,
        *,
        kind: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._redis = redis
        self._kind = kind
        self._ttl = ttl_seconds

    @property
    def key(self) -> str:
        return heartbeat_key(self._kind)

    async def beat(self) -> bool:
        """Record that this kind is being run by a live process.

        Returns whether it was recorded.

        A Redis failure is swallowed and logged: a worker that crashed because
        it could not announce itself would turn a cache blip into an outage of
        the thing being watched. The consequence is that the key expires and the
        container is reported unhealthy, which is the correct signal - Redis
        being down *is* a reason this worker cannot do its job.
        """
        try:
            await self._redis.client.set(self.key, "1", ex=self._ttl)
        except RedisError:
            logger.warning(
                "worker.heartbeat_failed",
                extra={"event": "worker.heartbeat_failed", "kind": self._kind},
            )
            return False
        return True

    async def is_alive(self) -> bool:
        """Whether this kind has beaten recently enough.

        Read by the container's health command. A Redis failure answers *not
        alive*, which is deliberate and the opposite of the rate limiter's
        choice: a limiter failing open keeps a working system serving, while a
        health probe failing open hides exactly the outage it exists to report.
        """
        try:
            return bool(await self._redis.client.exists(self.key))
        except RedisError:
            logger.warning(
                "worker.heartbeat_unreadable",
                extra={"event": "worker.heartbeat_unreadable", "kind": self._kind},
            )
            return False


async def all_alive(redis: RedisClient, kinds: tuple[str, ...]) -> bool:
    """Whether *every* named kind has beaten recently.

    All rather than any, deliberately: a container told to run six loops with
    one of them dead is not healthy, and reporting it healthy because the other
    five are beating is how a silently stopped loop survives for weeks. An empty
    set of kinds is not alive either - a worker process running nothing is a
    misconfiguration, not a healthy idle.
    """
    for kind in kinds:
        if not await Heartbeat(redis, kind=kind).is_alive():
            return False
    return bool(kinds)
