"""AI agents and the tools they are permitted to call.

An agent is configuration rather than code. A workspace describes how it wants
its customers answered - model, instructions, memory budget, permitted tools -
and the orchestrator reads that description at run time. Behaviour therefore
differs per workspace without a deployment, which is the whole point of a
multi-tenant agent platform.

Tool grants are rows rather than a list on the agent. Each grant carries its own
enabled flag and its own settings, and granting an agent a capability is a
permission question that deserves a queryable record rather than a JSON blob.

There is no delete. An agent that has answered real customers is part of the
audit trail, so retirement is a status change.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type


class AgentStatus(StrEnum):
    """Whether an agent may answer customers.

    `DRAFT` exists so an agent can be configured and reviewed before it is ever
    pointed at a real conversation.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


AGENT_STATUS_TYPE = _enum_type(AgentStatus, name="agent_status")

# Defaults live here so the column default and the repository signature cannot
# drift apart.
DEFAULT_TEMPERATURE: Final = 0.3
DEFAULT_MEMORY_MESSAGE_LIMIT: Final = 20
DEFAULT_MEMORY_TOKEN_BUDGET: Final = 4_000


class Agent(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One configured assistant belonging to a workspace.

    Names are unique per workspace, not globally: two businesses may both call
    their assistant "Sales", and neither should have to care that the other
    exists.
    """

    __tablename__ = "agents"
    # Restated, not inherited: see TenantScopedMixin.
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_id_name"),
        Index("ix_agents_tenant_id", "tenant_id"),
        Index("ix_agents_tenant_id_status", "tenant_id", "status"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[AgentStatus] = mapped_column(
        AGENT_STATUS_TYPE,
        nullable=False,
        default=AgentStatus.DRAFT,
    )
    # The provider model id, stored per agent so one workspace can run a cheaper
    # model than another without a code change (ADR-007).
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    temperature: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=DEFAULT_TEMPERATURE,
    )
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Two limits, because either alone is insufficient: a message count keeps
    # the prompt cheap, and a token budget keeps a single long message from
    # blowing the context anyway.
    memory_message_limit: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MEMORY_MESSAGE_LIMIT,
    )
    memory_token_budget: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MEMORY_TOKEN_BUDGET,
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @property
    def is_answering(self) -> bool:
        """Whether this agent is allowed to reply to a customer right now."""
        return self.status is AgentStatus.ACTIVE


class AgentTool(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """A capability granted to one agent.

    `name` is a key into the application's tool registry rather than a free
    label. A row naming a tool the registry does not implement is inert, which
    is deliberate: removing a tool from the code must not break existing agents.
    """

    __tablename__ = "agent_tools"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "agent_id",
            "name",
            name="uq_agent_tools_tenant_id_agent_id_name",
        ),
        Index("ix_agent_tools_tenant_id", "tenant_id"),
        Index("ix_agent_tools_agent_id", "agent_id"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Per-grant settings, such as which lead statuses a tool may write. Shape is
    # owned by the tool, so it is stored rather than modelled.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
