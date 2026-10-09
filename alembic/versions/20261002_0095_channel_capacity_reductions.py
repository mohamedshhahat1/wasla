"""Capacity reductions: the grace, the owner's choice and the automatic fallback.

Revision ID: 0095
Revises: 0094

ADR-131 (ENT-14, ENT-15).

1. **`audit_action` gains** `channel_capacity_reduction_opened`,
   `channel_capacity_reduction_resolved` and `channel_capacity_selection_saved`
   - appended in an autocommit block first, as every label is.
2. **`channel_capacity_reductions`** - one workspace's obligation to come down to
   a smaller capacity: cause, the target it must fit (general slots, typed slots,
   allowed channel types), when it took effect, when the grace ends, whether the
   owners were told and warned, and how it ended. One open per workspace
   (`uq_channel_capacity_reductions_open`, partial); the billing worker's grace
   phase reads `ix_channel_capacity_reductions_grace_ends_at`, partial too.
3. **`channel_capacity_preselections`** - the connections an owner chose ahead
   of a boundary. The key to `channel_connections (tenant_id, id)` carries the
   workspace, so a selection can only name the workspace's own connections.

**Online.** Two new, empty tables and two new enum types: nothing existing is
rewritten or locked beyond the catalogue entries. `lock_timeout` is bounded.

**Downgrade refuses** while any reduction or pre-selection exists, or any audit
entry names one of the three actions: the pre-0095 schema cannot explain the
connections a reduction disabled. The enum labels stay.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"

AUDIT_ACTIONS = (
    "channel_capacity_reduction_opened",
    "channel_capacity_reduction_resolved",
    "channel_capacity_selection_saved",
)
CAUSES = ("downgrade", "topup_expired", "topup_withdrawn", "grant_expired", "migration")
STATUSES = (
    "pending_selection",
    "resolved_by_owner",
    "resolved_automatically",
    "no_longer_needed",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for action in AUDIT_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{action}'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    bind = op.get_bind()
    postgresql.ENUM(*CAUSES, name="channel_capacity_reduction_cause").create(bind, checkfirst=True)
    postgresql.ENUM(*STATUSES, name="channel_capacity_reduction_status").create(
        bind, checkfirst=True
    )

    op.create_table(
        "channel_capacity_reductions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "cause",
            postgresql.ENUM(name="channel_capacity_reduction_cause", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="channel_capacity_reduction_status", create_type=False),
            nullable=False,
        ),
        sa.Column("target_general", sa.BigInteger(), nullable=False),
        sa.Column(
            "target_typed",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("target_allowed_types", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grace_ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "kept_connection_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True
        ),
        sa.Column(
            "disabled_connection_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "grace_ends_at >= effective_at",
            name=op.f("ck_channel_capacity_reductions_grace_after_effective"),
        ),
        sa.CheckConstraint(
            "(status = 'pending_selection') = (resolved_at IS NULL)",
            name=op.f("ck_channel_capacity_reductions_resolved_when_closed"),
        ),
        sa.CheckConstraint(
            "target_general >= 0",
            name=op.f("ck_channel_capacity_reductions_target_general_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_channel_capacity_reductions_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name="fk_channel_capacity_reductions_resolved_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_channel_capacity_reductions"),
    )
    op.create_index(
        "ix_channel_capacity_reductions_tenant_id", "channel_capacity_reductions", ["tenant_id"]
    )
    op.create_index(
        "uq_channel_capacity_reductions_open",
        "channel_capacity_reductions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending_selection'"),
    )
    op.create_index(
        "ix_channel_capacity_reductions_grace_ends_at",
        "channel_capacity_reductions",
        ["grace_ends_at"],
        postgresql_where=sa.text("status = 'pending_selection'"),
    )

    op.create_table(
        "channel_capacity_preselections",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selected_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_channel_capacity_preselections_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["channel_connections.tenant_id", "channel_connections.id"],
            name="fk_channel_capacity_preselections_tenant_connection",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["selected_by"],
            ["users.id"],
            name="fk_channel_capacity_preselections_selected_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "connection_id", name="pk_channel_capacity_preselections"
        ),
    )
    op.create_index(
        "ix_channel_capacity_preselections_tenant_id",
        "channel_capacity_preselections",
        ["tenant_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    for table in ("channel_capacity_reductions", "channel_capacity_preselections"):
        count = bind.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one()  # noqa: S608
        if count:
            found.append(f"{table}: {count}")
    quoted = ", ".join(f"'{action}'" for action in AUDIT_ACTIONS)
    audited = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text IN ({quoted})"  # noqa: S608
    ).scalar_one()
    if audited:
        found.append(f"audit_logs naming a capacity reduction: {audited}")
    if found:
        raise RuntimeError(
            "Capacity reductions cannot be downgraded "
            "(docs/RUNBOOK.md, 'Entitlements and channel capacity (0092-0098)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_table("channel_capacity_preselections")
    op.drop_table("channel_capacity_reductions")
    postgresql.ENUM(name="channel_capacity_reduction_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="channel_capacity_reduction_cause").drop(bind, checkfirst=True)
