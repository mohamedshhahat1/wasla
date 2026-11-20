"""`audit_action` gains `member_role_changed`.

Revision ID: 0142
Revises: 0141

FE-8, FEB-42 (MG8-01). Changing a member's role between member and
administrator is a new administrative act, and none of the existing labels
describes it truthfully: `member_reinstated` is a return after removal and
`workspace_ownership_transferred` is the hand-over of ownership. The label is
appended in an autocommit block, as every label is.

**Online.** `ALTER TYPE ... ADD VALUE` takes no table lock.

**Downgrade** leaves the label: PostgreSQL cannot drop one, and the downgrade
refuses while an audit row uses it.
"""

from __future__ import annotations

from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None

AUDIT_LABEL = "member_role_changed"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{AUDIT_LABEL}'")


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text = '{AUDIT_LABEL}'"  # noqa: S608
    ).scalar_one()
    if used:
        raise RuntimeError(
            f"Refusing to downgrade 0142: {used} audit rows record a member's role change."
        )
