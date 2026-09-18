"""The purge's ledger of object deletes it still owes (MEDIA-07).

Platform-wide on purpose, like `PlatformMediaRepository`: the drain is a sweep
across every purged workspace, and nothing reachable from a request constructs
it. The rows name keys in the object store and nothing else.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.media_purge import MediaPurgeObject


@dataclass(frozen=True, slots=True)
class OwedDelete:
    """One key the store still has to confirm is gone."""

    entry_id: uuid.UUID
    storage_key: str


class MediaPurgeLedger:
    """Keys recorded by workspace purges and not yet confirmed deleted."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def due(self, *, now: datetime, limit: int) -> list[OwedDelete]:
        """Keys whose delete may be attempted now, the longest-owed first."""
        rows = await self._session.execute(
            select(MediaPurgeObject.id, MediaPurgeObject.storage_key)
            .where(MediaPurgeObject.not_before <= now)
            .order_by(MediaPurgeObject.not_before)
            .limit(limit)
        )
        return [OwedDelete(entry_id=entry, storage_key=key) for entry, key in rows]

    async def settle(self, entry_id: uuid.UUID) -> None:
        """The store confirmed the object is gone; the debt is paid."""
        await self._session.execute(delete(MediaPurgeObject).where(MediaPurgeObject.id == entry_id))

    async def refused(self, entry_id: uuid.UUID, *, now: datetime) -> None:
        """The store would not delete it this time. The row stays, and says so."""
        await self._session.execute(
            update(MediaPurgeObject)
            .where(MediaPurgeObject.id == entry_id)
            .values(attempts=MediaPurgeObject.attempts + 1, last_attempt_at=now)
        )

    async def outstanding(self, tenant_id: uuid.UUID | None = None) -> int:
        """How many deletes are still owed, overall or for one workspace."""
        statement = select(func.count()).select_from(MediaPurgeObject)
        if tenant_id is not None:
            statement = statement.where(MediaPurgeObject.tenant_id == tenant_id)
        return int((await self._session.execute(statement)).scalar_one())

    async def backlog(self, *, now: datetime) -> tuple[int, float]:
        """How many deletes are owed and how long the oldest has been owed.

        The gauge the purge alert reads. Age is from when the purge recorded
        the key, not from `not_before`: a delete that has been refused for a day
        is a day old whatever grace it started with.
        """
        count, oldest = (
            await self._session.execute(
                select(func.count(), func.min(MediaPurgeObject.created_at)).select_from(
                    MediaPurgeObject
                )
            )
        ).one()
        age = (now - oldest).total_seconds() if oldest is not None else 0.0
        return int(count), max(age, 0.0)


__all__ = ["MediaPurgeLedger", "OwedDelete"]
