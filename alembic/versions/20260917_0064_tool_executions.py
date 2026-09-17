"""record every tool call an agent was asked to make, and what became of it

Revision ID: 0064
Revises: 0063

**Why** (TOOL-12). A tool left a trace only when it succeeded *and* mutated
something: `audit_logs` gained a row, keyed by conversation rather than by turn.
A refusal, a rejected argument, a lifecycle denial, a duplicate suppression and
a call whose turn then died were indistinguishable afterwards - and so was a
committed lead whose turn stranded, because nothing tied the effect to the
execution that produced it. "Did this run, and did it have an effect" was not a
question the database could answer.

`tool_executions` is one row per call the provider asked for: the ones that ran,
the ones refused before they ran, and the ones suppressed as duplicates. It
carries the turn, the trigger message, the agent and the conversation, so the
join the audit trail could not make is a foreign-key-shaped column; the
provider's call id, for correlation; the round and position within the response,
so a turn's calls order without relying on clock resolution; a closed state and a
closed reason; and the four timestamps a lifecycle needs.

**No arguments, ever.** `argument_fields` carries the argument *names* a call
supplied, as a JSON array, and nothing else - the same rule `audit_logs.meta`
follows (ADR-052). Tool arguments are where a customer's name, a handoff
sentence and a follow-up body live, and a second copy of those with a different
retention story is the leak TOOL-10 found in the logs wearing a different hat.

**No history is invented.** Turns that ran before this revision keep no
execution rows: there is no evidence from which to reconstruct which calls they
made, and a fabricated row in a table an investigation reads is worse than an
honest gap. `agent_turn_id` and `provider_call_id` are nullable for the same
reason - a record with a partly-known identity is still worth having.

The unique constraint is `(tenant_id, agent_turn_id, provider_call_id)`.
PostgreSQL treats nulls as distinct in a unique index, which is what this wants:
it binds a provider call id to one execution *within a turn* without refusing to
record a call the provider left unidentified.

Two `audit_action` labels come with it (TOOL-13): granting and revoking a tool
is the decision that sets what a model may do, and it was the one privileged
configuration change in the product that wrote no audit row. Added in an
autocommit block, because `ADD VALUE` cannot be used in the transaction that
added it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

# `create_type=False` so the columns below do not try to create the types a
# second time; the explicit `create` in `upgrade` owns them, as in 0057.
TOOL_EXECUTION_STATE = postgresql.ENUM(
    "requested",
    "authorized",
    "started",
    "succeeded",
    "rejected",
    "failed",
    "duplicate",
    "ambiguous",
    name="tool_execution_state",
    create_type=False,
)

TOOL_EXECUTION_REASON = postgresql.ENUM(
    "not_granted",
    "tool_disabled",
    "tool_not_implemented",
    "workspace_suspended",
    "workspace_deleted",
    "agent_disabled",
    "conversation_human",
    "conversation_closed",
    "channel_unavailable",
    "invalid_arguments",
    "unsafe_text",
    "range_violation",
    "response_call_limit",
    "turn_call_limit",
    "round_limit",
    "duplicate_call",
    "handoff_completed",
    "domain_error",
    "internal_error",
    name="tool_execution_reason",
    create_type=False,
)


NEW_AUDIT_ACTIONS = (
    "agent_tool_granted",
    "agent_tool_revoked",
)


def upgrade() -> None:
    bind = op.get_bind()
    TOOL_EXECUTION_STATE.create(bind, checkfirst=False)
    TOOL_EXECUTION_REASON.create(bind, checkfirst=False)

    op.create_table(
        "tool_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trigger_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("provider_call_id", sa.String(length=128), nullable=True),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("call_ordinal", sa.Integer(), nullable=False),
        sa.Column("state", TOOL_EXECUTION_STATE, nullable=False),
        sa.Column("reason_code", TOOL_EXECUTION_REASON, nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("argument_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tool_executions_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        # An execution belongs to a conversation of its own workspace (ADR-100).
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_tool_executions_tenant_conversation",
            ondelete="CASCADE",
        ),
    )

    # Partial, excluding `duplicate`: a suppressed repeat shares the identity by
    # definition, and a constraint that refused to record it would make the
    # table unable to hold the very thing it exists to show.
    op.create_index(
        "uq_tool_executions_turn_provider_call_id",
        "tool_executions",
        ["tenant_id", "agent_turn_id", "provider_call_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'duplicate'"),
    )
    op.create_index("ix_tool_executions_tenant_id", "tool_executions", ["tenant_id"])
    op.create_index("ix_tool_executions_agent_turn_id", "tool_executions", ["agent_turn_id"])
    op.create_index("ix_tool_executions_conversation_id", "tool_executions", ["conversation_id"])
    # Partial: successful calls are the overwhelming majority, and a failure
    # sweep asks only about the rest.
    op.create_index(
        "ix_tool_executions_unsuccessful",
        "tool_executions",
        ["tool_name", "created_at"],
        postgresql_where=sa.text("state <> 'succeeded'"),
    )

    # `ADD VALUE` cannot be used later in the transaction that added it, and
    # Alembic runs a migration inside one. `autocommit_block` is the supported
    # way round that, and `IF NOT EXISTS` makes the step re-runnable after a
    # partial failure - which matters precisely because this part is not
    # covered by the surrounding transaction.
    with op.get_context().autocommit_block():
        for label in NEW_AUDIT_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    """Drop the table and its types; the two enum labels stay.

    PostgreSQL cannot drop an enum label, and doing it properly means creating a
    replacement type and rewriting every `audit_logs` row onto it - a full
    rewrite of the one table in this schema that only ever grows, to remove two
    labels that are inert the moment nothing emits them. Migrations 0025, 0029,
    0034, 0036, 0045, 0046, 0047, 0049, 0050 and 0051 all took this position,
    and the safe order for a rollback is the same one: deploy the older code
    first, then downgrade.
    """
    op.drop_index("ix_tool_executions_unsuccessful", table_name="tool_executions")
    op.drop_index("ix_tool_executions_conversation_id", table_name="tool_executions")
    op.drop_index("ix_tool_executions_agent_turn_id", table_name="tool_executions")
    op.drop_index("ix_tool_executions_tenant_id", table_name="tool_executions")
    op.drop_index("uq_tool_executions_turn_provider_call_id", table_name="tool_executions")
    op.drop_table("tool_executions")
    TOOL_EXECUTION_REASON.drop(op.get_bind(), checkfirst=False)
    TOOL_EXECUTION_STATE.drop(op.get_bind(), checkfirst=False)
