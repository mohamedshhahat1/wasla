"""Marketing consent per channel.

Revision ID: 0097
Revises: 0096

ADR-131 (ENT-19); supersedes ADR-122 decision 3.

1. **`contact_channel_consents`** - one row per contact and channel: when the
   person opted out there, who decided and by which evidence, and the last
   re-admission there with its provenance. Keyed `(tenant_id, contact_id,
   channel)`, the contact reached through the workspace-agreed key
   `contacts (tenant_id, id)`.
2. **Data step**: every person-level opt-out or resume on `contacts` moves to
   that contact's WhatsApp row - the only channel a customer could have opted
   out on before this revision. Refused, changing nothing, if a contact holds
   an opt-out with no source: the consent row cannot record a refusal without
   saying who made it, and inventing one would falsify the record.
3. **`contacts` drops** `marketing_opt_out_at`, `opt_out_source`, `opt_out_via`
   and `marketing_resumed_at`. One opt-out system, not two: a person-level
   column left behind could only be read by mistake (M-E28).

**Online.** A new table; one `INSERT ... SELECT` over the contacts that ever
opted out or resumed; four `DROP COLUMN`s, which are catalogue changes and
rewrite nothing. `lock_timeout` is bounded.

**Downgrade refuses** while any consent is on a channel other than WhatsApp
(the person-level columns cannot say which channel), or carries a resume's
provenance (they have no column for it): it would lose data. Otherwise it
restores the four columns from the WhatsApp rows and drops the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"
TABLE = "contact_channel_consents"
CONTACT_COLUMNS = ("marketing_opt_out_at", "opt_out_source", "opt_out_via", "marketing_resumed_at")
RUNBOOK = "docs/RUNBOOK.md, 'Entitlements and channel capacity (0092-0098)'"


def upgrade() -> None:
    bind = op.get_bind()
    sourceless = bind.exec_driver_sql(
        "SELECT count(*) FROM contacts"
        " WHERE marketing_opt_out_at IS NOT NULL AND opt_out_source IS NULL"
    ).scalar_one()
    if sourceless:
        raise RuntimeError(
            f"Per-channel consent cannot be adopted ({RUNBOOK}). Nothing has been changed:\n"
            f"  contacts opted out with no recorded source: {sourceless}"
        )

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    source = postgresql.ENUM(name="opt_out_source", create_type=False)
    via = postgresql.ENUM(name="opt_out_via", create_type=False)
    op.create_table(
        TABLE,
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel", postgresql.ENUM(name="channel_kind", create_type=False), nullable=False
        ),
        sa.Column("marketing_opt_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out_source", source, nullable=True),
        sa.Column("opt_out_via", via, nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resume_source", source, nullable=True),
        sa.Column("resume_via", via, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(marketing_opt_out_at IS NULL) = (opt_out_source IS NULL)",
            name=op.f("ck_contact_channel_consents_opt_out_has_source"),
        ),
        sa.CheckConstraint(
            "opt_out_via IS NULL OR marketing_opt_out_at IS NOT NULL",
            name=op.f("ck_contact_channel_consents_via_needs_opt_out"),
        ),
        sa.CheckConstraint(
            "(resume_source IS NULL AND resume_via IS NULL) OR resumed_at IS NOT NULL",
            name=op.f("ck_contact_channel_consents_resume_provenance_needs_resume"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_contact_channel_consents_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["contacts.tenant_id", "contacts.id"],
            name="fk_contact_channel_consents_tenant_contact",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "contact_id", "channel", name="pk_contact_channel_consents"
        ),
    )
    op.create_index("ix_contact_channel_consents_tenant_id", TABLE, ["tenant_id"])

    # Every opt-out and resume recorded so far was recorded on WhatsApp.
    op.execute(
        f"INSERT INTO {TABLE} (tenant_id, contact_id, channel, marketing_opt_out_at,"
        " opt_out_source, opt_out_via, resumed_at)"
        " SELECT tenant_id, id, 'whatsapp', marketing_opt_out_at, opt_out_source,"
        " CASE WHEN marketing_opt_out_at IS NULL THEN NULL ELSE opt_out_via END,"
        " marketing_resumed_at"
        " FROM contacts"
        " WHERE marketing_opt_out_at IS NOT NULL OR marketing_resumed_at IS NOT NULL"
    )
    for column in CONTACT_COLUMNS:
        op.drop_column("contacts", column)


def downgrade() -> None:
    bind = op.get_bind()
    found = []
    other = bind.exec_driver_sql(
        f"SELECT count(*) FROM {TABLE} WHERE channel <> 'whatsapp'"  # noqa: S608 - constant
    ).scalar_one()
    if other:
        found.append(f"consents on a channel other than WhatsApp: {other}")
    provenance = bind.exec_driver_sql(
        f"SELECT count(*) FROM {TABLE}"  # noqa: S608 - constant
        " WHERE resume_source IS NOT NULL OR resume_via IS NOT NULL"
    ).scalar_one()
    if provenance:
        found.append(f"consents recording who resumed them: {provenance}")
    if found:
        raise RuntimeError(
            f"Per-channel consent cannot be downgraded ({RUNBOOK}). "
            "Nothing has been changed:\n  " + "\n  ".join(found)
        )

    op.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    op.add_column(
        "contacts", sa.Column("marketing_opt_out_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "contacts",
        sa.Column(
            "opt_out_source",
            postgresql.ENUM(name="opt_out_source", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "contacts",
        sa.Column(
            "opt_out_via", postgresql.ENUM(name="opt_out_via", create_type=False), nullable=True
        ),
    )
    op.add_column(
        "contacts", sa.Column("marketing_resumed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE contacts c SET marketing_opt_out_at = k.marketing_opt_out_at,"
        " opt_out_source = k.opt_out_source, opt_out_via = k.opt_out_via,"
        " marketing_resumed_at = k.resumed_at"
        f" FROM {TABLE} k"
        " WHERE k.tenant_id = c.tenant_id AND k.contact_id = c.id AND k.channel = 'whatsapp'"
    )
    op.drop_table(TABLE)
