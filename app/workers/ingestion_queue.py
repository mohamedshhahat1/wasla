"""The document ingestion queue.

A second queue rather than a second job type on the agent queue, because the two
have different urgency and different failure costs. An agent job is a customer
waiting for a reply; an ingestion job is a document that will be searchable in a
minute. Sharing one list would let a bulk upload of a hundred documents sit in
front of somebody's question.

The reliability mechanics are the same as the agent queue's (ADR-015): work
moves to an in-flight list as it is reserved, so a worker killed mid-job leaves
the job recoverable rather than silently dropping it, and releasing removes it
by exact value - which is why encoding sorts its keys and uses compact
separators.

Ingestion is genuinely idempotent, unlike an agent turn. Re-running it replaces
the document's chunks rather than appending, so a duplicated job wastes
embedding calls and changes nothing. That is what makes requeueing safe here
when it is not safe there.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Final, Self

from redis.asyncio import Redis

from app.workers.queue import MalformedJobError, _command

INGESTION_NAMESPACE: Final = "knowledge:ingestion"
# How long a reserve call waits before returning empty, so a worker loop can
# notice it has been asked to stop.
BLOCK_SECONDS: Final = 5


@dataclass(frozen=True, slots=True)
class IngestionJob:
    """A request to ingest one document.

    Carries identifiers only. The document's text is read fresh when the job
    runs, because a resubmission may have replaced it while the job waited.
    """

    tenant_id: uuid.UUID
    document_id: uuid.UUID

    def encode(self) -> str:
        return json.dumps(
            {
                "tenant_id": str(self.tenant_id),
                "document_id": str(self.document_id),
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
            document_id=_identifier(payload, "document_id"),
        )


def _identifier(payload: dict[str, Any], key: str) -> uuid.UUID:
    value = payload.get(key)
    if not isinstance(value, str):
        raise MalformedJobError(f"The job is missing {key}.")
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise MalformedJobError(f"The job has an unusable {key}.") from error


class IngestionQueue:
    """Redis-backed queue of document ingestion jobs.

    Takes a raw client rather than the wrapper, so a test can pass a fake and
    the queue stays independent of application startup.
    """

    def __init__(self, redis: Redis, *, namespace: str = INGESTION_NAMESPACE) -> None:
        self._redis = redis
        self._pending = namespace + ":pending"
        self._inflight = namespace + ":inflight"
        self._failed = namespace + ":failed"

    async def enqueue(self, job: IngestionJob) -> None:
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

        Kept rather than discarded. The document row already records why it
        failed, but the job records that an attempt was made at all, which is
        what distinguishes a document nobody tried from one that broke.
        """
        await _command(self._redis.lrem(self._inflight, 1, raw))
        await _command(self._redis.rpush(self._failed, raw))

    async def depth(self) -> int:
        """How many documents are waiting."""
        return int(await _command(self._redis.llen(self._pending)))

    async def failed_depth(self) -> int:
        return int(await _command(self._redis.llen(self._failed)))
