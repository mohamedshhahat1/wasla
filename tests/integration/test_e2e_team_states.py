"""The e2e seed's team states (FE-7, EC7-04): parsed before connecting, written as read."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InvitationStatus, TenantRole
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.invitation import TenantInvitation
from app.db.models.tenant import Tenant
from tests.e2e_stack import team_states
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["expired-invitation"],
        ["unknown", "x"],
        ["expired-invitation", "not-a-uuid"],
        ["every-activity", ""],
        ["a", "b", "c"],
    ],
)
def test_an_incomplete_command_is_refused_with_the_usage(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        team_states.parse(argv)
    assert refused.value.code == team_states.USAGE


def test_every_command_parses() -> None:
    target = str(uuid.uuid4())
    assert team_states.parse(["expired-invitation", target]) == ("expired-invitation", target)
    assert team_states.parse(["every-activity", "acme"]) == ("every-activity", "acme")


async def _tenant(session: AsyncSession, slug: str) -> Tenant:
    tenant = Tenant(timezone=TEST_TIMEZONE, name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def test_an_invitation_is_expired_and_stays_pending(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, "team-expired")
    invitation = TenantInvitation(
        tenant_id=tenant.id,
        email="expired@example.com",
        role=TenantRole.MEMBER,
        status=InvitationStatus.PENDING,
        token_hash=uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db_session.add(invitation)
    await db_session.flush()

    made = await team_states.apply(db_session, "expired-invitation", str(invitation.id))

    assert made["status"] == "pending"
    assert invitation.expires_at < datetime.now(UTC)
    assert not invitation.is_open(now=datetime.now(UTC))


async def test_an_unknown_invitation_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(SystemExit):
        await team_states.apply(db_session, "expired-invitation", str(uuid.uuid4()))


async def test_every_activity_writes_each_action_once_to_that_workspace(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, "team-activity")
    other = await _tenant(db_session, "team-other")

    made = await team_states.apply(db_session, "every-activity", tenant.slug)

    assert made == {
        "workspace": tenant.slug,
        "entries": len(AuditAction),
        "actions": len(AuditAction),
    }
    actions = (
        await db_session.scalars(select(AuditLog.action).where(AuditLog.tenant_id == tenant.id))
    ).all()
    assert sorted(actions) == sorted(AuditAction)
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == other.id)
        )
    ) == 0
