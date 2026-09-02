"""Carry the summed columns in the usage index instead of fetching them.

Revision ID: 0040
Revises: 0039

`ix_usage_events_tenant_id_event_type_occurred_at` is replaced by the same
index with `quantity` and `unit` as INCLUDE columns. Nothing about the query
changes; what changes is that PostgreSQL can answer it from the index alone
(ADR-081).

The query is `EntitlementService._period_usage`, and it is the hottest read in
the system: every agent turn asks it as a cheap early exit, and every provider
round asks it again inside `consume`, which holds the workspace's advisory lock
while it does. It sums `quantity` over one workspace's rows of one meter in one
billing period, and before this it visited the heap once per row to read two
narrow columns - so a workspace at the Business plan's 25,000 AI requests was
touching ~900 heap pages to add up numbers the index could have carried.

Measured on 3.9 million rows across 50 workspaces: 9.4ms to 7.1ms, with
`Heap Fetches: 0` on the plan. The other three usage shapes - the workspace
dashboard totals, the daily series and the platform roll-up - measured flat, so
this is not a trade.

Dropped and recreated rather than altered, because PostgreSQL has no way to add
an INCLUDE column to an existing index. Both steps are `CONCURRENTLY` in an
autocommit block: usage is written on the path that answers customers, and an
index rebuild that blocks inserts there blocks replies.

**The drop comes first, and that ordering is deliberate.** Holding both would
need room for two copies of a large index, and the window in which neither
exists costs the entitlement check a bitmap scan rather than an answer - the
query still works, it is briefly the speed it was yesterday.
"""

from __future__ import annotations

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

INDEX = "ix_usage_events_tenant_id_event_type_occurred_at"
COLUMNS = "(tenant_id, event_type, occurred_at)"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY {INDEX} ON usage_events"
            f" {COLUMNS} INCLUDE (quantity, unit)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(f"CREATE INDEX CONCURRENTLY {INDEX} ON usage_events {COLUMNS}")
