"""A workspace's own audit trail.

Open to owners and administrators, not to every member. The trail names who did
what, and in a small company that is a record of colleagues' actions - the
people who can already see the workspace's billing are the right ones to see it.

Read-only, and there is no route that writes an entry. Entries are staged by the
services that perform the actions, in the same transaction, so an entry cannot
exist for something that did not happen and cannot be missing from something
that did.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import AuditLogRepositoryDep, TenantAdminDep
from app.api.route import CommittingRoute
from app.db.models.audit import AuditAction
from app.schemas.audit import AuditEntryRead

router = APIRouter(route_class=CommittingRoute, prefix="/audit-logs", tags=["audit"])

LimitQuery = Annotated[int, Query(ge=1, le=200)]
SinceQuery = Annotated[datetime | None, Query(description="Start of the window, inclusive (UTC)")]
UntilQuery = Annotated[datetime | None, Query(description="End of the window, exclusive (UTC)")]


@router.get("", response_model=list[AuditEntryRead])
async def list_audit_entries(
    workspace: TenantAdminDep,
    entries: AuditLogRepositoryDep,
    action: Annotated[list[AuditAction] | None, Query()] = None,
    actor_id: uuid.UUID | None = None,
    since: SinceQuery = None,
    until: UntilQuery = None,
    limit: LimitQuery = 50,
) -> list[AuditEntryRead]:
    """This workspace's trail, newest first. Owners and administrators.

    A platform action taken *against* this workspace - a payment recorded, an
    invoice voided - appears here too, attributed to the staff member who did
    it. The customer is entitled to see who marked their invoice paid.
    """
    rows = await entries.list_entries(
        actions=action,
        actor_id=actor_id,
        since=since,
        until=until,
        limit=limit,
    )
    return [AuditEntryRead.from_model(row) for row in rows]
