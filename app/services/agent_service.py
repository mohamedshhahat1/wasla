"""Agent configuration.

Separate from the orchestrator on purpose: this decides what an agent is, the
orchestrator decides what it says. Mixing them would mean a configuration
screen and a customer reply shared a code path.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import ToolRegistry, build_default_registry
from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.agent import (
    DEFAULT_MEMORY_MESSAGE_LIMIT,
    DEFAULT_MEMORY_TOKEN_BUDGET,
    DEFAULT_TEMPERATURE,
    Agent,
    AgentStatus,
    AgentTool,
)
from app.integrations.openai.types import ToolSpec
from app.repositories.agent_repository import AgentRepository, AgentToolRepository

logger = get_logger(__name__)


class AgentService:
    """Configures the agents of one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        tenant_id: uuid.UUID,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._agents = AgentRepository(session, tenant_id=tenant_id)
        self._grants = AgentToolRepository(session, tenant_id=tenant_id)
        self._registry = registry if registry is not None else build_default_registry()

    async def list_agents(self, *, limit: int = 50) -> list[Agent]:
        return await self._agents.list_all(limit=limit)

    async def get(self, agent_id: uuid.UUID) -> Agent:
        return await self._agents.require_by_id(agent_id)

    async def create(
        self,
        *,
        name: str,
        system_prompt: str,
        description: str | None = None,
        model: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_output_tokens: int | None = None,
        memory_message_limit: int = DEFAULT_MEMORY_MESSAGE_LIMIT,
        memory_token_budget: int = DEFAULT_MEMORY_TOKEN_BUDGET,
    ) -> Agent:
        """Create an agent as a draft.

        Drafts rather than active: an agent that answered customers the moment
        it was created would go live before anyone had read its prompt.
        """
        agent = await self._agents.create(
            name=name.strip(),
            model=model or self._settings.openai_model,
            system_prompt=system_prompt,
            description=description,
            status=AgentStatus.DRAFT,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            memory_message_limit=memory_message_limit,
            memory_token_budget=memory_token_budget,
        )
        # Flushed so the caller can read the generated id.
        await self._session.flush()
        logger.info("agent.created", extra={"agent_id": str(agent.id)})
        return agent

    async def update(
        self,
        agent_id: uuid.UUID,
        *,
        name: str | None = None,
        system_prompt: str | None = None,
        description: str | None = None,
        model: str | None = None,
        status: AgentStatus | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        memory_message_limit: int | None = None,
        memory_token_budget: int | None = None,
    ) -> Agent:
        """Apply changes. Anything left as None is unchanged."""
        agent = await self._agents.require_by_id(agent_id)

        if name is not None:
            await self._rename(agent, name.strip())
        if system_prompt is not None:
            agent.system_prompt = system_prompt
        if description is not None:
            agent.description = description
        if model is not None:
            agent.model = model
        if status is not None:
            agent.status = status
        if temperature is not None:
            agent.temperature = temperature
        if max_output_tokens is not None:
            agent.max_output_tokens = max_output_tokens
        if memory_message_limit is not None:
            agent.memory_message_limit = memory_message_limit
        if memory_token_budget is not None:
            agent.memory_token_budget = memory_token_budget

        logger.info("agent.updated", extra={"agent_id": str(agent.id)})
        return agent

    async def _rename(self, agent: Agent, name: str) -> None:
        """Rename, refusing a name already used in this workspace.

        Checked rather than left to the unique constraint so the caller gets a
        sentence instead of a driver error.
        """
        if name == agent.name:
            return
        if await self._agents.get_by_name(name) is not None:
            raise ConflictError("An agent with that name already exists.")
        agent.name = name

    async def make_default(self, agent_id: uuid.UUID) -> Agent:
        """Promote one agent to the workspace default.

        Its own operation rather than a field on update, because it is not a
        property of one agent: it demotes another. A draft is refused, since a
        workspace showing a configured default that cannot answer is worse than
        one showing no default at all.
        """
        agent = await self._agents.require_by_id(agent_id)
        if agent.status is not AgentStatus.ACTIVE:
            raise ValidationError("Activate this agent before making it the default.")

        await self._agents.clear_defaults()
        agent.is_default = True
        logger.info("agent.default_changed", extra={"agent_id": str(agent.id)})
        return agent

    def available_tools(self) -> list[ToolSpec]:
        """Every tool this build can run, for a configuration screen."""
        return self._registry.specs(self._registry.names())

    async def list_tools(self, agent_id: uuid.UUID) -> list[AgentTool]:
        """Every grant, including disabled ones.

        require_by_id first, so an agent from another workspace answers not
        found rather than an empty list.
        """
        await self._agents.require_by_id(agent_id)
        return await self._grants.list_for_agent(agent_id=agent_id, enabled_only=False)

    async def grant_tool(
        self,
        agent_id: uuid.UUID,
        *,
        name: str,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
    ) -> AgentTool:
        """Grant a tool, refusing a name this build cannot run.

        Validated when granted rather than when read: a typo should fail at the
        moment it is made, while a grant for a tool a later release removed must
        still be readable instead of breaking the screen.
        """
        agent = await self._agents.require_by_id(agent_id)
        if not self._registry.knows(name):
            raise ValidationError(f"There is no tool named {name}.")

        grant = await self._grants.upsert(
            agent_id=agent.id,
            name=name,
            enabled=enabled,
            config=config,
        )
        await self._session.flush()
        logger.info(
            "agent.tool_granted",
            extra={"agent_id": str(agent.id), "tool": name, "enabled": enabled},
        )
        return grant

    async def revoke_tool(self, agent_id: uuid.UUID, *, name: str) -> AgentTool:
        """Withdraw a tool by disabling the grant.

        Disabled rather than deleted, so re-enabling it later does not silently
        discard the configuration stored with it.
        """
        agent = await self._agents.require_by_id(agent_id)
        grant = await self._grants.get(agent_id=agent.id, name=name)
        if grant is None:
            raise NotFoundError("This agent does not have that tool.")

        logger.info("agent.tool_revoked", extra={"agent_id": str(agent.id), "tool": name})
        return await self._grants.set_enabled(grant, enabled=False)
