"""Runs of a workspace's automatic follow-ups, and the steps that belong to them.

Revision ID: 0123
Revises: 0122

FUS-02 .. FUS-06 (ADR-151).

1. `follow_up_sequence_runs`: one conversation's run of the reminders -
   `anchor_at`, `state` (active / ended), `end_reason` (a closed vocabulary),
   `ended_at`, `steps_sent`. `(tenant_id, id)` unique; a tenant-composite key to
   its conversation (ADR-100); `uq_follow_up_sequence_runs_active_conversation`,
   a partial unique index: one active run per conversation.
2. `follow_ups.origin` (`one_off` / `sequence`, NOT NULL, constant default
   `one_off` - catalogue only), `sequence_run_id` (tenant-composite key to its
   run), `step_index`, `anchor_at`, `skip_reason` (a closed vocabulary), and the
   CHECKs that a sequence step names all three and a one-off none. A partial
   index on `sequence_run_id`, built `CONCURRENTLY`.

**Online.** A new table; on `follow_ups` nullable columns and a constant
default (no rewrite); every CHECK and the foreign key added `NOT VALID` and
validated under a bounded `lock_timeout`; the index built concurrently in an
autocommit block with an INVALID leftover dropped first.

**Downgrade refuses** while any run exists or any follow-up is a sequence step:
each would be lost. Otherwise it drops exactly what the upgrade added.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
RUNBOOK = "docs/RUNBOOK.md, 'Follow-ups, header media and realtime (0121-0125)'"
END_REASONS = (
    "ai_skipped",
    "ai_unavailable",
    "allowance_exhausted",
    "channel_unavailable",
    "completed",
    "conversation_closed",
    "customer_replied",
    "failed",
    "guardrail",
    "human_took_over",
    "lead_closed",
    "not_served",
    "opted_out",
    "purchased",
    "replaced_by_person",
    "sequence_restarted",
    "settings_changed",
    "stopped_by_person",
    "window_closed",
    "window_closing",
)
SKIP_REASONS = (
    "concluded",
    "purchased",
    "declined",
    "waiting_on_business",
    "inappropriate",
    "other",
    "guardrail",
    "window_closed",
    "ai_unavailable",
    "allowance_exhausted",
    "not_served",
    "human_took_over",
    "conversation_closed",
    "opted_out",
    "lead_closed",
    "channel_unavailable",
)
INDEX = "ix_follow_ups_sequence_run_id"
CHECKS = {
    "ck_follow_ups_origin_known": "origin IN ('one_off', 'sequence')",
    "ck_follow_ups_sequence_step_complete": (
        "(origin = 'sequence') = (sequence_run_id IS NOT NULL"
        " AND step_index IS NOT NULL AND anchor_at IS NOT NULL)"
    ),
    "ck_follow_ups_skip_reason_known": (
        "skip_reason IS NULL OR skip_reason IN ("
        + ", ".join(f"'{reason}'" for reason in SKIP_REASONS)
        + ")"
    ),
}
RUN_FK = "fk_follow_ups_tenant_sequence_run"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


def upgrade() -> None:
    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        "follow_up_sequence_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("end_reason", sa.String(40), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("steps_sent", sa.Integer(), nullable=False),
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
        sa.CheckConstraint(
            "state IN ('active', 'ended')", name=op.f("ck_follow_up_sequence_runs_state_known")
        ),
        sa.CheckConstraint(
            f"end_reason IS NULL OR end_reason IN ({_in(END_REASONS)})",
            name=op.f("ck_follow_up_sequence_runs_end_reason_known"),
        ),
        sa.CheckConstraint(
            "(state = 'active') = (end_reason IS NULL AND ended_at IS NULL)",
            name=op.f("ck_follow_up_sequence_runs_ended_has_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_follow_up_sequence_runs_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_follow_up_sequence_runs_tenant_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_follow_up_sequence_runs")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_follow_up_sequence_runs_tenant_id_id"),
    )
    op.create_index(
        "ix_follow_up_sequence_runs_tenant_id", "follow_up_sequence_runs", ["tenant_id"]
    )
    op.create_index(
        "ix_follow_up_sequence_runs_conversation_id",
        "follow_up_sequence_runs",
        ["conversation_id"],
    )
    op.create_index(
        "uq_follow_up_sequence_runs_active_conversation",
        "follow_up_sequence_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )

    op.add_column(
        "follow_ups",
        sa.Column("origin", sa.String(16), nullable=False, server_default=sa.text("'one_off'")),
    )
    op.add_column("follow_ups", sa.Column("sequence_run_id", sa.UUID(), nullable=True))
    op.add_column("follow_ups", sa.Column("step_index", sa.Integer(), nullable=True))
    op.add_column("follow_ups", sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("follow_ups", sa.Column("skip_reason", sa.String(32), nullable=True))
    for name, condition in CHECKS.items():
        op.execute(f"ALTER TABLE follow_ups ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID")
    op.execute(
        f"ALTER TABLE follow_ups ADD CONSTRAINT {RUN_FK} FOREIGN KEY (tenant_id, sequence_run_id)"
        " REFERENCES follow_up_sequence_runs (tenant_id, id) ON DELETE CASCADE NOT VALID"
    )
    for name in CHECKS:
        op.execute(f"ALTER TABLE follow_ups VALIDATE CONSTRAINT {name}")
    op.execute(f"ALTER TABLE follow_ups VALIDATE CONSTRAINT {RUN_FK}")
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
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON follow_ups (sequence_run_id)"
            " WHERE sequence_run_id IS NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    runs = bind.exec_driver_sql("SELECT count(*) FROM follow_up_sequence_runs").scalar_one()
    if runs:
        found.append(f"follow-up sequence runs: {runs}")
    steps = bind.exec_driver_sql(
        "SELECT count(*) FROM follow_ups WHERE origin <> 'one_off' OR skip_reason IS NOT NULL"
    ).scalar_one()
    if steps:
        found.append(f"follow-ups that are sequence steps or record a skip reason: {steps}")
    if found:
        raise RuntimeError(
            f"Follow-up sequences cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"ALTER TABLE follow_ups DROP CONSTRAINT IF EXISTS {RUN_FK}")
    for name in CHECKS:
        op.execute(f"ALTER TABLE follow_ups DROP CONSTRAINT IF EXISTS {name}")
    for column in ("skip_reason", "anchor_at", "step_index", "sequence_run_id", "origin"):
        op.drop_column("follow_ups", column)
    op.drop_table("follow_up_sequence_runs")
