"""Campaign test sends.

Revision ID: 0117
Revises: 0116

PG-09 (ADR-149).

1. `message_origin` gains `campaign_test`: an admin's test of a campaign,
   sent to their own contact.
2. `campaign_test_sends`: one row per test - the campaign, the contact, the
   message, who sent it - from which a workspace's daily allowance is
   counted; tenant-composite keys to the campaign and the contact (ADR-100),
   cascading with them; the message and the sender set null.

**Online.** The enum value in an autocommit block; a new, empty table and its
indexes under a bounded `lock_timeout`.

**Downgrade refuses** while any test send or `campaign_test` message exists.
The enum value itself stays: PostgreSQL cannot drop one, and an unused value
is harmless.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
TABLE = "campaign_test_sends"
RUNBOOK = "docs/RUNBOOK.md, 'Campaign page features (0108-0113)'"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE message_origin ADD VALUE IF NOT EXISTS 'campaign_test'")

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.create_table(
        TABLE,
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sent_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_campaign_test_sends"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_campaign_test_sends_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_campaign_test_sends_tenant_campaign",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_campaign_test_sends_tenant_contact",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_campaign_test_sends_message_id_messages",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sent_by_id"],
            ["users.id"],
            name="fk_campaign_test_sends_sent_by_id_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_campaign_test_sends_tenant_id_id"),
    )
    op.create_index(
        "ix_campaign_test_sends_tenant_id_created_at", TABLE, ["tenant_id", "created_at"]
    )
    op.create_index("ix_campaign_test_sends_campaign_id", TABLE, ["campaign_id"])
    op.create_index(
        "ix_campaign_test_sends_message_id",
        TABLE,
        ["message_id"],
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    tests = bind.exec_driver_sql(f"SELECT count(*) FROM {TABLE}").scalar_one()  # noqa: S608
    if tests:
        found.append(f"campaign test sends: {tests}")
    messages = bind.exec_driver_sql(
        "SELECT count(*) FROM messages WHERE origin::text = 'campaign_test'"
    ).scalar_one()
    if messages:
        found.append(f"messages sent as campaign tests: {messages}")
    if found:
        raise RuntimeError(
            f"Campaign test sends cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_table(TABLE)
