"""New agents can read the knowledge base; a workspace whose agent cannot is told (RQ-11, RC-7).

P-R18: at the base head an agent was created with no tools at all. A workspace
that uploaded documents and activated that agent was answered from the prompt
alone - every question about its prices or policies - and nothing anywhere
said the documents were unreachable.

Real PostgreSQL through the real `AgentService`; the grant and the flag are read
back from the database, not from the service's own objects.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.agents.registry import SEARCH_KNOWLEDGE_TOOL
from app.db.models.agent import AgentStatus, AgentTool
from app.db.models.audit import AuditAction
from app.services.agent_service import AgentService
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration


async def _grants(indexing: Indexing, agent_id: uuid.UUID) -> list[tuple[str, bool]]:
    async with indexing.database.session() as session:
        rows = await session.execute(
            select(AgentTool.name, AgentTool.enabled).where(AgentTool.agent_id == agent_id)
        )
        return [(name, enabled) for name, enabled in rows]


async def _answering_agent(
    indexing: Indexing, tenant_id: uuid.UUID, **settings: object
) -> uuid.UUID:
    indexing.configure(**settings)
    async with indexing.database.session() as session:
        service = AgentService(session=session, settings=indexing.settings, tenant_id=tenant_id)
        agent = await service.create(name="Sales", system_prompt="Be helpful.")
        await service.update(agent.id, status=AgentStatus.ACTIVE)
        await service.make_default(agent.id)
        return agent.id


async def _unreachable(indexing: Indexing, tenant_id: uuid.UUID) -> bool:
    async with indexing.database.session() as session:
        return await AgentService(
            session=session, settings=indexing.settings, tenant_id=tenant_id
        ).knowledge_unreachable()


async def test_a_new_agent_can_search_knowledge_and_nothing_else(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    await indexing.submit(tenant_id)
    await indexing.drain()

    agent_id = await _answering_agent(indexing, tenant_id)

    assert await _grants(indexing, agent_id) == [(SEARCH_KNOWLEDGE_TOOL, True)]
    assert not await _unreachable(indexing, tenant_id)
    (granted,) = await indexing.audit(tenant_id, AuditAction.AGENT_TOOL_GRANTED)
    assert granted.target_label == SEARCH_KNOWLEDGE_TOOL


async def test_a_workspace_whose_agent_cannot_search_is_flagged_until_it_can(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    await indexing.submit(tenant_id)
    await indexing.drain()
    # What every agent created before this stage looks like.
    agent_id = await _answering_agent(indexing, tenant_id, agent_default_knowledge_grant=False)
    assert await _grants(indexing, agent_id) == []

    assert await _unreachable(indexing, tenant_id)

    async with indexing.database.session() as session:
        await AgentService(
            session=session, settings=indexing.settings, tenant_id=tenant_id
        ).grant_tool(agent_id, name=SEARCH_KNOWLEDGE_TOOL)
    assert not await _unreachable(indexing, tenant_id)


async def test_the_flag_is_one_workspaces_fact(indexing: Indexing) -> None:
    flagged = await indexing.workspace()
    await indexing.submit(flagged)
    other = await indexing.workspace()
    await indexing.drain()
    await _answering_agent(indexing, flagged, agent_default_knowledge_grant=False)
    await _answering_agent(indexing, other, agent_default_knowledge_grant=False)

    assert await _unreachable(indexing, flagged)
    # No documents of its own: another workspace's are none of its business.
    assert not await _unreachable(indexing, other)
