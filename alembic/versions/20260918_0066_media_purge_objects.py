"""record the object deletes a workspace purge still owes

Revision ID: 0066
Revises: 0065

One table, ``media_purge_objects``.

A workspace purge deletes the rows that name a workspace's files and deletes
the objects after its transaction commits. The keys lived only in the worker's
memory in between, so a refused delete - or a process that died after the
commit - left the customer's files in the bucket with no row anywhere naming
them, and the workspace recorded as purged (MEDIA-07). Each key is now written
here in the same transaction that deletes its row, and removed only once the
store confirms the object is gone.

``not_before`` defers the delete of a key whose upload was still in flight at
the purge, until the upload grace period has passed, so a write landing late is
deleted rather than orphaned.

New and empty: nothing is backfilled. Objects already orphaned by earlier purges
have no row to derive a key from, which is the defect this closes and not
something a migration can recover; operator guidance is in docs/RUNBOOK.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_purge_objects",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_media_purge_objects_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_purge_objects")),
        sa.UniqueConstraint("storage_key", name="uq_media_purge_objects_storage_key"),
    )
    op.create_index(
        "ix_media_purge_objects_not_before", "media_purge_objects", ["not_before"]
    )
    op.create_index("ix_media_purge_objects_tenant_id", "media_purge_objects", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_media_purge_objects_tenant_id", table_name="media_purge_objects")
    op.drop_index("ix_media_purge_objects_not_before", table_name="media_purge_objects")
    op.drop_table("media_purge_objects")
