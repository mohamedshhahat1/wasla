"""index the agent turns that engaged a provider and never finished

Revision ID: 0062
Revises: 0061

A turn that engages and then fails - a provider outage past its retries, an
exception after the provider was called - stays `engaged` for ever, by design:
it may already have reached the customer, so nothing retries it. Those rows are
exactly the "did this customer get an answer?" cases an incident needs, and
nothing counted them (AI-09). `ix_agent_turns_unfinished` covers only `claimed`.

This partial index serves the stranded-turn gauge the metrics exposition reads at
every scrape. It holds only the turns engaged right now, which on a healthy
deployment is the handful in flight.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_turns_engaged",
        "agent_turns",
        ["engaged_at"],
        postgresql_where=sa.text("state = 'engaged'"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_turns_engaged", table_name="agent_turns")
