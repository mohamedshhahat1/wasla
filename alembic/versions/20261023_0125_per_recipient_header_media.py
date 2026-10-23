"""A campaign may give different recipients different header files.

Revision ID: 0125
Revises: 0124

HMR-01 .. HMR-03 (ADR-154).

1. `campaigns.header_media_mode` (`campaign` / `per_recipient`, NOT NULL,
   constant default `campaign` - catalogue only; `ck_campaigns_header_media_mode_known`),
   `campaigns.header_media_rules` (JSONB `[{when: {field, key?, equals},
   header_media_id}]`, first match wins) and `campaigns.header_media_refusals`
   (JSONB `{file id: refusals}` - Meta refusing one file's upload).
2. `campaign_recipients.header_media_id`: the file chosen for the person at
   materialisation and frozen there, a tenant-composite key to the file
   (`fk_campaign_recipients_tenant_header_media`, RESTRICT), its partial index
   built `CONCURRENTLY`.
3. `ck_campaign_recipients_reason_code_known` gains `header_media_refused`.

**Online.** Nullable columns and a constant default (no rewrite); the CHECKs and
the foreign key added `NOT VALID` and validated under a bounded `lock_timeout`;
the index built concurrently with an INVALID leftover dropped first.

**Downgrade refuses** while any campaign is per-recipient or records rules or
refusals, any recipient names a file, or any recipient was skipped
`header_media_refused`. Otherwise it drops exactly what the upgrade added.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
RUNBOOK = "docs/RUNBOOK.md, 'Follow-ups, header media and realtime (0121-0125)'"
MODE_CHECK = "ck_campaigns_header_media_mode_known"
REASON_CHECK = "ck_campaign_recipients_reason_code_known"
FK = "fk_campaign_recipients_tenant_header_media"
INDEX = "ix_campaign_recipients_header_media_id"
REASONS_0113 = (
    "opted_out",
    "contact_gone",
    "other_connection",
    "identity_changed",
    "channel_not_in_plan",
    "recipient_opted_out_at_provider",
    "marketing_limit_per_user",
    "undeliverable",
    "delivery_unconfirmed",
    "attempts_exhausted",
    "template_parameters_refused",
    "send_window_closed",
    "contact_frequency_cap",
)
NEW_REASONS = ("header_media_refused",)


def _check(column: str, values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{value}'" for value in values)
    return f"CHECK ({column} IS NULL OR {column} IN ({listed}))"


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "campaigns",
        sa.Column(
            "header_media_mode",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'campaign'"),
        ),
    )
    op.add_column(
        "campaigns",
        sa.Column("header_media_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "campaigns",
        sa.Column("header_media_refusals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("campaign_recipients", sa.Column("header_media_id", sa.UUID(), nullable=True))
    op.execute(
        f"ALTER TABLE campaigns ADD CONSTRAINT {MODE_CHECK}"
        " CHECK (header_media_mode IN ('campaign', 'per_recipient')) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE campaign_recipients ADD CONSTRAINT {FK}"
        " FOREIGN KEY (tenant_id, header_media_id)"
        " REFERENCES campaign_header_media (tenant_id, id) ON DELETE RESTRICT NOT VALID"
    )
    op.execute(f"ALTER TABLE campaign_recipients DROP CONSTRAINT {REASON_CHECK}")
    op.execute(
        f"ALTER TABLE campaign_recipients ADD CONSTRAINT {REASON_CHECK}"
        f" {_check('reason_code', REASONS_0113 + NEW_REASONS)} NOT VALID"
    )
    op.execute(f"ALTER TABLE campaigns VALIDATE CONSTRAINT {MODE_CHECK}")
    op.execute(f"ALTER TABLE campaign_recipients VALIDATE CONSTRAINT {FK}")
    op.execute(f"ALTER TABLE campaign_recipients VALIDATE CONSTRAINT {REASON_CHECK}")
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
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX}"
            " ON campaign_recipients (header_media_id) WHERE header_media_id IS NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    for label, sql in (
        (
            "per-recipient campaigns, or campaigns recording header rules or refusals",
            "SELECT count(*) FROM campaigns WHERE header_media_mode <> 'campaign'"
            " OR header_media_rules IS NOT NULL OR header_media_refusals IS NOT NULL",
        ),
        (
            "recipients naming a header file",
            "SELECT count(*) FROM campaign_recipients WHERE header_media_id IS NOT NULL",
        ),
        (
            "recipients skipped header_media_refused",
            "SELECT count(*) FROM campaign_recipients WHERE reason_code = 'header_media_refused'",
        ),
    ):
        count = bind.exec_driver_sql(sql).scalar_one()
        if count:
            found.append(f"{label}: {count}")
    if found:
        raise RuntimeError(
            f"Per-recipient header media cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"ALTER TABLE campaign_recipients DROP CONSTRAINT IF EXISTS {FK}")
    op.execute(f"ALTER TABLE campaign_recipients DROP CONSTRAINT IF EXISTS {REASON_CHECK}")
    op.execute(
        f"ALTER TABLE campaign_recipients ADD CONSTRAINT {REASON_CHECK} "
        f"{_check('reason_code', REASONS_0113)}"
    )
    op.execute(f"ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS {MODE_CHECK}")
    op.drop_column("campaign_recipients", "header_media_id")
    op.drop_column("campaigns", "header_media_refusals")
    op.drop_column("campaigns", "header_media_rules")
    op.drop_column("campaigns", "header_media_mode")
