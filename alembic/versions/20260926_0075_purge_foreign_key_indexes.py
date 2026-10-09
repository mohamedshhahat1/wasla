"""Index the foreign keys a workspace purge fires for every deleted row.

Revision ID: 0075
Revises: 0074

The database audit (DB-002) measured a workspace purge dominated by foreign-key
actions no index could serve. Deleting one workspace's 10,000 messages took
6.4 s, 5.3 s of it in `fk_campaign_recipients_message_id_messages`: each deleted
message ran `UPDATE campaign_recipients SET message_id = NULL WHERE message_id =
$1`, a sequential scan of *every workspace's* recipients. The cost was messages
x platform-wide recipients, inside one transaction.

Four indexes, one per foreign key the purge's deletes fire:

    campaign_recipients (message_id)       WHERE message_id IS NOT NULL
    campaign_recipients (conversation_id)  WHERE conversation_id IS NOT NULL
    follow_ups          (message_id)       WHERE message_id IS NOT NULL
    agent_turns         (conversation_id)

Partial where the column is nullable: the action's `WHERE column = $1` is
strict, so PostgreSQL proves the predicate and uses the index, and rows that
never named a message cost nothing to keep.

**Built `CONCURRENTLY`.** These are among the largest tables on the platform,
and a plain `CREATE INDEX` blocks every write to them for the whole build.
`CREATE INDEX CONCURRENTLY` cannot run inside a transaction, so this migration
runs entirely in Alembic's autocommit block. A build that fails part-way
leaves an INVALID index behind, which `IF NOT EXISTS` would then silently
accept; each build therefore drops an invalid index of its own name first, so
re-running the migration after a failure finishes the job rather than
skipping it.

**Downgrade (MIG-0091 survey, ADR-132)** drops the four indexes in the run's transaction
under a 15 s lock_timeout, no longer `CONCURRENTLY` in an autocommit block: a
lower migration refusing used to leave the stamp at this revision without the four indexes,
and `upgrade head` never rebuilt them. The upgrade is unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None

# name, table, column list, predicate (or None)
INDEXES = (
    (
        "ix_campaign_recipients_message_id",
        "campaign_recipients",
        "message_id",
        "message_id IS NOT NULL",
    ),
    (
        "ix_campaign_recipients_conversation_id",
        "campaign_recipients",
        "conversation_id",
        "conversation_id IS NOT NULL",
    ),
    ("ix_follow_ups_message_id", "follow_ups", "message_id", "message_id IS NOT NULL"),
    ("ix_agent_turns_conversation_id", "agent_turns", "conversation_id", None),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, columns, predicate in INDEXES:
            invalid = (
                op.get_bind()
                .execute(
                    sa.text(
                        "SELECT count(*) FROM pg_index x JOIN pg_class c ON c.oid = x.indexrelid"
                        " WHERE c.relname = :name AND NOT x.indisvalid"
                    ),
                    {"name": name},
                )
                .scalar_one()
            )
            if invalid:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            where = f" WHERE {predicate}" if predicate else ""
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({columns}){where}"
            )


def downgrade() -> None:
    # In the run's transaction, not `CONCURRENTLY` (MIG-0091): an autocommit
    # block commits every downgrade step above this one, so a refusal further
    # down would leave the stamp here without these indexes, and `upgrade head` would
    # never rebuild them. Run with the application stopped (docs/RUNBOOK.md);
    # the drop waits at most 15 s for its lock, then fails the whole run.
    op.execute("SET LOCAL lock_timeout = '15s'")
    for name, _, _, _ in reversed(INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {name}")
