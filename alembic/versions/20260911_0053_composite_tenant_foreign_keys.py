"""make selected parent/child tenant agreement a database fact

Revision ID: 0053
Revises: 0052

Every tenant-owned table carries ``tenant_id``, and every foreign key between
two of them references the parent's ``id`` alone. So PostgreSQL accepts a
conversation in workspace A whose contact belongs to workspace B, and the audit
demonstrated it with a direct ``INSERT`` (AUTHZ-04). Nothing in the application
builds such a row - ingestion derives every id from one resolved account inside
one tenant-scoped service, and a sweep of ten relational invariants found no
violations other than the one the audit inserted by hand - which is the reason
this is defence in depth rather than a fix for a reachable defect (ADR-100).

The distinction being closed is between "no code path does this" and "this
cannot be". The first is a property of today's call graph and has to be
re-established by every reviewer of every future writer; the second is a
property of the schema and re-establishes itself.

**Six relations, not thirty.** The schema has thirty tenant-to-tenant
parent/child relations. These six are the ones where a mismatch would be a
confidentiality event rather than an inconsistency - they carry customer
messages and the corpus a retrieval answers from:

    conversations  -> contacts            (whose conversation this is)
    conversations  -> whatsapp_accounts   (which number it arrived on)
    messages       -> conversations       (the transcript itself)
    documents      -> knowledge_bases     (the corpus a document joins)
    document_chunks-> documents           (what a retrieval actually reads)
    document_chunks-> knowledge_bases     (and how a retrieval scopes it)

The remaining twenty-four are left alone deliberately. Adding constraints
mechanically would buy indexes and write-path cost on tables like
``usage_events`` and ``analytics_events`` for relations whose worst case is a
wrong number on a dashboard. ``docs/AUTHORIZATION.md`` records which are
enforced and why the rest are not.

**The unique constraints are not redundant.** ``UNIQUE (tenant_id, id)`` cannot
fail on a table whose primary key is ``id`` - and a composite foreign key can
only reference a uniquely constrained set of columns, so the parents need one
before the children can point at it.

**A mismatch stops the deployment, and that is the intent.** ``ALTER TABLE ...
ADD FOREIGN KEY`` validates existing rows, so a database that already holds a
crossed row fails here rather than silently keeping it. The check below runs
first only to say *which* rows, because PostgreSQL's own error names the
constraint and not the data. No row is deleted or rewritten: a
cross-tenant row is evidence of something worth understanding before it is
tidied away, and a migration is the wrong place to decide what that was.

**The single-column keys are replaced, not joined.** ``(tenant_id, child_id)``
referencing ``(tenant_id, id)`` already guarantees a parent row with that id, so
keeping ``child_id -> parent.id`` beside it would be the same check paid for
twice on every insert into the two largest tables in the schema. ``ON DELETE
CASCADE`` is carried over unchanged, and the explicit indexes on the child
columns are untouched - they serve queries, not the constraint. The downgrade
puts the single-column keys back, so it restores the previous schema exactly.
"""

from __future__ import annotations

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

# parent table -> the unique constraint a composite key can reference
PARENT_KEYS = [
    ("contacts", "uq_contacts_tenant_id_id"),
    ("whatsapp_accounts", "uq_whatsapp_accounts_tenant_id_id"),
    ("conversations", "uq_conversations_tenant_id_id"),
    ("knowledge_bases", "uq_knowledge_bases_tenant_id_id"),
    ("documents", "uq_documents_tenant_id_id"),
]

# The single-column keys the composites supersede, restored by the downgrade.
# child table, constraint name, child column, parent table
SINGLE_KEYS = [
    ("conversations", "fk_conversations_contact_id_contacts", "contact_id", "contacts"),
    (
        "conversations",
        "fk_conversations_account_id_whatsapp_accounts",
        "account_id",
        "whatsapp_accounts",
    ),
    (
        "messages",
        "fk_messages_conversation_id_conversations",
        "conversation_id",
        "conversations",
    ),
    (
        "documents",
        "fk_documents_knowledge_base_id_knowledge_bases",
        "knowledge_base_id",
        "knowledge_bases",
    ),
    (
        "document_chunks",
        "fk_document_chunks_document_id_documents",
        "document_id",
        "documents",
    ),
    (
        "document_chunks",
        "fk_document_chunks_knowledge_base_id_knowledge_bases",
        "knowledge_base_id",
        "knowledge_bases",
    ),
]

# constraint name, child table, child column, parent table
COMPOSITE_KEYS = [
    ("fk_conversations_tenant_contact", "conversations", "contact_id", "contacts"),
    ("fk_conversations_tenant_account", "conversations", "account_id", "whatsapp_accounts"),
    ("fk_messages_tenant_conversation", "messages", "conversation_id", "conversations"),
    (
        "fk_documents_tenant_knowledge_base",
        "documents",
        "knowledge_base_id",
        "knowledge_bases",
    ),
    ("fk_document_chunks_tenant_document", "document_chunks", "document_id", "documents"),
    (
        "fk_document_chunks_tenant_knowledge_base",
        "document_chunks",
        "knowledge_base_id",
        "knowledge_bases",
    ),
]


def _refuse_existing_mismatches() -> None:
    """Name the crossed rows before PostgreSQL refuses them anonymously.

    Without this, a deployment holding one lands on ``insert or update on table
    "conversations" violates foreign key constraint``, which says nothing about
    which row, which workspace, or how many. An operator who has to find that
    out during a deploy window is the reader this exists for.
    """
    connection = op.get_bind()
    crossed = []
    for _, child, column, parent in COMPOSITE_KEYS:
        count = connection.exec_driver_sql(
            f"SELECT count(*) FROM {child} c "  # noqa: S608 - names are module constants
            f"JOIN {parent} p ON p.id = c.{column} "
            f"WHERE c.tenant_id <> p.tenant_id"
        ).scalar_one()
        if count:
            crossed.append(f"{child}.{column} -> {parent}: {count} row(s)")

    if crossed:
        raise RuntimeError(
            "Cross-tenant parent/child rows exist and must be investigated before "
            "this constraint can be added. Nothing has been changed:\n  " + "\n  ".join(crossed)
        )


def upgrade() -> None:
    _refuse_existing_mismatches()
    for table, name in PARENT_KEYS:
        op.create_unique_constraint(name, table, ["tenant_id", "id"])
    for name, child, column, parent in COMPOSITE_KEYS:
        op.create_foreign_key(
            name,
            child,
            parent,
            ["tenant_id", column],
            ["tenant_id", "id"],
            ondelete="CASCADE",
        )
    # Dropped last, so the stronger constraint is in place before the weaker one
    # goes and there is no instant at which the relation is unenforced.
    for child, name, _, _ in SINGLE_KEYS:
        op.drop_constraint(name, child, type_="foreignkey")


def downgrade() -> None:
    """Remove the constraints, leaving every row exactly as it is.

    Reversible in full: nothing here writes data, so going back restores the
    previous schema without an accompanying repair. The order mirrors the
    upgrade - the single-column keys come back before the composites go, again
    so the relation is never briefly unenforced, and the unique constraints go
    last because a composite key cannot be dropped after the constraint it
    references.
    """
    for child, name, column, parent in SINGLE_KEYS:
        op.create_foreign_key(name, child, parent, [column], ["id"], ondelete="CASCADE")
    for name, child, _, _ in COMPOSITE_KEYS:
        op.drop_constraint(name, child, type_="foreignkey")
    for table, name in PARENT_KEYS:
        op.drop_constraint(name, table, type_="unique")
