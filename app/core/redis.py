"""Redis client.

Redis backs caching, queues, rate limiting and follow-up scheduling (ADR-006).
One client and connection pool are shared per process.
"""

from __future__ import annotations

import asyncio
from typing import Final

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

# The longest a queue reserve may block waiting for work. Declared here rather
# than in the queues because it is a property of the *connection*: redis-py
# applies `socket_timeout` to every read, including the one that is deliberately
# waiting, so the two constants have to be chosen together.
MAX_BLOCKING_SECONDS: Final = 5

# Margin between the block and the read timeout. Without it the two race: a
# `BLMOVE` that waits exactly as long as the socket allows means an idle worker
# dies on a spurious timeout roughly every interval. That is not theoretical -
# it is what the worker container did the first time it was started.
BLOCKING_HEADROOM_SECONDS: Final = 5


class RedisClient:
    """Thin wrapper owning the connection pool and health probe."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Redis = Redis.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            # Never below what a blocking reserve needs. The configured value
            # governs ordinary commands; a shorter read timeout than the block
            # duration would make every idle worker fail on its own patience.
            socket_timeout=max(
                settings.redis_socket_timeout_seconds,
                MAX_BLOCKING_SECONDS + BLOCKING_HEADROOM_SECONDS,
            ),
            # Connecting does not block on work, so this keeps the configured
            # value: a dead host should be reported quickly.
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
            decode_responses=True,
        )

    @property
    def client(self) -> Redis:
        return self._client

    async def check(self, timeout_seconds: float | None = None) -> None:
        """Verify connectivity. Raises DependencyUnavailableError on failure."""
        limit = self._settings.health_check_timeout_seconds
        if timeout_seconds is not None:
            limit = timeout_seconds
        try:
            async with asyncio.timeout(limit):
                await self._client.ping()
        except Exception as exc:
            logger.warning(
                "health.redis_unavailable",
                extra={"event": "health.redis_unavailable", "reason": type(exc).__name__},
            )
            raise DependencyUnavailableError(
                "Redis is unavailable.",
                details={"dependency": "redis"},
            ) from exc

    async def close(self) -> None:
        await self._client.aclose()
