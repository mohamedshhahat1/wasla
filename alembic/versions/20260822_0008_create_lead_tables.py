"""create lead, lead note and lead activity tables

Revision ID: 0008
Revises: 0007

The one thing here that is not a plain table is the partial unique index on
``leads``. It is what makes "one open opportunity per customer" a database
guarantee rather than a service convention, and it has to be partial: a closed
lead must not occupy the slot, or a returning customer could never be recorded
again, and leads entered by hand carry no contact at all and would otherwise
collide with each other on a null.

``ondelete`` differs by relationship on purpose. Notes and activities are parts
of a lead and cascade with it. A lead's contact and conversation are its origin,
so deleting either nulls the reference and leaves the lead - losing the record
of an opportunity because a conversation was tidied away would be a data loss
the business notices.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

LEAD_STATUS = postgresql.ENUM(
    "new",
    "contacted",
    "qualified",
    "proposal",
    "won",
    "lost",
    name="lead_status",
    create_type=False,
)
LEAD_SOURCE = postgresql.ENUM(
    "whatsapp",
    "agent",
    "manual",
    "import",
    name="lead_source",
    create_type=False,
)
ACTOR_KIND = postgresql.ENUM(
    "user",
    "agent",
    "system",
    name="actor_kind",
    create_type=False,
)
LEAD_ACTIVITY_KIND = postgresql.ENUM(
    "created",
    "status_changed",
    "assigned",
    "unassigned",
    "fields_updated",
    "note_added",
    "score_changed",
    name="lead_activity_kind",
    create_type=False,
)
ENUM_TYPES = (LEAD_STATUS, LEAD_SOURCE, ACTOR_KIND, LEAD_ACTIVITY_KIND)

# Matches the expression declared on the model. Both sides have to agree, or
# ``alembic check`` reports drift on every run.
ACTIVE_LEAD_PREDICATE = "contact_id IS NOT NULL AND status <> 'won' AND status <> 'lost'"


def _audit_columns():
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade():
    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.create(bind, checkfirst=False)

    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("interest", sa.String(length=500), nullable=True),
        sa.Column("budget_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("budget_currency", sa.String(length=3), nullable=True),
        sa.Column("status", LEAD_STATUS, nullable=False),
        sa.Column("source", LEAD_SOURCE, nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("assigned_to_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.String(length=50)), nullable=False),
        sa.Column("custom_fields", postgresql.JSONB(), nullable=False),
        sa.Column(
            "human_verified_fields",
            postgresql.ARRAY(sa.String(length=50)),
            nullable=False,
        ),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_leads"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_leads_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_leads_contact_id_contacts",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_leads_conversation_id_conversations",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assigned_to_id"],
            ["users.id"],
            name="fk_leads_assigned_to_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_leads_tenant_id", "leads", ["tenant_id"])
    op.create_index("ix_leads_tenant_id_status", "leads", ["tenant_id", "status"])
    op.create_index(
        "ix_leads_tenant_id_assigned_to_id",
        "leads",
        ["tenant_id", "assigned_to_id"],
    )
    op.create_index("ix_leads_tenant_id_created_at", "leads", ["tenant_id", "created_at"])
    op.create_index("ix_leads_contact_id", "leads", ["contact_id"])
    op.create_index("ix_leads_conversation_id", "leads", ["conversation_id"])
    op.create_index("ix_leads_tags", "leads", ["tags"], postgresql_using="gin")
    op.create_index(
        "uq_leads_active_contact",
        "leads",
        ["tenant_id", "contact_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_LEAD_PREDICATE),
    )

    op.create_table(
        "lead_notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_kind", ACTOR_KIND, nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_lead_notes"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_lead_notes_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name="fk_lead_notes_lead_id_leads",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name="fk_lead_notes_author_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_lead_notes_tenant_id", "lead_notes", ["tenant_id"])
    op.create_index(
        "ix_lead_notes_lead_id_created_at",
        "lead_notes",
        ["lead_id", "created_at"],
    )

    op.create_table(
        "lead_activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", LEAD_ACTIVITY_KIND, nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_kind", ACTOR_KIND, nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id", name="pk_lead_activities"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_lead_activities_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name="fk_lead_activities_lead_id_leads",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_lead_activities_actor_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_lead_activities_tenant_id", "lead_activities", ["tenant_id"])
    op.create_index(
        "ix_lead_activities_lead_id_created_at",
        "lead_activities",
        ["lead_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_lead_activities_lead_id_created_at", table_name="lead_activities")
    op.drop_index("ix_lead_activities_tenant_id", table_name="lead_activities")
    op.drop_table("lead_activities")

    op.drop_index("ix_lead_notes_lead_id_created_at", table_name="lead_notes")
    op.drop_index("ix_lead_notes_tenant_id", table_name="lead_notes")
    op.drop_table("lead_notes")

    op.drop_index("uq_leads_active_contact", table_name="leads")
    op.drop_index("ix_leads_tags", table_name="leads")
    op.drop_index("ix_leads_conversation_id", table_name="leads")
    op.drop_index("ix_leads_contact_id", table_name="leads")
    op.drop_index("ix_leads_tenant_id_created_at", table_name="leads")
    op.drop_index("ix_leads_tenant_id_assigned_to_id", table_name="leads")
    op.drop_index("ix_leads_tenant_id_status", table_name="leads")
    op.drop_index("ix_leads_tenant_id", table_name="leads")
    op.drop_table("leads")

    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.drop(bind, checkfirst=False)
