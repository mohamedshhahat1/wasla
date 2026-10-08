"""AI turns are held at engagement and charged on successful generation.

Revision ID: 0094
Revises: 0093

ADR-131 (ENT-01, ENT-02, ENT-03, ENT-16).

1. **`agent_turn_outcome` gains `channel_not_in_plan`** - appended in an
   autocommit block first, as every label is: a turn on a channel the plan in
   force does not include ends with it (ENT-16).
2. **`agent_turns` carries the turn's hold and how it settled**: `charge_state`
   (`held`, `charged`, `released`), `held_at`, `charged_at`, `released_at` and
   `charge_release_reason`. Null on every turn written before this revision,
   which was charged at engagement (ADR-104) and holds nothing.
   `ck_agent_turns_charge_state_consistent` makes the columns move together.
3. **`usage_events` gains reporting dimensions**: `channel` and `connection_id`
   (ENT-01, ENT-22), and `agent_turn_id` - the turn an `ai_turn` charge settled,
   unique per workspace and turn (`uq_usage_events_tenant_id_agent_turn_id`) and
   allowed only on `ai_turn` rows. None is a foreign key: usage is a ledger
   that outlives the connection and the turn it describes. Null on history.

**Online.** Every column is nullable with no default, so `ADD COLUMN` is
metadata only; the CHECKs are added `NOT VALID` and validated in an autocommit
block, where validation holds no lock that blocks writes; the two partial
indexes are built `CONCURRENTLY`, an invalid leftover of either name dropped
first (0091's pattern). `lock_timeout` is bounded.

**Downgrade refuses** while a turn holds the allowance - the pre-0094 worker
charges at engagement, so an open hold would never be charged or released - or
while a turn ended `channel_not_in_plan`, an outcome the earlier schema cannot
explain. Otherwise it drops what this revision added: a charge stays on the
ledger as its `ai_turn` event, whose conversation its metadata names; the
dimension columns go with the knowledge they carried. The enum label stays.
The downgrade is one transaction - its indexes are dropped in it - so a
refusal below 0094 rolls back the whole run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"

CHARGE_STATES = ("held", "charged", "released")
RELEASE_REASONS = ("not_chargeable", "generation_failed", "hold_expired")

CHARGE_STATE_CONSTRAINT = "ck_agent_turns_charge_state_consistent"
CHARGE_STATE_CHECK = (
    "(charge_state IS NULL AND held_at IS NULL AND charged_at IS NULL"
    " AND released_at IS NULL AND charge_release_reason IS NULL)"
    " OR (charge_state = 'held' AND held_at IS NOT NULL AND charged_at IS NULL"
    " AND released_at IS NULL AND charge_release_reason IS NULL)"
    " OR (charge_state = 'released' AND held_at IS NOT NULL AND charged_at IS NULL"
    " AND released_at IS NOT NULL AND charge_release_reason IS NOT NULL)"
    " OR (charge_state = 'charged' AND held_at IS NOT NULL AND charged_at IS NOT NULL"
    " AND ((released_at IS NULL AND charge_release_reason IS NULL)"
    " OR (released_at IS NOT NULL AND charge_release_reason = 'hold_expired')))"
)
AI_TURN_ONLY_CONSTRAINT = "ck_usage_events_agent_turn_only_for_ai_turn"
AI_TURN_ONLY_CHECK = "agent_turn_id IS NULL OR event_type = 'ai_turn'"

# (name, table, definition) - built concurrently, after the transaction.
INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "ix_agent_turns_held",
        "agent_turns",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_agent_turns_held"
        " ON agent_turns (tenant_id, held_at) WHERE charge_state = 'held'",
    ),
    (
        "uq_usage_events_tenant_id_agent_turn_id",
        "usage_events",
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_usage_events_tenant_id_agent_turn_id"
        " ON usage_events (tenant_id, agent_turn_id) WHERE agent_turn_id IS NOT NULL",
    ),
)

TURN_COLUMNS = ("charge_state", "held_at", "charged_at", "released_at", "charge_release_reason")
USAGE_COLUMNS = ("channel", "connection_id", "agent_turn_id")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE agent_turn_outcome ADD VALUE IF NOT EXISTS 'channel_not_in_plan'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    bind = op.get_bind()
    charge_state = postgresql.ENUM(*CHARGE_STATES, name="ai_turn_charge_state")
    release_reason = postgresql.ENUM(*RELEASE_REASONS, name="ai_turn_release_reason")
    charge_state.create(bind, checkfirst=True)
    release_reason.create(bind, checkfirst=True)
    channel = postgresql.ENUM(name="channel_kind", create_type=False)

    # 2. The turn's hold and its settlement.
    op.add_column(
        "agent_turns",
        sa.Column(
            "charge_state",
            postgresql.ENUM(name="ai_turn_charge_state", create_type=False),
            nullable=True,
        ),
    )
    for column in ("held_at", "charged_at", "released_at"):
        op.add_column("agent_turns", sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "agent_turns",
        sa.Column(
            "charge_release_reason",
            postgresql.ENUM(name="ai_turn_release_reason", create_type=False),
            nullable=True,
        ),
    )
    op.execute(
        f"ALTER TABLE agent_turns ADD CONSTRAINT {CHARGE_STATE_CONSTRAINT}"
        f" CHECK ({CHARGE_STATE_CHECK}) NOT VALID"
    )

    # 3. Usage dimensions and the charge's turn.
    op.add_column("usage_events", sa.Column("channel", channel, nullable=True))
    op.add_column(
        "usage_events", sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "usage_events", sa.Column("agent_turn_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.execute(
        f"ALTER TABLE usage_events ADD CONSTRAINT {AI_TURN_ONLY_CONSTRAINT}"
        f" CHECK ({AI_TURN_ONLY_CHECK}) NOT VALID"
    )

    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE agent_turns VALIDATE CONSTRAINT {CHARGE_STATE_CONSTRAINT}")
        op.execute(f"ALTER TABLE usage_events VALIDATE CONSTRAINT {AI_TURN_ONLY_CONSTRAINT}")
        for name, _table, definition in INDEXES:
            invalid = (
                op.get_bind()
                .exec_driver_sql(
                    "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608 - module constant
                    f" WHERE c.relname = '{name}' AND NOT i.indisvalid"
                )
                .scalar_one()
            )
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            op.execute(definition)


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    held = bind.exec_driver_sql(
        "SELECT count(*) FROM agent_turns WHERE charge_state = 'held'"
    ).scalar_one()
    if held:
        found.append(f"agent_turns holding the AI allowance: {held}")
    not_in_plan = bind.exec_driver_sql(
        "SELECT count(*) FROM agent_turns WHERE outcome::text = 'channel_not_in_plan'"
    ).scalar_one()
    if not_in_plan:
        found.append(f"agent_turns ended channel_not_in_plan: {not_in_plan}")
    if found:
        raise RuntimeError(
            "The AI turn charge cannot be downgraded "
            "(docs/RUNBOOK.md, 'Entitlements and channel capacity (0092-0098)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    # In the transaction, not `CONCURRENTLY`: an autocommit block would commit
    # every downgrade above this one, so a refusal further down would leave the
    # stamp at 0094 without these indexes, and `upgrade head` would never
    # rebuild them. The column drops below take the same ACCESS EXCLUSIVE locks
    # on both tables, so the concurrent drop never spared a writer anything.
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    for name, _table, _definition in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute(f"ALTER TABLE usage_events DROP CONSTRAINT IF EXISTS {AI_TURN_ONLY_CONSTRAINT}")
    for column in USAGE_COLUMNS:
        op.drop_column("usage_events", column)
    op.execute(f"ALTER TABLE agent_turns DROP CONSTRAINT IF EXISTS {CHARGE_STATE_CONSTRAINT}")
    for column in TURN_COLUMNS:
        op.drop_column("agent_turns", column)
    bind = op.get_bind()
    postgresql.ENUM(name="ai_turn_release_reason").drop(bind, checkfirst=True)
    postgresql.ENUM(name="ai_turn_charge_state").drop(bind, checkfirst=True)
