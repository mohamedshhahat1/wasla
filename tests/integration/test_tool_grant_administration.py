"""Who decided what an agent may do, and what a grant's settings actually mean.

**Granting a tool was not audited** (TOOL-13). It is the decision that sets what
a model is able to do to a workspace's customers - hand conversations over,
write to the CRM, arrange a message - and it was the one privileged
configuration change in the product that wrote no row, while workspace
suspension, number release and model configuration all write one. "Who gave this
agent this capability, and when" is the first question after a prompt-injection
report, and it was unanswerable.

**`agent_tools.config` was fiction** (TOOL-14). It was stored, bounded by a
schema, documented on the model as carrying per-grant policy - "such as which
lead statuses a tool may write" - and read by nothing at all. An administrator
could set it, watch it come back on the next read, and believe a tool was
constrained. Nothing was. The column stays for the first tool that needs a knob;
the pretence does not.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import HANDOFF_TOOL, RECORD_LEAD_TOOL
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.agent import Agent, AgentStatus
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.agent_service import AgentService

pytestmark = pytest.mark.integration

PROMPT = "You answer as the sales desk."


async def _setup(
    session: AsyncSession, settings: Settings
) -> tuple[Tenant, Agent, User, AgentService]:
    slug = f"grants-{uuid.uuid4().hex[:8]}"
    tenant = Tenant(name="Grants", slug=slug)
    admin = User(
        email=f"{slug}@example.test",
        hashed_password="x" * 20,
        full_name="Nadia Admin",
    )
    session.add_all([tenant, admin])
    await session.flush()

    service = AgentService(session=session, settings=settings, tenant_id=tenant.id)
    agent = await service.create(name="Sales", system_prompt=PROMPT)
    agent.status = AgentStatus.ACTIVE
    await session.flush()
    return tenant, agent, admin, service


async def _rows(session: AsyncSession, tenant: Tenant) -> list[AuditLog]:
    rows = await session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.occurred_at)
    )
    return list(rows)


async def test_granting_a_tool_is_recorded_against_the_person_who_granted_it(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant, agent, admin, service = await _setup(db_session, settings)

    grant = await service.grant_tool(agent.id, name=HANDOFF_TOOL, actor=admin)
    await db_session.flush()

    rows = await _rows(db_session, tenant)
    assert [row.action for row in rows] == [AuditAction.AGENT_TOOL_GRANTED]
    entry = rows[0]
    assert entry.actor_id == admin.id
    assert entry.actor_kind is AuditActorKind.USER
    assert entry.target_type == "agent_tool"
    assert entry.target_id == grant.id
    assert entry.meta == {"agent_id": str(agent.id), "tool": HANDOFF_TOOL, "enabled": True}


async def test_revoking_a_tool_is_recorded_too(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant, agent, admin, service = await _setup(db_session, settings)
    await service.grant_tool(agent.id, name=RECORD_LEAD_TOOL, actor=admin)

    await service.revoke_tool(agent.id, name=RECORD_LEAD_TOOL, actor=admin)
    await db_session.flush()

    rows = await _rows(db_session, tenant)
    assert [row.action for row in rows] == [
        AuditAction.AGENT_TOOL_GRANTED,
        AuditAction.AGENT_TOOL_REVOKED,
    ]
    assert rows[1].meta == {"agent_id": str(agent.id), "tool": RECORD_LEAD_TOOL, "enabled": False}


async def test_granting_a_tool_switched_off_is_recorded_as_a_withdrawal(
    db_session: AsyncSession, settings: Settings
) -> None:
    """The row on the table is what matters, not which endpoint reached it."""
    tenant, agent, admin, service = await _setup(db_session, settings)

    await service.grant_tool(agent.id, name=RECORD_LEAD_TOOL, enabled=False, actor=admin)
    await db_session.flush()

    rows = await _rows(db_session, tenant)
    assert [row.action for row in rows] == [AuditAction.AGENT_TOOL_REVOKED]


async def test_an_audit_row_never_lands_in_another_workspace(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant, agent, admin, service = await _setup(db_session, settings)
    other, _, _, _ = await _setup(db_session, settings)

    await service.grant_tool(agent.id, name=HANDOFF_TOOL, actor=admin)
    await db_session.flush()

    assert [row.tenant_id for row in await _rows(db_session, tenant)] == [tenant.id]
    assert await _rows(db_session, other) == []


async def test_per_grant_settings_are_refused_rather_than_silently_ignored(
    db_session: AsyncSession, settings: Settings
) -> None:
    """An administrator must not be able to believe a tool is constrained (TOOL-14)."""
    _, agent, admin, service = await _setup(db_session, settings)

    with pytest.raises(ValidationError) as refusal:
        await service.grant_tool(
            agent.id,
            name=RECORD_LEAD_TOOL,
            config={"statuses": ["new", "qualified"]},
            actor=admin,
        )
    assert "reserved" in str(refusal.value)


async def test_an_empty_settings_object_is_still_accepted(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A client that always sends the field is not broken by the refusal above."""
    _, agent, admin, service = await _setup(db_session, settings)

    grant = await service.grant_tool(agent.id, name=RECORD_LEAD_TOOL, config={}, actor=admin)

    assert grant.enabled is True
