"""A member-given name on a contact, and the history of its marketing consent.

Revision ID: 0140
Revises: 0139

FE-5, FEB-19 (CD-02, CD-06). Three things, because the code proved each missing:

- `contacts.name` - the name a member gave a customer; nullable, no default,
  NULL is "no name given". `display_name` stays the channel's own name, kept
  current by the webhook; nothing automatic writes `name`.
- `contact_consent_events` - one row per change of a person's marketing consent
  on one channel: who decided (`source`), by which evidence (`via`), which
  member recorded it (`actor_user_id`, null for the customer or the provider),
  and when. `contact_channel_consents` holds the state and overwrites it, so a
  clear forgot the stop it undid; the audit log is the administrators' record
  and is not what a member reads. Append-only, and empty at first: an opt-out
  older than this table is shown from the state's own timestamp.
- `audit_action` gains `contact_renamed`, appended in an autocommit block as
  every label is.

**Online.** A nullable column without a default is a catalog change; the table
is new and empty; `ALTER TYPE ... ADD VALUE` takes no table lock.

**Downgrade** drops the table, its enum and the column - the names members gave
and the consent history go with them, the consent *state* is untouched. The
audit label stays: PostgreSQL cannot drop one, and the downgrade refuses while
an audit row uses it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
TABLE = "contact_consent_events"
AUDIT_LABEL = "contact_renamed"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{AUDIT_LABEL}'")

    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column("contacts", sa.Column("name", sa.String(length=200), nullable=True))

    action = postgresql.ENUM("opted_out", "opt_out_cleared", name="consent_action")
    action.create(op.get_bind(), checkfirst=True)
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel", postgresql.ENUM(name="channel_kind", create_type=False), nullable=False
        ),
        sa.Column(
            "action", postgresql.ENUM(name="consent_action", create_type=False), nullable=False
        ),
        sa.Column(
            "source", postgresql.ENUM(name="opt_out_source", create_type=False), nullable=False
        ),
        sa.Column("via", postgresql.ENUM(name="opt_out_via", create_type=False), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_consent_events")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_contact_consent_events_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_contact_consent_events_tenant_contact",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_contact_consent_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_contact_consent_events_tenant_id", TABLE, ["tenant_id"])
    op.create_index(
        "ix_contact_consent_events_contact",
        TABLE,
        ["tenant_id", "contact_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.exec_driver_sql(
        f"SELECT count(*) FROM audit_logs WHERE action::text = '{AUDIT_LABEL}'"  # noqa: S608
    ).scalar_one()
    if used:
        raise RuntimeError(
            f"Refusing to downgrade 0140: {used} audit rows record a contact renamed."
        )
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.drop_index("ix_contact_consent_events_contact", table_name=TABLE)
    op.drop_index("ix_contact_consent_events_tenant_id", table_name=TABLE)
    op.drop_table(TABLE)
    postgresql.ENUM(name="consent_action").drop(bind, checkfirst=True)
    op.drop_column("contacts", "name")
