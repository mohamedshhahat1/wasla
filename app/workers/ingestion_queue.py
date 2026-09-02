"""The document ingestion queue.

A second queue rather than a second job type on the agent queue, because the two
have different urgency and different failure costs. An agent job is a customer
waiting for a reply; an ingestion job is a document that will be searchable in a
minute. Sharing one list would let a bulk upload of a hundred documents sit in
front of somebody's question.

The reliability mechanics are `ReliableQueue`'s, shared with every other queue
here (ADR-015, ADR-068): work moves to an in-flight list as it is reserved, a
transient failure goes to a delayed set with its attempt count, and a terminal
one goes to the dead-letter list as a record rather than a bare payload.

Ingestion is genuinely idempotent, unlike an agent turn. Re-running it replaces
the document's chunks rather than appending, so a duplicated job wastes
embedding calls and changes nothing. That is what makes retrying safe here when
it is not safe there, and it is why this queue carries `IDEMPOTENT_RETRY` while
the agent worker narrows its own policy the moment a turn engages the provider.
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
    INGESTION_NAMESPACE,
    MalformedJobError,
    ReliableQueue,
    _decode_object,
    _identifier,
)

# How long a reserve call waits before returning empty, so a worker loop can
# notice it has been asked to stop.
# From the Redis client, which sizes its read timeout around this. The two
# must be chosen together or a blocking reserve trips its own socket.
BLOCK_SECONDS: Final = MAX_BLOCKING_SECONDS


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
        payload = _decode_object(raw)
        return cls(
            tenant_id=_identifier(payload, "tenant_id"),
            document_id=_identifier(payload, "document_id"),
        )


class IngestionQueue(ReliableQueue):
    """Redis-backed queue of document ingestion jobs.

    Takes a raw client rather than the wrapper, so a test can pass a fake and
    the queue stays independent of application startup.
    """

    label = "ingestion"

    #: Re-ingesting replaces a document's chunks rather than appending, so a
    #: repeat costs embedding calls and changes nothing anybody can see.
    idempotent = True

    def __init__(
        self,
        redis: Redis,
        *,
        namespace: str = INGESTION_NAMESPACE,
        worker_id: str | None = None,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            redis,
            namespace=namespace,
            worker_id=worker_id,
            visibility_timeout_seconds=visibility_timeout_seconds,
        )

    async def enqueue(self, job: IngestionJob, *, now: datetime | None = None) -> None:
        await self.enqueue_body(job.encode(), now=now)

    def identify(self, body: str) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        try:
            job = IngestionJob.decode(body)
        except MalformedJobError:
            return None, None
        return job.tenant_id, job.document_id


__all__ = ["BLOCK_SECONDS", "INGESTION_NAMESPACE", "IngestionJob", "IngestionQueue"]
