"""index the windows analytics reads

Revision ID: 0019
Revises: 0018

Four indexes, all `(tenant_id, created_at)`, all for queries phase 12 added and
phase 12 did not index.

Every tenant analytics figure is "this workspace, this window". The tables it
reads had a `(tenant_id)` index and nothing carrying the time, so PostgreSQL
found the workspace's rows and then discarded most of them by filter — measured
before this migration on fifty workspaces and fifty thousand messages:

    Bitmap Heap Scan on messages  (rows=150)
      Recheck Cond: (tenant_id = $0)
      Filter: (created_at >= …) AND (created_at < …)
      Rows Removed by Filter: 850
      Buffers: shared hit=772

The waste is proportional to how long a workspace has existed, which is the
worst possible shape: the dashboard gets slower for exactly the customers who
have been paying longest. `leads` already had this index, which is why the lead
figures were the only ones that did not have the problem.

Not added: an index for the platform-wide analytics reads. Those aggregate every
workspace and are read by a handful of staff; a tenant-leading index cannot
serve them and the sequential scan they do instead is the honest plan for
"count everything".
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

# (index name, table, columns). Written as data so the upgrade and the downgrade
# cannot disagree about what was created.
INDEXES = (
    ("ix_conversations_tenant_id_created_at", "conversations", ["tenant_id", "created_at"]),
    ("ix_messages_tenant_id_created_at", "messages", ["tenant_id", "created_at"]),
    (
        "ix_message_sentiments_tenant_id_created_at",
        "message_sentiments",
        ["tenant_id", "created_at"],
    ),
    (
        "ix_campaign_recipients_tenant_id_created_at",
        "campaign_recipients",
        ["tenant_id", "created_at"],
    ),
)


def upgrade():
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade():
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
