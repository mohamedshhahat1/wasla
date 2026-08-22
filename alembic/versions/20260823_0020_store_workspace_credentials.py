"""add the encrypted credential column to whatsapp accounts

Revision ID: 0020
Revises: 0019

One nullable column, and the reason it took until phase 14 is recorded in
ADR-009: a plaintext token column puts a live sending capability into every
backup, read replica and over-broad support query, so the column was refused
until encryption at rest and key management existed. ADR-034 supersedes it now
that they do.

`Text` rather than a bounded `String`: the value is an envelope
(`v1.<key id>.<nonce>.<ciphertext>`) whose length depends on the token and on
the scheme, and guessing a maximum here would mean a migration the first time a
provider issues a longer credential.

Nullable, and it stays nullable. A workspace without its own token sends through
the platform credential, which is how every workspace worked before this column
existed and how a new one works until it supplies one.

Nothing is backfilled. There is nothing to backfill: no token has ever been
stored, which was the entire point of ADR-009.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "whatsapp_accounts",
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
    )


def downgrade():
    # Drops the credentials with the column. That is the correct behaviour for a
    # downgrade - the ciphertext is unreadable to the older code anyway - and it
    # means a workspace must reconnect its number after one.
    op.drop_column("whatsapp_accounts", "access_token_encrypted")
