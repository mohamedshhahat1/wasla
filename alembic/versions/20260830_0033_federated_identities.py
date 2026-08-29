"""Federated authentication identities.

Revision ID: 0030
Revises: 0029

One table and one enum type.

The two unique constraints are the reason this migration matters more than its
size suggests. `(provider, provider_subject)` is what makes "one Google account
cannot become two Wasla accounts" a property of the database, so the concurrent
first-login race is resolved by PostgreSQL rather than by hoping the service
serialises. `(user_id, provider)` bounds an account to one identity per issuer,
and its index is also the one that answers "is Google attached to this user?" -
which is why no separate index on `user_id` is created here.

The foreign key is named explicitly. `app/db/base.py` declares a naming
convention, and Alembic applies it to `op.create_table` through the target
metadata - but an explicit name that matches the convention is identical when
that works and correct when it does not, which is a strictly better trade for
one string.

The enum type is dropped on downgrade, unlike `audit_action` in 0029. It is
used by exactly one table and that table is being dropped in the same
function, so nothing is left depending on it and the downgrade/upgrade cycle
returns to a genuinely clean schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

# `create_type=False` so that referencing this in the column definition below
# does not try to create the type a second time while the table is being built.
# It is created once, explicitly, above the table.
_PROVIDER = postgresql.ENUM("google", name="identity_provider", create_type=False)


def upgrade() -> None:
    _PROVIDER.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "user_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                ondelete="CASCADE",
                name="fk_user_identities_user_id_users",
            ),
            nullable=False,
        ),
        sa.Column("provider", _PROVIDER, nullable=False),
        sa.Column("provider_subject", sa.String(length=255), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_user_identities_provider_subject",
        ),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            name="uq_user_identities_user_id_provider",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_identities")
    _PROVIDER.drop(op.get_bind(), checkfirst=True)
