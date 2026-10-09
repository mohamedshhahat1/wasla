"""Withdrawing a platform grant before it ends.

Revision ID: 0099
Revises: 0098

ADR-132 (PLAT-G1).

1. **Labels**, appended in an autocommit block first, as every label is:
   `topup_status` gains `withdrawn`, `channel_capacity_reduction_cause` gains
   `grant_withdrawn`, `audit_action` gains `billing_topup_grant_withdrawn`.
2. **`topup_purchases`** gains `withdrawn_at`, `withdrawn_by` (a user, SET
   NULL) and `withdrawal_reason`, all nullable - metadata only. Two CHECKs: only
   a platform grant is ever `withdrawn`, and a withdrawn one says when and why.
   Neither column is part of the frozen snapshot; the snapshot trigger keeps
   the key, quantity and channel exactly as they were.
3. **`channel_capacity_reductions`** gains `topup_purchase_id`, the grant whose
   withdrawal opened it, and a CHECK that exactly the `grant_withdrawn` cause
   names one (E18).

**Online.** No table rewrite. The constraints are added `NOT VALID` and then
validated, which holds SHARE UPDATE EXCLUSIVE on two small tables; every
statement waits at most 15 s for its lock.

**Downgrade** refuses while any grant is withdrawn, any reduction names one, or
any audit entry is a withdrawal: the pre-0099 schema cannot say why a grant
stopped counting. Otherwise it drops what this revision added, in the run's
own transaction, so a refusal below 0099 rolls the whole run back (MIG-0091). The enum labels stay; PostgreSQL cannot
drop one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
MAX_REASON = 500

LABELS = (
    ("topup_status", "withdrawn"),
    ("channel_capacity_reduction_cause", "grant_withdrawn"),
    ("audit_action", "billing_topup_grant_withdrawn"),
)
PURCHASE_CHECKS = (
    (
        "ck_topup_purchases_withdrawn_is_a_grant",
        "status <> 'withdrawn' OR source = 'platform_grant'",
    ),
    (
        "ck_topup_purchases_withdrawal_recorded",
        "(status = 'withdrawn') = (withdrawn_at IS NOT NULL AND withdrawal_reason IS NOT NULL)",
    ),
)
REDUCTION_CHECK = (
    "ck_channel_capacity_reductions_withdrawn_grant_named",
    "(cause = 'grant_withdrawn') = (topup_purchase_id IS NOT NULL)",
)
WITHDRAWN_BY_FK = "fk_topup_purchases_withdrawn_by_users"
REDUCTION_FK = "fk_channel_capacity_reductions_topup_purchase"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for type_name, label in LABELS:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{label}'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "topup_purchases", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "topup_purchases",
        sa.Column("withdrawn_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "topup_purchases", sa.Column("withdrawal_reason", sa.String(MAX_REASON), nullable=True)
    )
    op.execute(
        f"ALTER TABLE topup_purchases ADD CONSTRAINT {WITHDRAWN_BY_FK} FOREIGN KEY"
        " (withdrawn_by) REFERENCES users (id) ON DELETE SET NULL NOT VALID"
    )
    for name, condition in PURCHASE_CHECKS:
        op.execute(
            f"ALTER TABLE topup_purchases ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID"
        )

    op.add_column(
        "channel_capacity_reductions",
        sa.Column("topup_purchase_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        f"ALTER TABLE channel_capacity_reductions ADD CONSTRAINT {REDUCTION_FK} FOREIGN KEY"
        " (topup_purchase_id) REFERENCES topup_purchases (id) ON DELETE RESTRICT NOT VALID"
    )
    op.execute(
        f"ALTER TABLE channel_capacity_reductions ADD CONSTRAINT {REDUCTION_CHECK[0]}"
        f" CHECK ({REDUCTION_CHECK[1]}) NOT VALID"
    )

    op.execute(f"ALTER TABLE topup_purchases VALIDATE CONSTRAINT {WITHDRAWN_BY_FK}")
    for name, _condition in PURCHASE_CHECKS:
        op.execute(f"ALTER TABLE topup_purchases VALIDATE CONSTRAINT {name}")
    op.execute(f"ALTER TABLE channel_capacity_reductions VALIDATE CONSTRAINT {REDUCTION_FK}")
    op.execute(f"ALTER TABLE channel_capacity_reductions VALIDATE CONSTRAINT {REDUCTION_CHECK[0]}")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    withdrawn = bind.exec_driver_sql(
        "SELECT count(*) FROM topup_purchases"
        " WHERE status::text = 'withdrawn' OR withdrawn_at IS NOT NULL"
    ).scalar_one()
    if withdrawn:
        found.append(f"topup_purchases withdrawn by staff: {withdrawn}")
    reductions = bind.exec_driver_sql(
        "SELECT count(*) FROM channel_capacity_reductions"
        " WHERE cause::text = 'grant_withdrawn' OR topup_purchase_id IS NOT NULL"
    ).scalar_one()
    if reductions:
        found.append(f"channel_capacity_reductions opened by a withdrawn grant: {reductions}")
    audited = bind.exec_driver_sql(
        "SELECT count(*) FROM audit_logs WHERE action::text = 'billing_topup_grant_withdrawn'"
    ).scalar_one()
    if audited:
        found.append(f"audit_logs recording a grant withdrawal: {audited}")
    if found:
        raise RuntimeError(
            "Grant withdrawals cannot be downgraded "
            "(docs/RUNBOOK.md, 'Platform entitlement operations (0099-0100)'). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(
        f"ALTER TABLE channel_capacity_reductions DROP CONSTRAINT IF EXISTS {REDUCTION_CHECK[0]}"
    )
    op.execute(f"ALTER TABLE channel_capacity_reductions DROP CONSTRAINT IF EXISTS {REDUCTION_FK}")
    op.drop_column("channel_capacity_reductions", "topup_purchase_id")
    for name, _condition in reversed(PURCHASE_CHECKS):
        op.execute(f"ALTER TABLE topup_purchases DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE topup_purchases DROP CONSTRAINT IF EXISTS {WITHDRAWN_BY_FK}")
    for column in ("withdrawal_reason", "withdrawn_by", "withdrawn_at"):
        op.drop_column("topup_purchases", column)
