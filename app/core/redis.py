"""Redis client.

Redis backs caching, queues, rate limiting and follow-up scheduling (ADR-006).
One client and connection pool are shared per process.
"""

from __future__ import annotations

import asyncio

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


class RedisClient:
    """Thin wrapper owning the connection pool and health probe."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Redis = Redis.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout_seconds,
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
