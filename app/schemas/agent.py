"""Agent configuration contracts.

Bounds live here rather than in the service so an unreasonable prompt or budget
is refused before any code runs, and so the limits appear in the generated API
documentation instead of only in a rejection message.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.agent import (
    DEFAULT_ESCALATION_SENTIMENT,
    DEFAULT_MEMORY_MESSAGE_LIMIT,
    DEFAULT_MEMORY_TOKEN_BUDGET,
    DEFAULT_TEMPERATURE,
    AgentStatus,
)
from app.db.models.sentiment import SentimentLabel

MAX_NAME_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 500
MAX_MODEL_LENGTH = 100
MAX_TOOL_NAME_LENGTH = 100
# Long enough for a detailed persona with examples, short enough that a runaway
# paste cannot become the system prompt.
MAX_PROMPT_LENGTH = 20_000
MAX_MESSAGE_LIMIT = 200
MIN_TOKEN_BUDGET = 200
MAX_TOKEN_BUDGET = 100_000
MAX_OUTPUT_TOKENS = 8_192
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0

AgentName = Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH)]
Description = Annotated[str, Field(min_length=1, max_length=MAX_DESCRIPTION_LENGTH)]
Prompt = Annotated[str, Field(min_length=1, max_length=MAX_PROMPT_LENGTH)]
ModelName = Annotated[str, Field(min_length=1, max_length=MAX_MODEL_LENGTH)]
ToolName = Annotated[str, Field(min_length=1, max_length=MAX_TOOL_NAME_LENGTH)]
Temperature = Annotated[float, Field(ge=MIN_TEMPERATURE, le=MAX_TEMPERATURE)]
MessageLimit = Annotated[int, Field(ge=1, le=MAX_MESSAGE_LIMIT)]
TokenBudget = Annotated[int, Field(ge=MIN_TOKEN_BUDGET, le=MAX_TOKEN_BUDGET)]
OutputTokens = Annotated[int, Field(ge=1, le=MAX_OUTPUT_TOKENS)]


class AgentCreate(BaseModel):
    """A new agent. It starts as a draft whatever else is sent."""

    name: AgentName
    system_prompt: Prompt
    description: Description | None = None
    model: ModelName | None = None
    temperature: Temperature = DEFAULT_TEMPERATURE
    max_output_tokens: OutputTokens | None = None
    memory_message_limit: MessageLimit = DEFAULT_MEMORY_MESSAGE_LIMIT
    memory_token_budget: TokenBudget = DEFAULT_MEMORY_TOKEN_BUDGET
    # How unhappy a customer must sound before this agent stops replying and
    # hands over. Null switches automatic handoff off; the reading is still
    # taken and the conversation is still flagged.
    escalation_sentiment: SentimentLabel | None = DEFAULT_ESCALATION_SENTIMENT


class AgentUpdate(BaseModel):
    """Changes to an agent. An absent field is left alone.

    Clearing a description is deliberately not expressible: telling "not sent"
    from "sent as null" needs a sentinel, and no screen needs it yet.

    `escalation_sentiment` is the exception, because null is a setting there
    rather than an absence - it is how a workspace switches automatic handoff
    off. Pydantic records which fields arrived, so `was_sent` reads that rather
    than inventing a sentinel value in the wire format.
    """

    name: AgentName | None = None
    system_prompt: Prompt | None = None
    description: Description | None = None
    model: ModelName | None = None
    status: AgentStatus | None = None
    temperature: Temperature | None = None
    max_output_tokens: OutputTokens | None = None
    memory_message_limit: MessageLimit | None = None
    memory_token_budget: TokenBudget | None = None
    escalation_sentiment: SentimentLabel | None = None

    def was_sent(self, field: str) -> bool:
        """Whether the caller included this field, null or not."""
        return field in self.model_fields_set


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    status: AgentStatus
    model: str
    system_prompt: str
    temperature: float
    max_output_tokens: int | None
    memory_message_limit: int
    memory_token_budget: int
    is_default: bool
    escalation_sentiment: SentimentLabel | None
    created_at: datetime
    updated_at: datetime


class ToolGrantRequest(BaseModel):
    """Granting a tool to an agent, or changing an existing grant."""

    name: ToolName
    enabled: bool = True
    config: dict[str, Any] | None = None


class ToolGrantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    enabled: bool
    config: dict[str, Any] | None
    created_at: datetime


class ToolSpecRead(BaseModel):
    """A tool this build can actually run."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    parameters: dict[str, Any]
