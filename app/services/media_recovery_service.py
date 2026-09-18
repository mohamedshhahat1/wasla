"""Finishing attached files that no attempt is going to finish (MEDIA-03).

The release gate - an agent answers a conversation only once none of its files
is unresolved - is only correct if every file resolves. Before this existed a
row whose job dead-lettered, or whose worker died mid-download, stayed `pending`
or `downloading` for ever, and that one row silenced every later attachment in
the conversation. Inbound recovery did not look at it, because it only rescues
events that never reached a queue; upload reconciliation did not either, because
it owns where the bytes are, not whether anybody read them.

**What counts as stranded is derived, not guessed** (`media_horizons`): a claim
older than the longest an attempt can take, or a file unclaimed for longer than
the queue's entire retry budget. Anything younger is ordinary work in flight and
is left alone.

**What happens to it is bounded.** A stranded file that has not yet had
`MAX_ATTEMPTS` attempts is put back on the queue - its worker died, and dying is
not a verdict on the file. One that has had them all is given up on: marked
`FAILED` with a fixed reason, and its conversation re-evaluated under the same
gate the worker uses, so the customer is answered. Nothing is re-parsed here;
this service never touches a file's bytes.

It decides and stages; the caller commits and only then enqueues, in that
order, for the reason ADR-092 gives: a job must never be consumable before the
row it names is durable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.storage import MediaStorage
from app.db.models.media import MediaStatus, MessageMedia
from app.repositories.media_repository import (
    ConversationMediaGate,
    MediaRepository,
    PlatformMediaRepository,
)
from app.services.media_horizons import claim_lease, unclaimed_horizon
from app.services.media_service import MediaService
from app.workers.media_queue import MediaJob
from app.workers.queue import AgentJob

logger = get_logger(__name__)

# How many stranded files one pass takes. A healthy deployment has none; an
# outage that stranded many drains over a few passes rather than in one
# transaction holding a lock on every one of them.
RECOVERY_BATCH_SIZE = 100


@dataclass(slots=True)
class MediaRecoveryPass:
    """What one sweep decided, and what its caller must enqueue after committing."""

    requeued: list[MediaJob] = field(default_factory=list)
    abandoned: int = 0
    releases: list[AgentJob] = field(default_factory=list)


class MediaRecoveryService:
    """Finds stranded files across every workspace and finishes them."""

    def __init__(self, session: AsyncSession, *, settings: Settings, storage: MediaStorage) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage

    async def sweep(self, *, now: datetime, limit: int = RECOVERY_BATCH_SIZE) -> MediaRecoveryPass:
        """One pass. Staged in the caller's transaction."""
        rows = await PlatformMediaRepository(self._session).claim_stranded(
            claimed_before=now - claim_lease(self._settings),
            created_before=now - unclaimed_horizon(self._settings),
            limit=limit,
        )
        result = MediaRecoveryPass()
        # The newest given-up file per conversation, so each conversation is
        # re-evaluated once and its turn is keyed on the latest message.
        given_up: dict[tuple[uuid.UUID, uuid.UUID], MessageMedia] = {}

        for row in rows:
            if row.is_exhausted:
                outcome = await self._service(row.tenant_id).abandon(row.id, claim_id=row.claim_id)
                if outcome.reason is not None:
                    result.abandoned += 1
                    key = (row.tenant_id, row.conversation_id)
                    latest = given_up.get(key)
                    if latest is None or row.created_at >= latest.created_at:
                        given_up[key] = row
                continue

            # A worker died holding it, or its job was lost before anybody
            # took it up. The first already spent an attempt when it claimed;
            # the second is counted here, so a file whose jobs keep vanishing
            # is still given up on after `MAX_ATTEMPTS`.
            if row.claim_id is None:
                row.attempts += 1
            row.claim_id = None
            row.claimed_at = now
            if row.status is MediaStatus.DOWNLOADING:
                # Nothing is downloading it. Whether bytes were written is the
                # storage state's business, and the next attempt reads it.
                row.status = MediaStatus.PENDING
            result.requeued.append(MediaJob(tenant_id=row.tenant_id, media_id=row.id))

        await self._session.flush()

        for (tenant_id, conversation_id), row in given_up.items():
            release = await self._release(tenant_id, conversation_id, trigger=row.message_id)
            if release is not None:
                result.releases.append(release)

        if rows:
            logger.warning(
                "media.recovery_pass",
                extra={
                    "event": "media.recovery_pass",
                    "stranded": len(rows),
                    "requeued": len(result.requeued),
                    "abandoned": result.abandoned,
                    "released": len(result.releases),
                },
            )
        return result

    async def _release(
        self, tenant_id: uuid.UUID, conversation_id: uuid.UUID, *, trigger: uuid.UUID
    ) -> AgentJob | None:
        """The same decision the worker makes, under the same gate.

        The lock serialises this against a worker finishing a sibling file on
        the same conversation, so between them exactly one sees zero
        unresolved and exactly one turn is owed.
        """
        await ConversationMediaGate(self._session, tenant_id=tenant_id).lock(conversation_id)
        remaining = await MediaRepository(self._session, tenant_id=tenant_id).count_unresolved(
            conversation_id
        )
        if remaining:
            return None
        return AgentJob(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            trigger_message_id=trigger,
        )

    def _service(self, tenant_id: uuid.UUID) -> MediaService:
        return MediaService(
            session=self._session,
            tenant_id=tenant_id,
            settings=self._settings,
            storage=self._storage,
        )


__all__ = ["RECOVERY_BATCH_SIZE", "MediaRecoveryPass", "MediaRecoveryService"]
