"""The inbox narrowed to one channel, in the inbox's own order.

Revision ID: 0091
Revises: 0090

OMNI-048. `GET /conversations?channel=` read the workspace's conversations by
`tenant_id` and discarded the other channels by predicate (the final audit's
E5). `(tenant_id, channel, last_message_at DESC NULLS LAST, id DESC)` serves
the filter and the order together, as 0084's connection index does for
`?account_id=`.

**Online.** `CREATE INDEX CONCURRENTLY` in an autocommit block. A build that
failed part-way leaves an INVALID index that `IF NOT EXISTS` would keep, so an
invalid leftover of this name is dropped and rebuilt first (0084's pattern).

**Downgrade** drops the index in the run's own transaction, a plain
`DROP INDEX IF EXISTS` after `SET LOCAL lock_timeout = '15s'` (MIG-0091,
ADR-132). It used to drop it `CONCURRENTLY` in an autocommit block, which
commits every downgrade step above 0091: a downgrade from head that 0090 (or
any lower migration) then refused stopped stamped 0091 without the index, and
`alembic upgrade head` never rebuilt it - the stamp said it was there. Now a
refusal anywhere below rolls the whole run back and the database is still at
its starting head with the index valid. The same fix 0094's downgrade got in
9695ec0.

The drop takes an ACCESS EXCLUSIVE lock on `conversations` for the moment of
the catalogue change, waiting at most 15 s for it (then the whole run fails
and changes nothing). A downgrade is an operator action run with the
application stopped (docs/RUNBOOK.md, deploy rule), so nothing waits on it.
Only this downgrade changed; the upgrade is as it was, so a database already
at head is unaffected. A database damaged by the old downgrade is found by
`python -m scripts.db_preflight verify` (it names the missing index).
"""

from __future__ import annotations

from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None

INDEX = "ix_conversations_tenant_id_channel_last_message_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        invalid = (
            op.get_bind()
            .exec_driver_sql(
                "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608 - module constant
                f" WHERE c.relname = '{INDEX}' AND NOT i.indisvalid"
            )
            .scalar_one()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON conversations"
            " (tenant_id, channel, last_message_at DESC NULLS LAST, id DESC)"
        )


def downgrade() -> None:
    # In the run's transaction, not `CONCURRENTLY` (MIG-0091): an autocommit
    # block commits every downgrade step above this one, so a refusal further
    # down would leave the stamp here without this index, and `upgrade head` would
    # never rebuild it. Run with the application stopped (docs/RUNBOOK.md);
    # the drop waits at most 15 s for its lock, then fails the whole run.
    op.execute("SET LOCAL lock_timeout = '15s'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
