"""A campaign's template header sent with an image, a video or a document.

Revision ID: 0107
Revises: 0106

HM-01 .. HM-08 (ADR-144). CMP-05's deferred half.

1. **Label**, appended in an autocommit block first, as every label is:
   `audit_action` gains `campaign_header_media_uploaded`. Enum labels cannot be
   dropped; the downgrade leaves it.
2. `campaign_header_media` - a workspace's stored header file: its kind, the
   canonical type resolved from its bytes, size, document name, SHA-256, and
   the object it owns under the `message_media` storage lifecycle (the same
   `media_storage_state` type and the same state CHECK). `(tenant_id, id)` is
   unique so campaigns reference it with the workspace.
3. `campaigns.header_media_id` -> `(tenant_id, header_media_id)` references
   `campaign_header_media (tenant_id, id)`; `header_provider_media_id`,
   `header_provider_uploaded_at` and `header_provider_account_id` -> `(tenant_id,
   header_provider_account_id)` references `whatsapp_accounts (tenant_id, id)`:
   Meta's id for the file, when and through which number it was uploaded.
   `ck_campaigns_header_provider_complete` keeps the three together.
4. `ck_campaigns_status_reason_known` gains `header_media_unavailable` and
   `header_media_refused`.
5. `ix_campaigns_header_media_id`, partial on `header_media_id IS NOT NULL`.

**Online.** The new table is empty when created. The four columns on
`campaigns` are nullable without a default: catalogue-only. Every constraint
on `campaigns` is added `NOT VALID` and then validated under a bounded
`lock_timeout`, so the validating scans hold only `SHARE UPDATE EXCLUSIVE`; the
index is built `CONCURRENTLY` in an autocommit block, an INVALID leftover of a
failed build dropped first (0100's pattern). No table rewrite; `messages` and
`campaign_recipients` are not touched.

**Downgrade refuses** while any asset exists, any campaign names one or carries
Meta's id, any campaign stopped for a header reason, or any audit entry records
an upload: each would lose what a campaign was sent with or why it stopped.
Otherwise it drops exactly what the upgrade added, in the run's transaction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
LABEL = "campaign_header_media_uploaded"
TABLE = "campaign_header_media"
REASON_CHECK = "ck_campaigns_status_reason_known"
MEDIA_FK = "fk_campaigns_tenant_header_media"
ACCOUNT_FK = "fk_campaigns_tenant_header_provider_account"
COMPLETE_CHECK = "ck_campaigns_header_provider_complete"
INDEX = "ix_campaigns_header_media_id"
COUNT_ASSETS = "SELECT count(*) FROM campaign_header_media"
KINDS = ("image", "video", "document")
REASONS_0102 = (
    "workspace_suspended",
    "workspace_deleted",
    "subscription_suspended",
    "admin_paused",
    "template_removed",
    "template_not_approved",
    "template_withdrawn",
    "template_category",
    "template_parameters_refused",
    "connection_disabled",
    "credential_refused",
    "connection_refused",
    "provider_unreachable",
    "repeated_batch_failure",
)
NEW_REASONS = ("header_media_unavailable", "header_media_refused")
STORAGE_STATE_CHECK = (
    "(storage_state = 'absent' AND storage_key IS NULL) "
    "OR (storage_state = 'pending' AND storage_key IS NOT NULL "
    "AND upload_started_at IS NOT NULL AND purge_started_at IS NULL) "
    "OR (storage_state = 'stored' AND storage_key IS NOT NULL "
    "AND purge_started_at IS NULL) "
    "OR (storage_state = 'purging' AND storage_key IS NOT NULL "
    "AND purge_started_at IS NOT NULL) "
    "OR (storage_state = 'purged' AND storage_key IS NULL "
    "AND purge_started_at IS NOT NULL) "
    "OR (storage_state = 'mismatched' AND storage_key IS NOT NULL)"
)


def _reason_check(reasons: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{reason}'" for reason in reasons)
    return f"CHECK (status_reason IS NULL OR status_reason IN ({allowed}))"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{LABEL}'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        TABLE,
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mime_type", sa.String(150), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(300), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=True),
        sa.Column(
            "storage_state",
            postgresql.ENUM(name="media_storage_state", create_type=False),
            nullable=False,
            server_default="absent",
        ),
        sa.Column("upload_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
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
            ["created_by_id"],
            ["users.id"],
            name="fk_campaign_header_media_created_by_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_campaign_header_media_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_header_media"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_campaign_header_media_tenant_id_id"),
        sa.UniqueConstraint("storage_key", name="uq_campaign_header_media_storage_key"),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{kind}'" for kind in KINDS) + ")",
            name=op.f("ck_campaign_header_media_kind_known"),
        ),
        sa.CheckConstraint(
            "byte_size > 0", name=op.f("ck_campaign_header_media_byte_size_positive")
        ),
        sa.CheckConstraint(
            STORAGE_STATE_CHECK, name=op.f("ck_campaign_header_media_storage_state")
        ),
    )
    op.create_index("ix_campaign_header_media_tenant_id", TABLE, ["tenant_id"])
    op.create_index(
        "ix_campaign_header_media_retention",
        TABLE,
        ["created_at"],
        postgresql_where=sa.text("storage_state = 'stored'"),
    )
    op.create_index(
        "ix_campaign_header_media_pending_upload",
        TABLE,
        ["upload_started_at"],
        postgresql_where=sa.text("storage_state = 'pending'"),
    )

    op.add_column("campaigns", sa.Column("header_media_id", postgresql.UUID(as_uuid=True)))
    op.add_column("campaigns", sa.Column("header_provider_media_id", sa.String(255)))
    op.add_column("campaigns", sa.Column("header_provider_uploaded_at", sa.DateTime(timezone=True)))
    op.add_column(
        "campaigns", sa.Column("header_provider_account_id", postgresql.UUID(as_uuid=True))
    )

    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT {REASON_CHECK}")
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {REASON_CHECK}"
        f" {_reason_check(REASONS_0102 + NEW_REASONS)} NOT VALID"
    )
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {MEDIA_FK}"
        f" FOREIGN KEY (tenant_id, header_media_id) REFERENCES {TABLE} (tenant_id, id)"
        " NOT VALID"
    )
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {ACCOUNT_FK}"
        " FOREIGN KEY (tenant_id, header_provider_account_id)"
        " REFERENCES whatsapp_accounts (tenant_id, id) ON DELETE CASCADE NOT VALID"
    )
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {COMPLETE_CHECK} CHECK ("
        "(header_provider_media_id IS NULL AND header_provider_uploaded_at IS NULL"
        " AND header_provider_account_id IS NULL)"
        " OR (header_provider_media_id IS NOT NULL AND header_provider_uploaded_at IS NOT NULL"
        " AND header_provider_account_id IS NOT NULL AND header_media_id IS NOT NULL)"
        ") NOT VALID"
    )
    for constraint in (REASON_CHECK, MEDIA_FK, ACCOUNT_FK, COMPLETE_CHECK):
        op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {constraint}")
    op.execute("RESET lock_timeout")

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
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON campaigns (header_media_id)"
            " WHERE header_media_id IS NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    assets = bind.exec_driver_sql(COUNT_ASSETS).scalar_one()
    if assets:
        found.append(f"campaign header media files: {assets}")
    named = bind.exec_driver_sql(
        "SELECT count(*) FROM campaigns WHERE header_media_id IS NOT NULL"
        " OR header_provider_media_id IS NOT NULL"
    ).scalar_one()
    if named:
        found.append(f"campaigns naming a header file or Meta's id for one: {named}")
    reasons = ", ".join(f"'{reason}'" for reason in NEW_REASONS)
    stopped = bind.exec_driver_sql(
        f"SELECT count(*) FROM campaigns WHERE status_reason IN ({reasons})"  # noqa: S608 - module constants
    ).scalar_one()
    if stopped:
        found.append(f"campaigns stopped for a header file: {stopped}")
    audited = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text = '{LABEL}'"  # noqa: S608 - module constant
    ).scalar_one()
    if audited:
        found.append(f"audit_logs recording a header file upload: {audited}")
    if found:
        raise RuntimeError(
            "Campaign header media cannot be downgraded "
            "(docs/RUNBOOK.md, 'Campaign header media (0107)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {COMPLETE_CHECK}")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {ACCOUNT_FK}")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {MEDIA_FK}")
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {REASON_CHECK}")
    op.execute(f"ALTER TABLE campaigns ADD CONSTRAINT {REASON_CHECK} {_reason_check(REASONS_0102)}")
    for column in (
        "header_provider_account_id",
        "header_provider_uploaded_at",
        "header_provider_media_id",
        "header_media_id",
    ):
        op.drop_column("campaigns", column)
    op.drop_table(TABLE)
