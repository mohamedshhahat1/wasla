"""Template header files for manual and follow-up templates; one Meta upload per file.

Revision ID: 0124
Revises: 0123

HMT-01 .. HMT-03, HMR-02 (ADR-154).

1. `campaign_header_media.deleted_at`: a file deleted from the workspace's
   template header library. Its name stays (HMT-01 renames nothing): the table
   now holds every template header file - for campaigns, a person's template
   and a follow-up template. A deleted file is no longer listed or usable and
   the retention sweep purges it.
2. `template_header_uploads`: Meta's id for one file through one connection
   (HMR-02, HMT-02) - `(tenant_id, header_media_id, account_id, campaign_id)`,
   `provider_media_id`, `uploaded_at`. `campaign_id` is null for a person's or
   a follow-up template, whose key is the file and the connection
   (`uq_template_header_uploads_file_connection`); a campaign's own uploads
   are keyed by the campaign too (`uq_template_header_uploads_campaign_file`).
   Tenant-composite keys to the file, the connection and the campaign
   (ADR-100). The provider id never leaves the server (HM-08).
3. `follow_ups.header_media_id`: the file a follow-up template is sent with,
   a tenant-composite key (HMT-03), and its partial index (`CONCURRENTLY`).

**Online.** A new table; nullable columns on existing tables; the foreign key
on `follow_ups` added `NOT VALID` and validated under a bounded `lock_timeout`;
the index built concurrently with an INVALID leftover dropped first.

**Downgrade refuses** while any file is deleted, any upload is recorded or any
follow-up names a file. Otherwise it drops exactly what the upgrade added.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
RUNBOOK = "docs/RUNBOOK.md, 'Follow-ups, header media and realtime (0121-0125)'"
FK = "fk_follow_ups_tenant_header_media"
INDEX = "ix_follow_ups_header_media_id"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "campaign_header_media",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "template_header_uploads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("header_media_id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("campaign_id", sa.UUID(), nullable=True),
        sa.Column("provider_media_id", sa.String(255), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
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
            name=op.f("fk_template_header_uploads_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "header_media_id"],
            ["campaign_header_media.tenant_id", "campaign_header_media.id"],
            name="fk_template_header_uploads_tenant_header_media",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["whatsapp_accounts.tenant_id", "whatsapp_accounts.id"],
            name="fk_template_header_uploads_tenant_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_template_header_uploads_tenant_campaign",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_header_uploads")),
    )
    op.create_index(
        "ix_template_header_uploads_tenant_id", "template_header_uploads", ["tenant_id"]
    )
    op.create_index(
        "uq_template_header_uploads_file_connection",
        "template_header_uploads",
        ["tenant_id", "header_media_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("campaign_id IS NULL"),
    )
    op.create_index(
        "uq_template_header_uploads_campaign_file",
        "template_header_uploads",
        ["campaign_id", "header_media_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("campaign_id IS NOT NULL"),
    )
    op.add_column("follow_ups", sa.Column("header_media_id", sa.UUID(), nullable=True))
    op.execute(
        f"ALTER TABLE follow_ups ADD CONSTRAINT {FK} FOREIGN KEY (tenant_id, header_media_id)"
        " REFERENCES campaign_header_media (tenant_id, id) ON DELETE RESTRICT NOT VALID"
    )
    op.execute(f"ALTER TABLE follow_ups VALIDATE CONSTRAINT {FK}")
    op.execute("RESET lock_timeout")

    with op.get_context().autocommit_block():
        bind = op.get_bind()
        invalid = bind.exec_driver_sql(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"  # noqa: S608
            f" WHERE c.relname = '{INDEX}' AND NOT i.indisvalid"
        ).first()
        if invalid is not None:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON follow_ups (header_media_id)"
            " WHERE header_media_id IS NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    for label, sql in (
        (
            "deleted template header files",
            "SELECT count(*) FROM campaign_header_media WHERE deleted_at IS NOT NULL",
        ),
        ("recorded template header uploads", "SELECT count(*) FROM template_header_uploads"),
        (
            "follow-ups naming a header file",
            "SELECT count(*) FROM follow_ups WHERE header_media_id IS NOT NULL",
        ),
    ):
        count = bind.exec_driver_sql(sql).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            f"Template header media cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"ALTER TABLE follow_ups DROP CONSTRAINT IF EXISTS {FK}")
    op.drop_column("follow_ups", "header_media_id")
    op.drop_table("template_header_uploads")
    op.drop_column("campaign_header_media", "deleted_at")
