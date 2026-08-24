"""create password reset tokens and their audit action

ADR-042. The reset flow docs/SECURITY.md deferred until the repository could
send email. Only the SHA-256 of a token is stored - the raw value exists
solely in the emailed link - and a token dies three ways: it expires, it is
consumed by exactly one confirmation, or it is superseded by a newer request
or a completed reset.

The audit vocabulary gains `password_reset_completed`. Only completion is
recorded: the request is unauthenticated, so auditing it would let anyone
write into a stranger's trail.

Downgrade note: as with 0025, PostgreSQL cannot remove a value from an enum
type, so the label stays behind rather than rewriting every audit row.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id",
        "password_reset_tokens",
        ["user_id"],
    )

    # `ADD VALUE` cannot run inside the migration's transaction; `IF NOT
    # EXISTS` makes it re-runnable after a partial failure (see 0025).
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'password_reset_completed'"
        )


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    # The enum label stays behind. See the module docstring.
