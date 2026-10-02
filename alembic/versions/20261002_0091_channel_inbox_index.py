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

**Downgrade** drops the index, also concurrently; nothing is lost.
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
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
