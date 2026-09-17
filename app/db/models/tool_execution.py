"""One durable record per tool call a model asked for (TOOL-12).

Before this table the only trace a tool left was an `audit_logs` row, and only
for the calls that *succeeded and mutated something*. A refusal, a rejected
argument, a lifecycle denial, a duplicate suppression and a call whose turn then
died were all indistinguishable afterwards - they were indistinguishable from
never having been requested at all. The operational question is not "what did
the agent change", which the audit trail answers well; it is "did this run, and
did it have an effect", and that could not be answered from the database.

**A row per requested call, not per effect.** Every call the provider asks for
gets one: the ones that ran, the ones that were refused before they ran, and the
ones that were suppressed as duplicates. That is what makes a zero meaningful -
"no execution row" now means "the provider never asked", rather than "something
happened and nothing recorded it".

**Identity is server-generated.** `id` is this platform's own key for one
execution, and it is the seed a future external tool would derive a provider
idempotency key from. The provider's `call_id` is recorded but is not the
identity: it is a value the provider chooses, this system has no guarantee it
is unique anywhere, and two calls can carry the same one inside a single
response. What the unique index says is narrower and true: *within one turn*, a
provider call id is executed once. Everything past the first is a `DUPLICATE`.

**Never the arguments.** `argument_fields` carries the *names* a call supplied
and nothing else, exactly as `audit_logs.meta` carries field names rather than
values (ADR-052). A customer's name, a handoff sentence and a follow-up body are
what the model puts in tool arguments; a table recording them would be a second,
worse copy of the conversation with a different retention story - which is the
same mistake TOOL-10 found in the logs.

**States are closed, and so are reasons.** An operator reading this table is
counting, not parsing: `state` says what became of the call and `reason_code`
says why, both from vocabularies this module owns. A driver's exception text
never lands in either.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type


class ToolExecutionState(StrEnum):
    """What became of one requested tool call.

    `AMBIGUOUS` is unreachable from the four tools shipped today - none of them
    has an external side effect whose outcome could be unknown - and it is here
    anyway. A state added at the moment it is first needed is a migration in the
    middle of an incident; the vocabulary that can express "we asked somebody
    else to do something and do not know whether they did" has to exist before
    the first tool that can reach it does.
    """

    #: The provider asked for this call and the executor has taken it up.
    REQUESTED = "requested"
    #: Grant, lifecycle and bounds all passed; the handler has not run yet.
    AUTHORIZED = "authorized"
    #: The handler was entered.
    STARTED = "started"
    #: The handler returned, and its effect is committed with this row.
    SUCCEEDED = "succeeded"
    #: Refused before the handler ran. `reason_code` says by what.
    REJECTED = "rejected"
    #: The handler raised. Its work was rolled back to this call's savepoint.
    FAILED = "failed"
    #: A repeat of a provider call id already executed in this turn. No effect.
    DUPLICATE = "duplicate"
    #: An external effect may or may not have happened. Needs reconciliation.
    AMBIGUOUS = "ambiguous"


TOOL_EXECUTION_STATE_TYPE = _enum_type(ToolExecutionState, name="tool_execution_state")

#: States from which nothing further will happen to this row.
TERMINAL_TOOL_EXECUTION_STATES = frozenset(
    {
        ToolExecutionState.SUCCEEDED,
        ToolExecutionState.REJECTED,
        ToolExecutionState.FAILED,
        ToolExecutionState.DUPLICATE,
        ToolExecutionState.AMBIGUOUS,
    }
)


class ToolExecutionReason(StrEnum):
    """Why a call was refused or how it failed, from a closed vocabulary.

    Closed because this column is read by counting. An exception message or a
    provider string here would be unbounded, would carry customer content, and
    would make "how often are tools denied because the workspace stopped being
    served" a text search instead of a `GROUP BY`.
    """

    #: The agent holds no grant for this tool at all.
    NOT_GRANTED = "not_granted"
    #: A grant exists and is switched off. Read at the instant of the call, so
    #: an administrator revoking a capability stops the calls of the turn that
    #: is already running (TOOL-05).
    TOOL_DISABLED = "tool_disabled"
    #: The grant names a tool this build does not implement. Kept apart from
    #: `NOT_GRANTED` because it is a deployment fact rather than an
    #: authorization one: grants outlive code, and removing a tool must not
    #: break every agent that was granted it.
    TOOL_NOT_IMPLEMENTED = "tool_not_implemented"
    WORKSPACE_SUSPENDED = "workspace_suspended"
    WORKSPACE_DELETED = "workspace_deleted"
    AGENT_DISABLED = "agent_disabled"
    #: A colleague owns the conversation. Human ownership outranks a decision
    #: the model reached before the takeover (PD-TOOLS-01).
    CONVERSATION_HUMAN = "conversation_human"
    CONVERSATION_CLOSED = "conversation_closed"
    #: The number this conversation is answered on cannot send.
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    #: Arguments the declaration refuses: unknown, missing, wrong type.
    INVALID_ARGUMENTS = "invalid_arguments"
    #: Text PostgreSQL could not store - a NUL, a lone surrogate (TOOL-01).
    UNSAFE_TEXT = "unsafe_text"
    #: A number or a length outside the bound the schema publishes.
    RANGE_VIOLATION = "range_violation"
    #: Past `MAX_TOOL_CALLS_PER_RESPONSE` for this model response.
    RESPONSE_CALL_LIMIT = "response_call_limit"
    #: Past `MAX_TOOL_CALLS_PER_TURN` for this turn.
    TURN_CALL_LIMIT = "turn_call_limit"
    #: A side-effecting call on the final round, whose result no round could
    #: read (PD-TOOLS-05).
    ROUND_LIMIT = "round_limit"
    #: A provider call id already executed in this turn.
    DUPLICATE_CALL = "duplicate_call"
    #: The handoff of this response succeeded; later calls do not run
    #: (PD-TOOLS-02).
    HANDOFF_COMPLETED = "handoff_completed"
    #: A `WaslaError` - a rule the domain enforces, told to the model.
    DOMAIN_ERROR = "domain_error"
    #: Anything else the handler raised. Contained, never forwarded (TOOL-01).
    INTERNAL_ERROR = "internal_error"


TOOL_EXECUTION_REASON_TYPE = _enum_type(ToolExecutionReason, name="tool_execution_reason")


class ToolExecution(Base, UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin):
    """One tool call a model requested, and what the server did about it."""

    __tablename__ = "tool_executions"
    __table_args__ = (
        # One logical execution per provider call, per turn. Duplicate
        # suppression is done in the executor; this is the constraint that makes
        # a second *execution* impossible if it ever is not.
        #
        # Partial, excluding `duplicate`, and that exclusion is the whole
        # subtlety: a suppressed repeat shares the identity by definition, and a
        # constraint that refused to record it would turn "we stopped a
        # duplicate" into an unflushable row - the table built to explain what
        # happened refusing to hold the very thing it exists to show.
        #
        # PostgreSQL treats nulls as distinct here too, which is also wanted: a
        # call the provider left unidentified, or a caller with no turn at all,
        # has nothing to be unique on and must still be recorded.
        Index(
            "uq_tool_executions_turn_provider_call_id",
            "tenant_id",
            "agent_turn_id",
            "provider_call_id",
            unique=True,
            postgresql_where=text("state <> 'duplicate'"),
        ),
        # Restated, not inherited: see TenantScopedMixin.
        Index("ix_tool_executions_tenant_id", "tenant_id"),
        # "What did this turn do" - the join the audit could not make.
        Index("ix_tool_executions_agent_turn_id", "agent_turn_id"),
        Index("ix_tool_executions_conversation_id", "conversation_id"),
        # "Which tool is failing, and since when." Partial, because the
        # successful calls are the overwhelming majority and a failure sweep
        # asks only about the rest.
        Index(
            "ix_tool_executions_unsuccessful",
            "tool_name",
            "created_at",
            postgresql_where=text("state <> 'succeeded'"),
        ),
        # An execution belongs to a conversation in its own workspace
        # (ADR-100), exactly as an agent turn does.
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_tool_executions_tenant_conversation",
            ondelete="CASCADE",
        ),
    )

    #: The turn that requested it. Not a foreign key, for the same reason
    #: `agent_turns.trigger_message_id` is not one: this row is evidence that
    #: work happened and has to survive retention erasing what it points at.
    #: Null only for a caller with no turn identity at all.
    agent_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: The inbound message the turn was answering, carried so an investigation
    #: can start from a customer's message rather than from a turn id.
    trigger_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: Which agent's configuration decided this call was available.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    #: The name as the registry spells it, or as the model spelled it when the
    #: registry has no such tool. Bounded by the grant column's own width.
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: The provider's id for this call. Recorded for correlation; never trusted
    #: as an identity beyond the turn it arrived in.
    provider_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Which inference round asked, and where in that response the call sat.
    #: Together they order the executions of a turn without relying on clock
    #: resolution.
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    call_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    state: Mapped[ToolExecutionState] = mapped_column(
        TOOL_EXECUTION_STATE_TYPE,
        nullable=False,
        default=ToolExecutionState.REQUESTED,
    )
    reason_code: Mapped[ToolExecutionReason | None] = mapped_column(
        TOOL_EXECUTION_REASON_TYPE,
        nullable=True,
    )

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: The argument *names* the call carried, sorted. Shapes only - never a
    #: value, for the same reason `audit_logs.meta` never carries one.
    argument_fields: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_TOOL_EXECUTION_STATES

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ToolExecution {self.tool_name} {self.state.value}"
            f"{' ' + self.reason_code.value if self.reason_code else ''}>"
        )


def meta_for(arguments: dict[str, Any] | None) -> list[str] | None:
    """The safe shape of a call's arguments: their names, sorted.

    A helper rather than an inline comprehension at the one call site, because
    "record the arguments" is exactly the change somebody makes in a hurry and
    this is where the rule that they must not be is written down.
    """
    if not arguments:
        return None
    return sorted(str(key) for key in arguments)


__all__ = [
    "TERMINAL_TOOL_EXECUTION_STATES",
    "TOOL_EXECUTION_REASON_TYPE",
    "TOOL_EXECUTION_STATE_TYPE",
    "ToolExecution",
    "ToolExecutionReason",
    "ToolExecutionState",
    "meta_for",
]
