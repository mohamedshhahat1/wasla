"""The media understanding queue.

A third queue, for the reason the second one exists (ADR-019): these jobs have
different urgency and different failure costs from the others. A media job *is*
a customer waiting - the agent cannot answer until the file is read - so it must
not sit behind a bulk document upload. And it must not share the agent queue
either, because a worker pool sized for inference is the wrong shape for a pool
that spends its time downloading.

The reliability mechanics are the agent queue's (ADR-015): work moves to an
in-flight list as it is reserved, so a worker killed mid-job leaves the job
recoverable, and releasing removes it by exact value - which is why encoding
sorts its keys and uses compact separators.

Media jobs are idempotent, like ingestion and unlike an agent turn. A file
already stored is not downloaded again and one already read is not read again,
so a duplicate job costs a database round trip and changes nothing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Final, Self

from redis.asyncio import Redis

from app.core.redis import MAX_BLOCKING_SECONDS
from app.workers.queue import MalformedJobError, _command

MEDIA_NAMESPACE: Final = "media:understanding"
# From the Redis client, which sizes its read timeout around this. The two must
# be chosen together or a blocking reserve trips its own socket.
BLOCK_SECONDS: Final = MAX_BLOCKING_SECONDS


@dataclass(frozen=True, slots=True)
class MediaJob:
    """A request to fetch and read one attached file.

    Carries identifiers only, like every other job here. The row is read fresh
    when the job runs, because a previous attempt may have advanced it while
    this one waited.
    """

    tenant_id: uuid.UUID
    media_id: uuid.UUID

    def encode(self) -> str:
        return json.dumps(
            {
                "tenant_id": str(self.tenant_id),
                "media_id": str(self.media_id),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

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
            media_id=_identifier(payload, "media_id"),
        )


def _identifier(payload: dict[str, Any], key: str) -> uuid.UUID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise MalformedJobError(f"The job is missing {key}.")
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise MalformedJobError(f"The job has an unusable {key}.") from error


class MediaQueue:
    """Redis-backed queue of media understanding jobs.

    Takes a raw client rather than the wrapper, so a test can pass a fake and
    the queue stays independent of application startup.
    """

    def __init__(self, redis: Redis, *, namespace: str = MEDIA_NAMESPACE) -> None:
        self._redis = redis
        self._pending = namespace + ":pending"
        self._inflight = namespace + ":inflight"
        self._failed = namespace + ":failed"

    async def enqueue(self, job: MediaJob) -> None:
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

        Kept rather than discarded: the media row already records why it failed,
        but the job records that an attempt was made at all, which distinguishes
        a file nobody tried from one that broke.
        """
        await _command(self._redis.lrem(self._inflight, 1, raw))
        await _command(self._redis.rpush(self._failed, raw))

    async def depth(self) -> int:
        """How many files are waiting to be read."""
        return int(await _command(self._redis.llen(self._pending)))

    async def failed_depth(self) -> int:
        return int(await _command(self._redis.llen(self._failed)))
