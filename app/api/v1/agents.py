"""Agent configuration endpoints.

Reading is open to any member of the workspace; changing what customers are told
is not, so every write requires an admin or the owner.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import AgentServiceDep, TenantAdminDep
from app.schemas.agent import (
    AgentCreate,
    AgentRead,
    AgentUpdate,
    ToolGrantRead,
    ToolGrantRequest,
    ToolSpecRead,
)

router = APIRouter(prefix="/agents", tags=["agents"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("")
async def list_agents(service: AgentServiceDep, limit: LimitQuery = 50) -> list[AgentRead]:
    agents = await service.list_agents(limit=limit)
    return [AgentRead.model_validate(agent) for agent in agents]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    service: AgentServiceDep,
    admin: TenantAdminDep,
) -> AgentRead:
    agent = await service.create(
        name=payload.name,
        system_prompt=payload.system_prompt,
        description=payload.description,
        model=payload.model,
        temperature=payload.temperature,
        max_output_tokens=payload.max_output_tokens,
        memory_message_limit=payload.memory_message_limit,
        memory_token_budget=payload.memory_token_budget,
    )
    return AgentRead.model_validate(agent)


@router.get("/available-tools")
async def list_available_tools(service: AgentServiceDep) -> list[ToolSpecRead]:
    """Tools this build can run, whether or not any agent has them.

    A static path so it can never be mistaken for an agent identifier.
    """
    return [ToolSpecRead.model_validate(spec) for spec in service.available_tools()]


@router.get("/{agent_id}")
async def read_agent(agent_id: uuid.UUID, service: AgentServiceDep) -> AgentRead:
    return AgentRead.model_validate(await service.get(agent_id))


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: uuid.UUID,
    payload: AgentUpdate,
    service: AgentServiceDep,
    admin: TenantAdminDep,
) -> AgentRead:
    agent = await service.update(
        agent_id,
        name=payload.name,
        system_prompt=payload.system_prompt,
        description=payload.description,
        model=payload.model,
        status=payload.status,
        temperature=payload.temperature,
        max_output_tokens=payload.max_output_tokens,
        memory_message_limit=payload.memory_message_limit,
        memory_token_budget=payload.memory_token_budget,
    )
    return AgentRead.model_validate(agent)


@router.post("/{agent_id}/default")
async def make_default(
    agent_id: uuid.UUID,
    service: AgentServiceDep,
    admin: TenantAdminDep,
) -> AgentRead:
    """Make this the agent that answers when nothing more specific applies."""
    return AgentRead.model_validate(await service.make_default(agent_id))


@router.get("/{agent_id}/tools")
async def list_agent_tools(
    agent_id: uuid.UUID,
    service: AgentServiceDep,
) -> list[ToolGrantRead]:
    grants = await service.list_tools(agent_id)
    return [ToolGrantRead.model_validate(grant) for grant in grants]


@router.put("/{agent_id}/tools")
async def grant_tool(
    agent_id: uuid.UUID,
    payload: ToolGrantRequest,
    service: AgentServiceDep,
    admin: TenantAdminDep,
) -> ToolGrantRead:
    """Grant a tool, or change an existing grant. Idempotent, hence PUT."""
    grant = await service.grant_tool(
        agent_id,
        name=payload.name,
        enabled=payload.enabled,
        config=payload.config,
    )
    return ToolGrantRead.model_validate(grant)


@router.delete(
    "/{agent_id}/tools/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    # See the note on the logout route: a 204 must declare no model.
    response_model=None,
)
async def revoke_tool(
    agent_id: uuid.UUID,
    name: str,
    service: AgentServiceDep,
    admin: TenantAdminDep,
) -> None:
    """Withdraw a tool.

    The grant is disabled rather than deleted, so anything configured with it
    survives being turned back on.
    """
    await service.revoke_tool(agent_id, name=name)
