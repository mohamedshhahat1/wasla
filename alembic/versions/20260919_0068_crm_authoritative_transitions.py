"""make CRM relations and transitions authoritative under concurrency

Revision ID: 0068
Revises: 0067

The CRM/handoff audit (crm-c4h7) found that the CRM record was a set of
unlocked read-modify-writes with no state precondition, and that one relation
the API writes - ``follow_ups.lead_id`` - accepted another workspace's lead
(CRM-01). Four schema changes carry that remediation; everything else it needed
is application code.

**Tenant- and customer-agreed CRM relations (CRM-01, CRM-14, ADR-100).**
ADR-100 left the CRM relations as plain keys on the premise that "no API path
builds such a row". CRM-01 falsified that premise for ``follow_ups.lead_id``,
so the relations a CRM write can name are now composite:

    follow_ups       (tenant_id, lead_id)          -> leads         SET NULL (lead_id)
    follow_ups       (tenant_id, conversation_id)  -> conversations CASCADE
    leads            (tenant_id, contact_id)       -> contacts      SET NULL (contact_id)
    leads            (tenant_id, conversation_id, contact_id)
                                                   -> conversations(tenant_id, id, contact_id)
                                                                    SET NULL (conversation_id)
    lead_notes       (tenant_id, lead_id)          -> leads         CASCADE
    lead_activities  (tenant_id, lead_id)          -> leads         CASCADE

The ``SET NULL (column)`` form (PostgreSQL 15+) is what keeps these nullable
references nullable without nulling ``tenant_id`` alongside them. The
three-column lead key makes "a lead's conversation is with that lead's
customer" a database fact whenever both are set; a lead with only one of them
is unconstrained by it, which is the ordinary manual lead.

A follow-up's lead-and-conversation agreement is *not* encoded here: it would
mean copying the contact onto every follow-up. The service enforces it, and the
invariant sweep checks it.

**An explicit follow-up claim (CRM-09, CRM-10).** The sweep used to lease a row
by pushing its ``scheduled_at`` forward, so a colleague's reschedule inside the
lease was indistinguishable from the lease and was overwritten by it. The claim
is now its own pair of columns; ``scheduled_at`` is only ever what somebody
asked for.

**Ownership audit vocabulary (CRM-05).** Seven ``audit_action`` labels.

**Event-time activity timestamps (CRM-16).** ``lead_activities.created_at``
defaults to ``clock_timestamp()`` rather than ``now()``, so the timeline orders
by when a change was written, not when its transaction began.

**A mismatch stops the deployment.** Existing rows that a new key would refuse
are named first and the migration fails without changing anything, for the
reason 0053 gives: a crossed row is evidence, and a migration is the wrong
place to decide what it was. ``docs/RUNBOOK.md`` has the queries.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

NEW_ACTIONS = (
    "conversation_taken_over",
    "conversation_released_to_ai",
    "conversation_assigned",
    "conversation_reassigned",
    "conversation_unassigned",
    "conversation_closed",
    "conversation_reopened",
)

# The single-column keys the composites supersede, restored by the downgrade.
# child table, constraint name, child column, parent table, on delete
SINGLE_KEYS = [
    ("follow_ups", "fk_follow_ups_lead_id_leads", "lead_id", "leads", "SET NULL"),
    (
        "follow_ups",
        "fk_follow_ups_conversation_id_conversations",
        "conversation_id",
        "conversations",
        "CASCADE",
    ),
    ("leads", "fk_leads_contact_id_contacts", "contact_id", "contacts", "SET NULL"),
    (
        "leads",
        "fk_leads_conversation_id_conversations",
        "conversation_id",
        "conversations",
        "SET NULL",
    ),
    ("lead_notes", "fk_lead_notes_lead_id_leads", "lead_id", "leads", "CASCADE"),
    ("lead_activities", "fk_lead_activities_lead_id_leads", "lead_id", "leads", "CASCADE"),
]

# The rows each new key would refuse, named so an operator knows which.
PRECHECKS = [
    (
        "follow_ups.lead_id in another workspace",
        "SELECT count(*) FROM follow_ups f JOIN leads l ON l.id = f.lead_id "
        "WHERE l.tenant_id <> f.tenant_id",
    ),
    (
        "follow_ups.conversation_id in another workspace",
        "SELECT count(*) FROM follow_ups f JOIN conversations c ON c.id = f.conversation_id "
        "WHERE c.tenant_id <> f.tenant_id",
    ),
    (
        "leads.contact_id in another workspace",
        "SELECT count(*) FROM leads l JOIN contacts c ON c.id = l.contact_id "
        "WHERE c.tenant_id <> l.tenant_id",
    ),
    (
        "leads.conversation_id in another workspace",
        "SELECT count(*) FROM leads l JOIN conversations c ON c.id = l.conversation_id "
        "WHERE c.tenant_id <> l.tenant_id",
    ),
    (
        "leads whose conversation is with a different customer",
        "SELECT count(*) FROM leads l JOIN conversations c ON c.id = l.conversation_id "
        "WHERE l.contact_id IS NOT NULL AND c.contact_id <> l.contact_id",
    ),
    (
        "lead_notes on another workspace's lead",
        "SELECT count(*) FROM lead_notes n JOIN leads l ON l.id = n.lead_id "
        "WHERE l.tenant_id <> n.tenant_id",
    ),
    (
        "lead_activities on another workspace's lead",
        "SELECT count(*) FROM lead_activities a JOIN leads l ON l.id = a.lead_id "
        "WHERE l.tenant_id <> a.tenant_id",
    ),
]


def _refuse_existing_mismatches() -> None:
    connection = op.get_bind()
    crossed = []
    for label, query in PRECHECKS:
        count = connection.exec_driver_sql(query).scalar_one()
        if count:
            crossed.append(f"{label}: {count} row(s)")
    if crossed:
        raise RuntimeError(
            "CRM rows exist that the new relational keys would refuse. They must be "
            "investigated before this migration can run (docs/RUNBOOK.md, "
            "'CRM relational invariants'). Nothing has been changed:\n  " + "\n  ".join(crossed)
        )


def upgrade() -> None:
    _refuse_existing_mismatches()

    op.create_unique_constraint("uq_leads_tenant_id_id", "leads", ["tenant_id", "id"])
    op.create_unique_constraint(
        "uq_conversations_tenant_id_id_contact_id",
        "conversations",
        ["tenant_id", "id", "contact_id"],
    )

    op.create_foreign_key(
        "fk_follow_ups_tenant_lead",
        "follow_ups",
        "leads",
        ["tenant_id", "lead_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL (lead_id)",
    )
    op.create_foreign_key(
        "fk_follow_ups_tenant_conversation",
        "follow_ups",
        "conversations",
        ["tenant_id", "conversation_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_leads_tenant_contact",
        "leads",
        "contacts",
        ["tenant_id", "contact_id"],
        ["tenant_id", "id"],
        ondelete="SET NULL (contact_id)",
    )
    op.create_foreign_key(
        "fk_leads_tenant_conversation_contact",
        "leads",
        "conversations",
        ["tenant_id", "conversation_id", "contact_id"],
        ["tenant_id", "id", "contact_id"],
        ondelete="SET NULL (conversation_id)",
    )
    op.create_foreign_key(
        "fk_lead_notes_tenant_lead",
        "lead_notes",
        "leads",
        ["tenant_id", "lead_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_lead_activities_tenant_lead",
        "lead_activities",
        "leads",
        ["tenant_id", "lead_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    for child, name, _, _, _ in SINGLE_KEYS:
        op.drop_constraint(name, child, type_="foreignkey")

    op.add_column(
        "follow_ups",
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "follow_ups",
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
    )

    op.alter_column(
        "lead_activities",
        "created_at",
        server_default=sa.text("clock_timestamp()"),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )

    # Last, and outside the transaction: `ADD VALUE` cannot run inside one.
    with op.get_context().autocommit_block():
        for value in NEW_ACTIONS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Restore the 0067 schema exactly, writing no data.

    The audit labels stay, for the reason migration 0051 gives: PostgreSQL
    cannot drop an enum label, and nothing at 0067 writes them.

    A follow-up claimed at the moment of downgrade loses its claim columns, so
    its lease is forgotten and the 0067 sweep may pick it up again - which that
    sweep's own re-take and send-intent guard handle as they always did.
    """
    op.alter_column(
        "lead_activities",
        "created_at",
        server_default=sa.text("now()"),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )
    op.drop_column("follow_ups", "claimed_until")
    op.drop_column("follow_ups", "claim_token")

    for child, name, column, parent, ondelete in SINGLE_KEYS:
        op.create_foreign_key(name, child, parent, [column], ["id"], ondelete=ondelete)
    for name, child in (
        ("fk_lead_activities_tenant_lead", "lead_activities"),
        ("fk_lead_notes_tenant_lead", "lead_notes"),
        ("fk_leads_tenant_conversation_contact", "leads"),
        ("fk_leads_tenant_contact", "leads"),
        ("fk_follow_ups_tenant_conversation", "follow_ups"),
        ("fk_follow_ups_tenant_lead", "follow_ups"),
    ):
        op.drop_constraint(name, child, type_="foreignkey")
    op.drop_constraint("uq_conversations_tenant_id_id_contact_id", "conversations", type_="unique")
    op.drop_constraint("uq_leads_tenant_id_id", "leads", type_="unique")
