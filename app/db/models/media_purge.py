"""Objects a workspace purge still owes the store a delete for (MEDIA-07).

A workspace purge deletes the rows that name a workspace's files in one
transaction and the objects afterwards, because an object delete is network I/O
and must not hold that transaction open. Until this table existed the list of
keys lived only in the purge worker's memory between the two: an object delete
that failed - or a process that died after the commit - left customer files in
the bucket with no row anywhere naming them, and the workspace recorded as
purged. The only trace was a warning line.

So the keys are written here **in the same transaction that deletes their
rows** - captured from the `DELETE ... RETURNING` itself, so a key allocated by
an upload that committed a moment earlier cannot slip between a read of the
keys and the delete. A row here is a delete the store still owes, durable across
a crash, a refused delete and any number of retries. It is removed only once
the store has confirmed the object is gone, so "purge complete" is: the
workspace is marked purged *and* none of its keys remain here.

`not_before` is the fence for writes already in flight. A key whose upload
intent was committed but not finished may still have its object written after
the purge; deleting it at once would race that write and could leave the
object behind. Such keys are not deleted before the upload grace period has
passed - the same horizon reconciliation already trusts for an interrupted
write (ADR-087) - so the delete lands after the late write rather than before
it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MediaPurgeObject(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One object key whose delete a workspace purge still owes."""

    __tablename__ = "media_purge_objects"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_media_purge_objects_storage_key"),
        Index("ix_media_purge_objects_not_before", "not_before"),
        Index("ix_media_purge_objects_tenant_id", "tenant_id"),
    )

    # The purged workspace. `RESTRICT`, not a cascade: this row is the only
    # record of an object still in the store, and nothing may erase it by
    # erasing something else.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    # No delete before this instant; see the module docstring.
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
