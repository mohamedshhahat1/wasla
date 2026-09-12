"""record how every agent turn ended, and the provider's id for it

Revision ID: 0061
Revises: 0060

A turn that ended without a reply used to end in `COMPLETED` and nothing else: a
refused allowance, a phantom handoff, an empty model answer and a reply that was
correctly suppressed all looked identical, and none of them told anyone which it
was. `agent_turns.outcome` names the ending - replied, handed over, escalated,
suppressed because the workspace, the conversation, the agent or the number could
no longer be served, blocked by quota, or empty. Null on turns completed before
this revision; the worker writes one on every turn it completes from now on.

`provider_response_id` keeps the provider's id for the turn's last response, so a
support question about one turn can be correlated with the provider's records.
It is an id and nothing else - no prompt, no body (AI-12).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

AGENT_TURN_OUTCOME = postgresql.ENUM(
    "replied",
    "handed_off",
    "escalated",
    "empty_response",
    "nothing_to_answer",
    "quota_blocked",
    "suppressed_human",
    "suppressed_agent",
    "suppressed_workspace",
    "suppressed_closed",
    "suppressed_channel",
    name="agent_turn_outcome",
    create_type=False,
)


def upgrade() -> None:
    AGENT_TURN_OUTCOME.create(op.get_bind(), checkfirst=False)
    op.add_column("agent_turns", sa.Column("outcome", AGENT_TURN_OUTCOME, nullable=True))
    op.add_column(
        "agent_turns",
        sa.Column("provider_response_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_turns", "provider_response_id")
    op.drop_column("agent_turns", "outcome")
    AGENT_TURN_OUTCOME.drop(op.get_bind(), checkfirst=False)
