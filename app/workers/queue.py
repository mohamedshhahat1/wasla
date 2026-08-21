"""The agent job queue.

A reliable queue rather than a plain list pop. Work is moved onto an in-flight
list as it is reserved, so a worker killed mid-job leaves the job recoverable
instead of silently dropping a customer's reply.

Releasing a job removes it from the in-flight list by exact value, which is why
encoding sorts its keys and uses compact separators: a payload re-serialised in
a different key order would never match, and the job would linger forever.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Final, Self, cast

from redis.asyncio import Redis

QUEUE_NAMESPACE: Final = "agent:jobs"
# How long a reserve call waits before returning empty, so a worker loop can
# notice it has been asked to stop.
BLOCK_SECONDS: Final = 5


async def _command[T](result: Awaitable[T] | T) -> T:
    """Await a redis-py command result.

    redis-py types every command as sync-or-async because one class backs both
    clients. On the async client the result is always awaitable, so this states
    that once here instead of at each of the call sites below.
    """
    return await cast("Awaitable[T]", result)


class MalformedJobError(Exception):
    """A queue entry that cannot be decoded.

    Separate from a processing failure: retrying it would fail identically
    forever, so the worker dead-letters it instead.
    """


@dataclass(frozen=True, slots=True)
class AgentJob:
    """A request for an agent to look at one conversation.

    Carries identifiers only. The conversation's contents are read fresh when
    the job runs, because a queued job may wait behind others and the customer
    may have sent more in the meantime.
    """

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    agent_id: uuid.UUID | None = None

    def encode(self) -> str:
        payload: dict[str, str] = {
            "tenant_id": str(self.tenant_id),
            "conversation_id": str(self.conversation_id),
        }
        if self.agent_id is not None:
            payload["agent_id"] = str(self.agent_id)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @classmethod
    def decode(cls, raw: str) -> Self:
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise MalformedJobError("The job is not valid JSON.") from error
        if not isinstance(payload, dict):
            raise MalformedJobError("The job is not an object.")
        return cls(
            tenant_id=_identifier(payload, "tenant_id"),
            conversation_id=_identifier(payload, "conversation_id"),
            agent_id=_optional_identifier(payload, "agent_id"),
        )


def _identifier(payload: dict[str, Any], key: str) -> uuid.UUID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise MalformedJobError(f"The job is missing {key}.")
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise MalformedJobError(f"The job has an unusable {key}.") from error


def _optional_identifier(payload: dict[str, Any], key: str) -> uuid.UUID | None:
    if payload.get(key) is None:
        return None
    return _identifier(payload, key)


class AgentQueue:
    """Redis-backed queue of agent jobs.

    Takes a raw client rather than the wrapper, so a test can pass a fake and
    the queue stays independent of application startup.
    """

    def __init__(self, redis: Redis, *, namespace: str = QUEUE_NAMESPACE) -> None:
        self._redis = redis
        self._pending = namespace + ":pending"
        self._inflight = namespace + ":inflight"
        self._failed = namespace + ":failed"

    async def enqueue(self, job: AgentJob) -> None:
        await _command(self._redis.rpush(self._pending, job.encode()))

    async def reserve(self, *, wait_seconds: int = BLOCK_SECONDS) -> str | None:
        """Claim the oldest job, or return None if none arrives in time.

        The payload is returned rather than a decoded job because releasing it
        later requires the exact original bytes.
        """
        raw: Any = await _command(self._redis.blmove(self._pending, self._inflight, wait_seconds))
        return raw if isinstance(raw, str) else None

    async def release(self, raw: str) -> None:
        """Mark a reserved job done."""
        await _command(self._redis.lrem(self._inflight, 1, raw))

    async def fail(self, raw: str) -> None:
        """Move a reserved job to the dead-letter list.

        Kept rather than discarded: a job that failed is the only evidence that
        a customer went unanswered.
        """
        await _command(self._redis.lrem(self._inflight, 1, raw))
        await _command(self._redis.rpush(self._failed, raw))

    async def depth(self) -> int:
        """How many jobs are waiting."""
        return int(await _command(self._redis.llen(self._pending)))

    async def failed_depth(self) -> int:
        return int(await _command(self._redis.llen(self._failed)))
