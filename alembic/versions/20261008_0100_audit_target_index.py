"""One object's audit history, newest first, paged by its key.

Revision ID: 0100
Revises: 0099

ADR-132 (PLAT-G6). `GET /platform/audit-logs?target_type=&target_id=` reads
one top-up product's, one grant's or one plan's changes, newest first, and
pages by `(occurred_at, id)`. `ix_audit_logs_target_type_target_id_occurred_at`
on `(target_type, target_id, occurred_at, id)` serves the filter, the order
and the cursor together.

**Online.** `audit_logs` is written by every request that changes anything, so
the index is built `CONCURRENTLY` in an autocommit block; an INVALID leftover
of a failed build is dropped and rebuilt first (0084's pattern).

**Downgrade** drops it in the run's own transaction, not `CONCURRENTLY`, under
a 15 s lock_timeout (MIG-0091): a refusal below 0100 must roll this back too.
Nothing is lost.
"""

from __future__ import annotations

from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

INDEX = "ix_audit_logs_target_type_target_id_occurred_at"


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
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON audit_logs"
            " (target_type, target_id, occurred_at, id)"
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '15s'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
