"""Data access for agents and their tool grants."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ColumnElement

from app.core.exceptions import ConflictError
from app.db.models.agent import (
    DEFAULT_MEMORY_MESSAGE_LIMIT,
    DEFAULT_MEMORY_TOKEN_BUDGET,
    DEFAULT_TEMPERATURE,
    Agent,
    AgentStatus,
    AgentTool,
)
from app.repositories.base import TenantScopedRepository


class AgentRepository(TenantScopedRepository[Agent]):
    """Agents of one workspace."""

    model = Agent

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Agent.tenant_id == self.tenant_id

    async def get_by_id(self, agent_id: uuid.UUID) -> Agent | None:
        return await self._first(self._select().where(Agent.id == agent_id))

    async def require_by_id(self, agent_id: uuid.UUID) -> Agent:
        return await self._require(self._select().where(Agent.id == agent_id))

    async def get_by_name(self, name: str) -> Agent | None:
        return await self._first(self._select().where(Agent.name == name))

    async def list_all(self, *, limit: int = 50) -> list[Agent]:
        return await self._all(self._select().order_by(Agent.name).limit(limit))

    async def get_answering_default(self) -> Agent | None:
        """The agent that replies when nothing more specific applies.

        Both conditions matter: a default that has been disabled must not answer
        customers, and the orchestrator needs to see that as "no agent" rather
        than reaching for a disabled configuration.
        """
        return await self._first(
            self._select().where(
                Agent.is_default.is_(True),
                Agent.status == AgentStatus.ACTIVE,
            )
        )

    async def clear_defaults(self) -> None:
        """Unset every default in this workspace.

        A single default is enforced here rather than by a partial unique index.
        Two rows briefly marked default inside one transaction is not a
        corruption the database needs to police, and an index would forbid the
        natural order of promoting one agent before demoting the other.
        """
        for existing in await self._all(self._select().where(Agent.is_default.is_(True))):
            existing.is_default = False

    async def create(
        self,
        *,
        name: str,
        model: str,
        system_prompt: str,
        description: str | None = None,
        status: AgentStatus = AgentStatus.DRAFT,
        temperature: float = DEFAULT_TEMPERATURE,
        max_output_tokens: int | None = None,
        memory_message_limit: int = DEFAULT_MEMORY_MESSAGE_LIMIT,
        memory_token_budget: int = DEFAULT_MEMORY_TOKEN_BUDGET,
        is_default: bool = False,
    ) -> Agent:
        """Create an agent, refusing a duplicate name in this workspace.

        The unique constraint is the real guarantee; this check exists to return
        a useful message rather than a driver error.
        """
        if await self.get_by_name(name) is not None:
            raise ConflictError("An agent with that name already exists.")

        return self.add(
            Agent(
                tenant_id=self.tenant_id,
                name=name,
                description=description,
                status=status,
                model=model,
                system_prompt=system_prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                memory_message_limit=memory_message_limit,
                memory_token_budget=memory_token_budget,
                is_default=is_default,
            )
        )


class AgentToolRepository(TenantScopedRepository[AgentTool]):
    """Tool grants of one workspace."""

    model = AgentTool

    def _tenant_filter(self) -> ColumnElement[bool]:
        return AgentTool.tenant_id == self.tenant_id

    async def get(self, *, agent_id: uuid.UUID, name: str) -> AgentTool | None:
        return await self._first(
            self._select().where(
                AgentTool.agent_id == agent_id,
                AgentTool.name == name,
            )
        )

    async def list_for_agent(
        self,
        *,
        agent_id: uuid.UUID,
        enabled_only: bool = True,
    ) -> list[AgentTool]:
        """Grants for one agent.

        Defaults to enabled grants only, because the orchestrator asking for an
        agent's tools is asking what it may actually call. Configuration screens
        pass `enabled_only=False` to show the rest.
        """
        query = self._select().where(AgentTool.agent_id == agent_id)
        if enabled_only:
            query = query.where(AgentTool.enabled.is_(True))
        return await self._all(query.order_by(AgentTool.name))

    async def upsert(
        self,
        *,
        agent_id: uuid.UUID,
        name: str,
        enabled: bool = True,
        config: dict[str, Any] | None = None,
    ) -> AgentTool:
        """Grant a tool, or update an existing grant.

        An absent `config` leaves stored settings alone rather than erasing
        them, so toggling a tool off and on again does not silently reset it.
        """
        grant = await self.get(agent_id=agent_id, name=name)
        if grant is None:
            return self.add(
                AgentTool(
                    tenant_id=self.tenant_id,
                    agent_id=agent_id,
                    name=name,
                    enabled=enabled,
                    config=config,
                )
            )

        grant.enabled = enabled
        if config is not None:
            grant.config = config
        return grant

    async def set_enabled(self, grant: AgentTool, *, enabled: bool) -> AgentTool:
        grant.enabled = enabled
        return grant
