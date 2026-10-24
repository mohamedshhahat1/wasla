"""A workspace's automatic follow-up settings, on by default.

Revision ID: 0122
Revises: 0121

FUS-01 (ADR-151).

1. **Labels**, appended in an autocommit block first, as every label is:
   `audit_action` gains `follow_up_settings_updated` and
   `follow_up_stopped_by_person`. Enum labels cannot be dropped; the downgrade
   leaves them.
2. `follow_up_settings`: one row per workspace (`uq_follow_up_settings_tenant_id`)
   - `enabled`, `channel_types` (JSONB), `steps` (JSONB `[{delay_minutes,
   guidance}]`), `version`, `updated_by_id`.
3. **The owner's default for every existing workspace**: on, WhatsApp, one
   reminder after five minutes - the card opens showing "مفعّل · خطوة واحدة"
   with "بعد 5 دقائق". New workspaces get the same row at creation.

**Online.** A new table, filled with one row per workspace in one statement
(the tenants table is small and only read). No existing table is altered.

**Downgrade refuses** while any workspace's settings differ from the default,
or any audit entry records a settings change or a stop: each would be lost.
Otherwise it drops the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
LABELS = ("follow_up_settings_updated", "follow_up_stopped_by_person")
DEFAULT_CHANNELS = '["whatsapp"]'
DEFAULT_STEPS = '[{"delay_minutes": 5, "guidance": null}]'
RUNBOOK = "docs/RUNBOOK.md, 'Follow-ups, header media and realtime (0121-0125)'"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label in LABELS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{label}'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        "follow_up_settings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("channel_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
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
            name=op.f("fk_follow_up_settings_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_follow_up_settings_updated_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_follow_up_settings")),
        sa.UniqueConstraint("tenant_id", name="uq_follow_up_settings_tenant_id"),
    )
    op.execute(
        "INSERT INTO follow_up_settings (id, tenant_id, enabled, channel_types, steps, version)"  # noqa: S608 - module constants
        f" SELECT gen_random_uuid(), t.id, true, '{DEFAULT_CHANNELS}'::jsonb,"
        f" '{DEFAULT_STEPS}'::jsonb, 1 FROM tenants t"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    changed = bind.exec_driver_sql(
        "SELECT count(*) FROM follow_up_settings WHERE NOT enabled OR version <> 1"  # noqa: S608 - module constants
        f" OR channel_types <> '{DEFAULT_CHANNELS}'::jsonb OR steps <> '{DEFAULT_STEPS}'::jsonb"
    ).scalar_one()
    if changed:
        found.append(f"workspaces whose follow-up settings differ from the default: {changed}")
    labels = ", ".join(f"'{label}'" for label in LABELS)
    audited = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text IN ({labels})"  # noqa: S608 - module constants
    ).scalar_one()
    if audited:
        found.append(f"audit_logs recording a follow-up settings change or a stop: {audited}")
    if found:
        raise RuntimeError(
            f"Follow-up settings cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_table("follow_up_settings")
