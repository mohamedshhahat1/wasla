"""Put an e2e workspace into a team state the API cannot reach in a few calls (FE-7, EC7-04).

    python -m tests.e2e_stack.team_states expired-invitation <invitation-id>
        the pending invitation expired a day ago (the API issues them for 7 days)
    python -m tests.e2e_stack.team_states every-activity <workspace-slug>
        the workspace's activity log holds one entry of every action the product
        can record, written as the system with a fixed label - so the activity
        page's words, and its fallback for an action it has no words for, are
        all reachable

Test-only, like the rest of ``tests.e2e_stack``: nothing under ``app/`` imports it
(``tests/unit/test_e2e_gate_states.py`` holds that for the package). The journeys
run it with ``docker exec wasla-fe-e2e-api``. Everything else FE-7's journeys need
is made through the API, the mailbox and the fake Google - an invitation, a
member removed and reinstated, the plan's seats used up by inviting, an owner
whose workspace blocks closing the account, a workspace suspended by staff, three
workspaces chosen between by switching.

Rows are written through the application's models. Each command prints one JSON
line naming what it made, read back from the database.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

COMMANDS: Final = ("expired-invitation", "every-activity")
USAGE: Final = (
    "usage: python -m tests.e2e_stack.team_states "
    "(expired-invitation <invitation-id> | every-activity <workspace-slug>)"
)
ACTIVITY_LABEL: Final = "e2e activity"


def parse(argv: list[str]) -> tuple[str, str]:
    """(command, target) or the usage, before any connection is opened."""
    if len(argv) == 2 and argv[0] in COMMANDS:
        if argv[0] == "expired-invitation":
            try:
                uuid.UUID(argv[1])
            except ValueError:
                raise SystemExit(USAGE) from None
        elif not argv[1]:
            raise SystemExit(USAGE)
        return argv[0], argv[1]
    raise SystemExit(USAGE)


async def apply(session: AsyncSession, command: str, target: str) -> dict[str, object]:
    from app.db.models import InvitationStatus, Tenant
    from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
    from app.db.models.invitation import TenantInvitation
    from app.services.audit_service import AuditTrail

    if command == "expired-invitation":
        invitation = await session.scalar(
            select(TenantInvitation).where(TenantInvitation.id == uuid.UUID(target))
        )
        if invitation is None or invitation.status is not InvitationStatus.PENDING:
            raise SystemExit(f"no pending invitation {target}")
        invitation.expires_at = datetime.now(UTC) - timedelta(days=1)
        await session.flush()
        return {
            "invitation_id": str(invitation.id),
            "expires_at": invitation.expires_at.isoformat(),
            "status": invitation.status.value,
        }

    tenant = await session.scalar(select(Tenant).where(Tenant.slug == target))
    if tenant is None:
        raise SystemExit(f"no workspace {target}")
    trail = AuditTrail(session, tenant_id=tenant.id)
    for action in AuditAction:
        trail.record(
            action,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="workspace",
            target_id=tenant.id,
            target_label=ACTIVITY_LABEL,
        )
    await session.flush()
    written = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.tenant_id == tenant.id, AuditLog.target_label == ACTIVITY_LABEL)
    )
    return {"workspace": tenant.slug, "entries": int(written or 0), "actions": len(AuditAction)}


async def run(argv: list[str]) -> dict[str, object]:
    from app.core.config import get_settings
    from app.db.session import Database

    command, target = parse(argv)
    database = Database(get_settings())
    try:
        async with database.session() as session:
            made = await apply(session, command, target)
            await session.commit()
        return made
    finally:
        await database.dispose()


def main(argv: list[str]) -> None:
    made = asyncio.run(run(argv))
    print(json.dumps(made, sort_keys=True))  # noqa: T201 - this script's output is its interface


if __name__ == "__main__":
    main(sys.argv[1:])
