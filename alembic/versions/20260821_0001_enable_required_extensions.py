"""enable required postgresql extensions

pgcrypto provides gen_random_uuid() for database-side identifier defaults.
vector (pgvector) is required by the knowledge base embeddings in Phase 6 and
is enabled now so every environment is provisioned identically from the start.

Revision ID: 0001
Revises:
Create Date: 2026-08-21

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "vector"')


def downgrade() -> None:
    op.execute('DROP EXTENSION IF EXISTS "vector"')
    op.execute('DROP EXTENSION IF EXISTS "pgcrypto"')
