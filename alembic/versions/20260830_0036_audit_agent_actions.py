"""Record what an AI agent did on its own initiative.

Revision ID: 0036
Revises: 0035

Two enums grow. `audit_actor_kind` gains `agent`, so an act by a model is
distinguishable from an act by the scheduler; `audit_action` gains the three
mutations an agent can perform through its tools.

Same shape as 0029 and 0034, for the same reason: `ALTER TYPE ... ADD VALUE`
cannot run inside a transaction block, so each statement goes in an autocommit
block, and `IF NOT EXISTS` makes a partially applied run repeatable - which is
the state autocommit can leave behind when four separate statements are not one
unit.
"""

from __future__ import annotations

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_NEW_ACTOR_KINDS = ("agent",)

_NEW_ACTIONS = (
    "agent_handoff_requested",
    "agent_lead_recorded",
    "agent_follow_up_scheduled",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for kind in _NEW_ACTOR_KINDS:
            op.execute(f"ALTER TYPE audit_actor_kind ADD VALUE IF NOT EXISTS '{kind}'")
        for action in _NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{action}'")


def downgrade() -> None:
    """Deliberately empty.

    PostgreSQL cannot remove a value from an enum. Doing it properly means
    creating a replacement type, rewriting every `audit_logs` row onto it,
    swapping the column and dropping the old type - a full rewrite of the one
    table in this schema that only ever grows, in order to remove four labels
    that are inert the moment nothing emits them.

    Migrations 0025, 0029 and 0034 took the same position.
    """
