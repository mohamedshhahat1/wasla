"""add the token version column to users

Revocation (ADR-036). Every access and refresh token carries the value that was
current when it was minted, and both are checked against this column on use, so
raising it by one ends every session that person holds.

`server_default="1"` rather than a backfill: it gives every existing row the
value without a data migration, and it means a row inserted by anything that
does not know about the column - a fixture, a support script - still gets a
usable version rather than a null.

The consequence for tokens already in circulation is deliberate and worth
stating. A token minted before this migration carries no `ver` claim at all, so
the comparison in `get_current_user` fails and it stops working. Every session
open at deploy time therefore ends, and everybody signs in once. That is the
correct direction to fail: the alternative is treating an unversioned token as
current, which would leave exactly the tokens this column exists to revoke
permanently exempt from it.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
