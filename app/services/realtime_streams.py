"""Who has an event stream open, across processes (RT-05, RT-07, ADR-155).

Counted in Redis rather than in one process, because a person's five streams
and a workspace's hundred may be spread over every API container. Each open
stream holds a member of a sorted set scored by its last keep-alive; a member
not refreshed for three keep-alives belongs to a process that died, and is
dropped before counting - so a crash never leaves a person locked out.
"""

from __future__ import annotations

import time
import uuid
from typing import Final

from redis.asyncio import Redis

_USER: Final = "wasla:rt:streams:user:"
_WORKSPACE: Final = "wasla:rt:streams:workspace:"
#: Keep-alives a stream may miss before it is counted as gone.
_MISSED: Final = 3


def _keys(user_id: uuid.UUID, tenant_id: uuid.UUID) -> tuple[str, str]:
    return f"{_USER}{user_id}", f"{_WORKSPACE}{tenant_id}"


async def _live(redis: Redis, key: str, keepalive_seconds: float) -> int:
    await redis.zremrangebyscore(key, "-inf", time.time() - _MISSED * keepalive_seconds)
    return int(await redis.zcard(key))


async def open_streams(redis: Redis, *, user_id: uuid.UUID, keepalive_seconds: float) -> int:
    """How many streams this person has open now."""
    return await _live(redis, f"{_USER}{user_id}", keepalive_seconds)


async def register(
    redis: Redis,
    *,
    stream_id: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    per_user: int,
    per_workspace: int,
    keepalive_seconds: float,
) -> bool:
    """Count a new stream in, or refuse it when the person or the workspace is at the limit."""
    user_key, workspace_key = _keys(user_id, tenant_id)
    if await _live(redis, user_key, keepalive_seconds) >= per_user:
        return False
    if await _live(redis, workspace_key, keepalive_seconds) >= per_workspace:
        return False
    now = time.time()
    await redis.zadd(user_key, {stream_id: now})
    await redis.zadd(workspace_key, {stream_id: now})
    ttl = int(_MISSED * keepalive_seconds) + 60
    await redis.expire(user_key, ttl)
    await redis.expire(workspace_key, ttl)
    return True


async def refresh(
    redis: Redis,
    *,
    stream_id: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    keepalive_seconds: float,
) -> None:
    user_key, workspace_key = _keys(user_id, tenant_id)
    now = time.time()
    await redis.zadd(user_key, {stream_id: now})
    await redis.zadd(workspace_key, {stream_id: now})
    ttl = int(_MISSED * keepalive_seconds) + 60
    await redis.expire(user_key, ttl)
    await redis.expire(workspace_key, ttl)


async def unregister(
    redis: Redis, *, stream_id: str, user_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    user_key, workspace_key = _keys(user_id, tenant_id)
    await redis.zrem(user_key, stream_id)
    await redis.zrem(workspace_key, stream_id)


__all__ = ["open_streams", "refresh", "register", "unregister"]
