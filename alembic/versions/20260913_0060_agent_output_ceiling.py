"""give every agent an output ceiling the database guarantees

Revision ID: 0060
Revises: 0059

`agents.max_output_tokens` was nullable, defaulted only by `AgentService` on
create and update, and never backfilled. An agent row carrying null - every row
written before the service default existed, and any row written around the
service - made the orchestrator omit `max_output_tokens` from the provider request
altogether, so that agent's per-call output spend was whatever the provider's own
default happened to be (AI-05).

Existing nulls take 2048, the configured default for `OPENAI_MAX_OUTPUT_TOKENS`,
and the column becomes NOT NULL with that server default. The orchestrator also
clamps every request to the deployment's ceiling at run time, so a deployment
configured below 2048 is still held to its own figure; the column guarantees a
value exists, and the configuration decides how large it may be.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE agents SET max_output_tokens = 2048 WHERE max_output_tokens IS NULL")
    op.alter_column(
        "agents",
        "max_output_tokens",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("2048"),
    )


def downgrade() -> None:
    op.alter_column(
        "agents",
        "max_output_tokens",
        existing_type=sa.Integer(),
        nullable=True,
        server_default=None,
    )
