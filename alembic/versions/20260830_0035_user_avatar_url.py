"""Give an account a picture.

Revision ID: 0035
Revises: 0034

One nullable column. Nullable rather than defaulted to an empty string because
"this person has no picture" is a real and common state that the interface has
to render differently, and a sentinel would make every reader remember which
falsy value meant it.

No backfill: every existing account predates Google sign-in, so there is
nothing to copy from. They acquire a picture the first time they sign in with
Google, and never otherwise.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_url", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_url")
