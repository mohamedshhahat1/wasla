"""give one inbound message one agent turn, however often it is queued

Revision ID: 0057
Revises: 0056

The queue delivers at least once, on purpose: a producer that published and then
failed to commit leaves a job behind, and `InboundRecoveryWorker` re-derives the
same work from the durable event so the customer is not left unanswered. What
neither of them could say is whether two envelopes naming one conversation are
two customers speaking or one customer's message arriving twice - an envelope
carries no business identity - and the answer was two of everything: two
sentiment classifications, two inferences, two tool loops, two committed
outbound rows, two messages on the customer's phone, and a workspace billed for
all of it. A single failed commit in the sweeper does that for the whole batch
it was holding (WQ-01).

**The turn is identified by the message it answers.** Not by the conversation,
because two messages in one conversation are two turns and both are owed an
answer; and not by anything minted at consume time, because the whole point is
that two independent publications of one turn agree. Every producer derives it
from the same durable row, so the webhook's job and the sweeper's job for one
message are one turn.

**Three states, because "finished" is the wrong question.** What decides whether
a second attempt may run is not whether the first one ended but whether anything
left the process, which is the same distinction `ReservationStage` draws on the
queue (ADR-074). `claimed` means nothing has; it carries a lease, so a worker
that died before reaching a provider hands its turn on instead of stranding the
customer's message for ever. `engaged` means something may have; it carries no
lease and is refused permanently. `completed` means the turn ran to its end.

`trigger_message_id` is deliberately **not** a foreign key to `messages`. A turn
is evidence that work happened and has to outlive the message being erased by
retention or by a workspace purge; the composite key to `conversations` is what
keeps it inside its own workspace (ADR-100).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

# `create_type=False` so the column below does not try to create the type a
# second time; the explicit `create` in `upgrade` owns it, as in 0016.
AGENT_TURN_STATE = postgresql.ENUM(
    "claimed",
    "engaged",
    "completed",
    name="agent_turn_state",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    AGENT_TURN_STATE.create(bind, checkfirst=False)

    op.create_table(
        "agent_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", AGENT_TURN_STATE, nullable=False),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            name="fk_agent_turns_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_agent_turns_tenant_conversation",
            ondelete="CASCADE",
        ),
    )

    # The guarantee. Everything else in this table is bookkeeping around it.
    op.create_unique_constraint(
        "uq_agent_turns_tenant_id_trigger_message_id",
        "agent_turns",
        ["tenant_id", "trigger_message_id"],
    )
    op.create_index("ix_agent_turns_tenant_id", "agent_turns", ["tenant_id"])
    # Partial: on a healthy deployment this is a handful of rows out of every
    # turn the platform has ever run, and a full index would be maintained
    # continuously to answer a question nobody usually has.
    op.create_index(
        "ix_agent_turns_unfinished",
        "agent_turns",
        ["claim_expires_at"],
        postgresql_where=sa.text("state = 'claimed'"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_turns_unfinished", table_name="agent_turns")
    op.drop_index("ix_agent_turns_tenant_id", table_name="agent_turns")
    op.drop_constraint(
        "uq_agent_turns_tenant_id_trigger_message_id",
        "agent_turns",
        type_="unique",
    )
    op.drop_table("agent_turns")
    AGENT_TURN_STATE.drop(op.get_bind(), checkfirst=False)
