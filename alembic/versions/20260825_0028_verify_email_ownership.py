"""Record proven email ownership, and the challenges that prove it.

Revision ID: 0028
Revises: 0027

`users.email_verified_at` is nullable with no default, because NULL is the
honest state for every account that already exists: none of them has proven
anything. Backfilling them as verified would be recording a proof that never
happened, and doing it the other way - a NOT NULL column with a default - has
no correct value to pick.

The partial unique index is the interesting object here. It makes "at most one
live challenge per account" true in the database, so two racing send requests
cannot leave two valid codes outstanding no matter how the service is written
later.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "email_verification_challenges",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
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
    )

    op.create_index(
        "ix_email_verification_challenges_user_id",
        "email_verification_challenges",
        ["user_id"],
    )
    # One live challenge per account. Partial, so consumed and superseded rows
    # accumulate freely without ever colliding.
    op.create_index(
        "uq_email_verification_challenges_active",
        "email_verification_challenges",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL AND superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_email_verification_challenges_active",
        table_name="email_verification_challenges",
    )
    op.drop_index(
        "ix_email_verification_challenges_user_id",
        table_name="email_verification_challenges",
    )
    op.drop_table("email_verification_challenges")
    op.drop_column("users", "email_verified_at")
