"""Redis client tests."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.exceptions import DependencyUnavailableError
from app.core.redis import RedisClient


async def test_client_is_built_from_settings():
    client = RedisClient(Settings(_env_file=None, redis_url="redis://cache:6379/3"))

    try:
        kwargs = client.client.connection_pool.connection_kwargs
        assert kwargs["host"] == "cache"
        assert kwargs["port"] == 6379
        assert kwargs["db"] == 3
    finally:
        await client.close()


async def test_check_raises_dependency_unavailable_when_unreachable():
    client = RedisClient(
        Settings(
            _env_file=None,
            redis_url="redis://127.0.0.1:1/0",
            redis_socket_timeout_seconds=1.0,
            health_check_timeout_seconds=1.0,
        )
    )

    try:
        with pytest.raises(DependencyUnavailableError) as exc_info:
            await client.check()
        assert exc_info.value.status_code == 503
        assert exc_info.value.details == {"dependency": "redis"}
    finally:
        await client.close()
