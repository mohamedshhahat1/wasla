"""Reading the audit trail.

Two readers, and the split is the one this codebase makes everywhere a query
legitimately crosses workspaces. A workspace reads its own entries through the
scoped repository; platform staff read every entry — including the ones with no
workspace at all — through a separate class that is obviously not scoped.

There is no write method here. Entries are staged by `AuditTrail`, and giving
this class an `update` or a `delete` would be handing somebody the ability to
edit the record of what they did.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, Select

from app.db.models.audit import AuditAction, AuditLog
from app.repositories.base import BaseRepository, TenantScopedRepository


def _filtered(
    statement: Select[tuple[AuditLog]],
    *,
    actions: Sequence[AuditAction] | None,
    actor_id: uuid.UUID | None,
    since: datetime | None,
    until: datetime | None,
) -> Select[tuple[AuditLog]]:
    """The same predicate for both readers, so they cannot disagree."""
    if actions:
        statement = statement.where(AuditLog.action.in_(list(actions)))
    if actor_id is not None:
        statement = statement.where(AuditLog.actor_id == actor_id)
    if since is not None:
        statement = statement.where(AuditLog.occurred_at >= since)
    if until is not None:
        statement = statement.where(AuditLog.occurred_at < until)
    return statement


class AuditLogRepository(TenantScopedRepository[AuditLog]):
    """One workspace's trail."""

    model = AuditLog

    def _tenant_filter(self) -> ColumnElement[bool]:
        return AuditLog.tenant_id == self.tenant_id

    async def list_entries(
        self,
        *,
        actions: Sequence[AuditAction] | None = None,
        actor_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        """Newest first, which is the only order anybody reads a log in."""
        statement = _filtered(
            self._select(),
            actions=actions,
            actor_id=actor_id,
            since=since,
            until=until,
        )
        return await self._all(
            statement.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit)
        )


class PlatformAuditLogRepository(BaseRepository[AuditLog]):
    """Every entry, across every workspace and the platform itself.

    Deliberately unscoped and deliberately its own class. Nothing constructs it
    except the platform layer, whose routes are behind the platform-role
    dependency — and the entries it exists to show are precisely the ones those
    same people generate.
    """

    model = AuditLog

    async def list_entries(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
        actions: Sequence[AuditAction] | None = None,
        actor_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        """Newest first, optionally narrowed to one workspace.

        `tenant_id` narrows; it does not scope. Omitting it returns platform
        actions alongside workspace ones, which is the view an investigation
        actually needs.
        """
        statement = _filtered(
            self._select(),
            actions=actions,
            actor_id=actor_id,
            since=since,
            until=until,
        )
        if tenant_id is not None:
            statement = statement.where(AuditLog.tenant_id == tenant_id)
        return await self._all(
            statement.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit)
        )
