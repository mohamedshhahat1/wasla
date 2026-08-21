"""Agent persistence against PostgreSQL.

The unit companion reads the mapped metadata; these prove the database actually
enforces what that metadata promises - the per-workspace name constraint, the
grant cascade - and that neither can be reached across workspaces.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, TenantIsolationError
from app.db.models.agent import Agent, AgentStatus, AgentTool
from app.db.models.tenant import Tenant
from app.repositories.agent_repository import AgentRepository, AgentToolRepository

pytestmark = pytest.mark.integration

MODEL = "claude-opus-5"
PROMPT = "You answer as the sales desk."


async def _tenant(session, *, slug):
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _agent(session, *, tenant, name="Sales", **overrides):
    repository = AgentRepository(session, tenant_id=tenant.id)
    agent = await repository.create(name=name, model=MODEL, system_prompt=PROMPT, **overrides)
    await session.flush()
    return agent


async def test_two_workspaces_can_both_name_an_agent_sales(db_session):
    first = await _tenant(db_session, slug="first")
    second = await _tenant(db_session, slug="second")

    mine = await _agent(db_session, tenant=first)
    theirs = await _agent(db_session, tenant=second)

    assert mine.id != theirs.id
    assert mine.name == theirs.name == "Sales"


async def test_a_duplicate_name_in_one_workspace_is_a_conflict(db_session):
    tenant = await _tenant(db_session, slug="acme")
    await _agent(db_session, tenant=tenant)

    with pytest.raises(ConflictError):
        await _agent(db_session, tenant=tenant)


async def test_the_database_rejects_a_duplicate_name_even_without_the_check(db_session):
    """The constraint is the guarantee; the repository check is only a message."""
    tenant = await _tenant(db_session, slug="acme")
    await _agent(db_session, tenant=tenant)

    db_session.add(Agent(tenant_id=tenant.id, name="Sales", model=MODEL, system_prompt=PROMPT))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_another_workspaces_agent_is_not_found(db_session):
    mine = await _tenant(db_session, slug="mine")
    theirs = await _tenant(db_session, slug="theirs")
    hidden = await _agent(db_session, tenant=theirs)

    repository = AgentRepository(db_session, tenant_id=mine.id)

    assert await repository.get_by_id(hidden.id) is None
    with pytest.raises(TenantIsolationError):
        await repository.require_by_id(hidden.id)


async def test_a_disabled_default_answers_nobody(db_session):
    """ "No agent" is the right answer, not "here is a disabled configuration"."""
    tenant = await _tenant(db_session, slug="acme")
    await _agent(db_session, tenant=tenant, status=AgentStatus.DISABLED, is_default=True)

    repository = AgentRepository(db_session, tenant_id=tenant.id)

    assert await repository.get_answering_default() is None


async def test_an_active_default_answers(db_session):
    tenant = await _tenant(db_session, slug="acme")
    agent = await _agent(db_session, tenant=tenant, status=AgentStatus.ACTIVE, is_default=True)

    repository = AgentRepository(db_session, tenant_id=tenant.id)

    found = await repository.get_answering_default()
    assert found is not None
    assert found.id == agent.id


async def test_clearing_defaults_leaves_another_workspace_alone(db_session):
    mine = await _tenant(db_session, slug="mine")
    theirs = await _tenant(db_session, slug="theirs")
    await _agent(db_session, tenant=mine, status=AgentStatus.ACTIVE, is_default=True)
    untouched = await _agent(db_session, tenant=theirs, status=AgentStatus.ACTIVE, is_default=True)

    await AgentRepository(db_session, tenant_id=mine.id).clear_defaults()
    await db_session.flush()

    assert untouched.is_default is True
    assert await AgentRepository(db_session, tenant_id=mine.id).get_answering_default() is None


async def test_a_tool_is_granted_once_per_agent(db_session):
    tenant = await _tenant(db_session, slug="acme")
    agent = await _agent(db_session, tenant=tenant)
    tools = AgentToolRepository(db_session, tenant_id=tenant.id)

    await tools.upsert(agent_id=agent.id, name="request_human_handoff")
    await db_session.flush()

    db_session.add(AgentTool(tenant_id=tenant.id, agent_id=agent.id, name="request_human_handoff"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_regranting_updates_rather_than_duplicating(db_session):
    tenant = await _tenant(db_session, slug="acme")
    agent = await _agent(db_session, tenant=tenant)
    tools = AgentToolRepository(db_session, tenant_id=tenant.id)

    await tools.upsert(agent_id=agent.id, name="request_human_handoff", config={"reason": "angry"})
    await db_session.flush()
    await tools.upsert(agent_id=agent.id, name="request_human_handoff", enabled=False)
    await db_session.flush()

    grants = await tools.list_for_agent(agent_id=agent.id, enabled_only=False)
    assert len(grants) == 1
    assert grants[0].enabled is False
    # An absent config must not silently reset stored settings.
    assert grants[0].config == {"reason": "angry"}


async def test_a_disabled_grant_is_hidden_from_the_orchestrator(db_session):
    tenant = await _tenant(db_session, slug="acme")
    agent = await _agent(db_session, tenant=tenant)
    tools = AgentToolRepository(db_session, tenant_id=tenant.id)

    await tools.upsert(agent_id=agent.id, name="request_human_handoff", enabled=False)
    await db_session.flush()

    assert await tools.list_for_agent(agent_id=agent.id) == []
    assert len(await tools.list_for_agent(agent_id=agent.id, enabled_only=False)) == 1


async def test_grants_die_with_their_agent(db_session):
    tenant = await _tenant(db_session, slug="acme")
    agent = await _agent(db_session, tenant=tenant)
    tools = AgentToolRepository(db_session, tenant_id=tenant.id)
    await tools.upsert(agent_id=agent.id, name="request_human_handoff")
    await db_session.flush()

    await db_session.execute(delete(Agent).where(Agent.id == agent.id))
    await db_session.flush()

    remaining = await db_session.execute(select(AgentTool).where(AgentTool.agent_id == agent.id))
    assert remaining.scalars().all() == []


async def test_another_workspaces_grant_is_invisible(db_session):
    mine = await _tenant(db_session, slug="mine")
    theirs = await _tenant(db_session, slug="theirs")
    hidden = await _agent(db_session, tenant=theirs)
    await AgentToolRepository(db_session, tenant_id=theirs.id).upsert(
        agent_id=hidden.id,
        name="request_human_handoff",
    )
    await db_session.flush()

    # Even naming their agent id directly, the tenant filter answers nothing.
    mine_tools = AgentToolRepository(db_session, tenant_id=mine.id)
    assert await mine_tools.list_for_agent(agent_id=hidden.id, enabled_only=False) == []
    assert await mine_tools.get(agent_id=hidden.id, name="request_human_handoff") is None


async def test_an_unknown_agent_id_is_not_found(db_session):
    tenant = await _tenant(db_session, slug="acme")
    repository = AgentRepository(db_session, tenant_id=tenant.id)

    with pytest.raises(TenantIsolationError):
        await repository.require_by_id(uuid.uuid4())
