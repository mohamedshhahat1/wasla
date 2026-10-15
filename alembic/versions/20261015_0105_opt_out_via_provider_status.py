"""An opt-out recorded from a provider's later `failed` status.

Revision ID: 0105
Revises: 0104

CMP-10 (ADR-142). Meta can accept a marketing message and later report it
`failed` with 131050 - the person stopped marketing messages. That is consent
evidence, recorded through `record_opt_out` like every other opt-out, and its
provenance is new: `opt_out_via` gains `provider_status`.

**Online.** `ALTER TYPE ... ADD VALUE` in an autocommit block, as every label
is (0099's pattern): a label cannot be used in the transaction that adds it.

**Downgrade refuses** while any consent row carries the label, and otherwise
leaves it: PostgreSQL cannot drop an enum label, and an unused one is inert.
"""

from __future__ import annotations

from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

LABEL = "provider_status"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE opt_out_via ADD VALUE IF NOT EXISTS '{LABEL}'")


def downgrade() -> None:
    used = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT count(*) FROM contact_channel_consents"  # noqa: S608 - module constant
            f" WHERE opt_out_via::text = '{LABEL}'"
        )
        .scalar_one()
    )
    if used:
        raise RuntimeError(
            "Opt-outs recorded from a provider status cannot be downgraded "
            "(docs/RUNBOOK.md, 'Campaigns remediation (0101-0106)'). "
            f"Nothing has been changed: contact_channel_consents via provider_status: {used}"
        )
