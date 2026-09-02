"""The media understanding queue.

A third queue, for the reason the second one exists (ADR-019): these jobs have
different urgency and different failure costs from the others. A media job *is*
a customer waiting - the agent cannot answer until the file is read - so it must
not sit behind a bulk document upload. And it must not share the agent queue
either, because a worker pool sized for inference is the wrong shape for a pool
that spends its time downloading.

The reliability mechanics are `ReliableQueue`'s, shared with every other queue
here (ADR-015, ADR-068): work moves to an in-flight list as it is reserved, a
transient failure goes to a delayed set with its attempt count, and a terminal
one goes to the dead-letter list as a record rather than a bare payload.

Media jobs are idempotent, like ingestion and unlike an agent turn. A file
already stored is not downloaded again and one already read is not read again,
so a duplicate job costs a database round trip and changes nothing - which is
what makes `IDEMPOTENT_RETRY` the right policy here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Self

from redis.asyncio import Redis

from app.core.redis import MAX_BLOCKING_SECONDS
from app.workers.queue import (
    DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    MEDIA_NAMESPACE,
    MalformedJobError,
    ReliableQueue,
    _decode_object,
    _identifier,
)

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
        payload = _decode_object(raw)
        return cls(
            tenant_id=_identifier(payload, "tenant_id"),
            media_id=_identifier(payload, "media_id"),
        )


class MediaQueue(ReliableQueue):
    """Redis-backed queue of media understanding jobs.

    Takes a raw client rather than the wrapper, so a test can pass a fake and
    the queue stays independent of application startup.
    """

    label = "media"

    #: A file already stored is not downloaded again and one already read is
    #: not read again, so a repeat costs a database round trip and no more.
    idempotent = True

    def __init__(
        self,
        redis: Redis,
        *,
        namespace: str = MEDIA_NAMESPACE,
        worker_id: str | None = None,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            redis,
            namespace=namespace,
            worker_id=worker_id,
            visibility_timeout_seconds=visibility_timeout_seconds,
        )

    async def enqueue(self, job: MediaJob, *, now: datetime | None = None) -> None:
        await self.enqueue_body(job.encode(), now=now)

    def identify(self, body: str) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        try:
            job = MediaJob.decode(body)
        except MalformedJobError:
            return None, None
        return job.tenant_id, job.media_id


__all__ = ["BLOCK_SECONDS", "MEDIA_NAMESPACE", "MediaJob", "MediaQueue"]
