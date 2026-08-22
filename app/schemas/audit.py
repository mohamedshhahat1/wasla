"""Audit log API contracts.

Read-only, and there is no write schema anywhere: entries are staged by the
services that perform the actions, never posted by a client. A log somebody can
write to by hand answers nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel

from app.db.models.audit import AuditAction, AuditActorKind, AuditLog


class AuditEntryRead(BaseModel):
    """One recorded act.

    The labels are the copies stored at write time, not joins. That is what
    makes an entry about a deleted account still readable, which is exactly
    when somebody is looking.
    """

    id: str
    action: AuditAction
    actor_kind: AuditActorKind
    actor_id: str | None
    actor_label: str | None
    target_type: str | None
    target_id: str | None
    target_label: str | None
    tenant_id: str | None
    occurred_at: datetime
    metadata: dict[str, Any] | None

    @classmethod
    def from_model(cls, entry: AuditLog) -> Self:
        return cls(
            id=str(entry.id),
            action=entry.action,
            actor_kind=entry.actor_kind,
            actor_id=str(entry.actor_id) if entry.actor_id else None,
            actor_label=entry.actor_label,
            target_type=entry.target_type,
            target_id=str(entry.target_id) if entry.target_id else None,
            target_label=entry.target_label,
            tenant_id=str(entry.tenant_id) if entry.tenant_id else None,
            occurred_at=entry.occurred_at,
            metadata=entry.meta,
        )


__all__ = ["AuditEntryRead"]
