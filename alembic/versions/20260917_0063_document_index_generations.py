"""index documents in generations, and pin a chunk to its document's knowledge base

Revision ID: 0063
Revises: 0062

**Why generations** (RAG-01, RAG-04, RAG-06, RAG-09, PD-RAG-1). A document's
`status` was carrying two facts - whether it serves, and whether an indexing
attempt is outstanding - and could not carry both. A re-index set a working
document `pending`, which hid it from every agent until the re-index finished and
for ever if it failed; a failure was written inside the transaction the failure
rolled back, so nothing recorded it and the recovery sweep re-embedded it every
minute; and the worker held the document's row lock across every embedding call.

`document_index_generations` holds the attempt half: its state, a claim token and
lease, the retry budget, the last error, and the embedding space its vectors were
made in. At most one generation per document is `active` and at most one is in
flight, both enforced by partial unique indexes. Every chunk now belongs to one
generation, and retrieval serves only the active one.

**Existing data is carried over as it stands.** Each document gets generation 1:

- `ready` documents get an `active` generation owning their existing chunks, with
  the embedding space set to the deployment's configured model at 1,536
  dimensions - the only model their vectors can have come from, since the width
  is fixed in the column type. If the configured model was changed *before* this
  migration without a re-index, those vectors are from a different space and the
  stale-embedding command will list them.
- `pending` and `processing` documents get a `pending` generation and are left
  `pending`; the recovery sweep picks them up as before.
- `failed` documents get a `failed` generation with their recorded reason.

Chunks of any document that was not `ready` are deleted: retrieval never served
them, and no generation may own a chunk it did not publish.

**Why a three-column key from chunks to documents** (RAG-13). The composite keys
of 0053 stopped a chunk naming another workspace's document or knowledge base, but
not a chunk of a document in knowledge base A claiming knowledge base B of the
same workspace. `knowledge_base_id` on a chunk is a copy of its document's, so a
disagreeing copy is repaired to match the document before the key is added.

The enum labels are added in an autocommit block: `ADD VALUE` cannot be used in
the transaction that added it, and `IF NOT EXISTS` makes a partial run
re-runnable.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

EMBEDDING_DIMENSIONS = 1536
EMBEDDING_SCHEMA_VERSION = 1

GENERATION_STATES = ("pending", "processing", "active", "superseded", "failed")

USAGE_LABELS = (
    ("embedding_request", "rag_query"),
    ("embedding_input_token", "embedding_request"),
)
AUDIT_LABELS = (
    "knowledge_base_created",
    "knowledge_document_submitted",
    "knowledge_document_deleted",
    "knowledge_document_reindex_requested",
    "knowledge_document_indexing_failed",
)


def _configured_embedding_model() -> str:
    # A migration only needs this one value. Loading the complete application
    # Settings would make a schema change depend on unrelated API credentials.
    return os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for label, after in USAGE_LABELS:
            op.execute(
                f"ALTER TYPE usage_event_type ADD VALUE IF NOT EXISTS '{label}' AFTER '{after}'"
            )
        for label in AUDIT_LABELS:
            op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{label}'")

    generation_state = postgresql.ENUM(*GENERATION_STATES, name="document_generation_state")
    generation_state.create(op.get_bind(), checkfirst=True)

    op.create_unique_constraint(
        "uq_documents_tenant_id_id_knowledge_base_id",
        "documents",
        ["tenant_id", "id", "knowledge_base_id"],
    )

    op.create_table(
        "document_index_generations",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(
                *GENERATION_STATES, name="document_generation_state", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.UUID(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding_provider", sa.String(length=32), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("embedding_schema_version", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        sa.CheckConstraint("chunk_count >= 0", name="chunk_count_not_negative"),
        sa.CheckConstraint(
            "state <> 'processing' OR (claim_token IS NOT NULL AND claimed_at IS NOT NULL)",
            name="processing_is_claimed",
        ),
        sa.CheckConstraint(
            "state NOT IN ('active', 'superseded') OR ("
            "published_at IS NOT NULL AND embedding_provider IS NOT NULL "
            "AND embedding_model IS NOT NULL AND embedding_dimensions IS NOT NULL "
            "AND embedding_schema_version IS NOT NULL)",
            name="published_has_identity",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            name="fk_document_index_generations_tenant_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_document_index_generations_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_index_generations"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "id",
            name="uq_document_index_generations_tenant_id_document_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "number",
            name="uq_document_index_generations_tenant_id_document_id_number",
        ),
    )
    op.create_index(
        "ix_document_index_generations_tenant_id", "document_index_generations", ["tenant_id"]
    )
    op.create_index(
        "ix_document_index_generations_document_id",
        "document_index_generations",
        ["document_id"],
    )
    op.create_index(
        "uq_document_index_generations_active",
        "document_index_generations",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_index(
        "uq_document_index_generations_in_flight",
        "document_index_generations",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'processing')"),
    )
    op.create_index(
        "ix_document_index_generations_outstanding",
        "document_index_generations",
        ["state", "next_retry_at"],
        postgresql_where=sa.text("state IN ('pending', 'processing')"),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text("""
            INSERT INTO document_index_generations (
                id, tenant_id, document_id, number, state, trigger, attempts,
                last_error_code, error, last_error_at,
                embedding_provider, embedding_model, embedding_dimensions,
                embedding_schema_version, chunk_count, published_at,
                created_at, updated_at
            )
            SELECT
                gen_random_uuid(), d.tenant_id, d.id, 1,
                CASE d.status
                    WHEN 'ready' THEN 'active'
                    WHEN 'failed' THEN 'failed'
                    ELSE 'pending'
                END::document_generation_state,
                'migrated', 0,
                CASE WHEN d.status = 'failed' THEN 'legacy_failure' END,
                CASE WHEN d.status = 'failed' THEN d.error END,
                CASE WHEN d.status = 'failed' THEN d.updated_at END,
                CASE WHEN d.status = 'ready' THEN 'openai' END,
                CASE WHEN d.status = 'ready' THEN CAST(:model AS varchar) END,
                CASE WHEN d.status = 'ready' THEN CAST(:dimensions AS integer) END,
                CASE WHEN d.status = 'ready' THEN CAST(:schema_version AS integer) END,
                CASE WHEN d.status = 'ready' THEN d.chunk_count ELSE 0 END,
                CASE WHEN d.status = 'ready' THEN coalesce(d.ingested_at, d.updated_at) END,
                d.created_at, now()
            FROM documents d
            """),
        {
            "model": _configured_embedding_model(),
            "dimensions": EMBEDDING_DIMENSIONS,
            "schema_version": EMBEDDING_SCHEMA_VERSION,
        },
    )
    bind.execute(sa.text("UPDATE documents SET status = 'pending' WHERE status = 'processing'"))

    op.add_column("document_chunks", sa.Column("generation_id", sa.UUID(), nullable=True))
    bind.execute(sa.text("""
            DELETE FROM document_chunks c
             USING documents d
             WHERE d.id = c.document_id AND d.status <> 'ready'
            """))
    bind.execute(sa.text("""
            UPDATE document_chunks c
               SET generation_id = g.id,
                   knowledge_base_id = d.knowledge_base_id
              FROM document_index_generations g
              JOIN documents d ON d.id = g.document_id
             WHERE g.document_id = c.document_id AND g.state = 'active'
            """))
    op.alter_column("document_chunks", "generation_id", nullable=False)

    op.create_foreign_key(
        "fk_document_chunks_tenant_document_knowledge_base",
        "document_chunks",
        "documents",
        ["tenant_id", "document_id", "knowledge_base_id"],
        ["tenant_id", "id", "knowledge_base_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_document_chunks_tenant_document_generation",
        "document_chunks",
        "document_index_generations",
        ["tenant_id", "document_id", "generation_id"],
        ["tenant_id", "document_id", "id"],
        ondelete="CASCADE",
    )
    # Dropped after its replacement exists, so the relation is never unenforced.
    op.drop_constraint("fk_document_chunks_tenant_document", "document_chunks", type_="foreignkey")

    op.create_unique_constraint(
        "uq_document_chunks_tenant_id_generation_id_ordinal",
        "document_chunks",
        ["tenant_id", "generation_id", "ordinal"],
    )
    op.drop_constraint(
        "uq_document_chunks_tenant_id_document_id_ordinal", "document_chunks", type_="unique"
    )
    op.create_index("ix_document_chunks_generation_id", "document_chunks", ["generation_id"])


def downgrade() -> None:
    """Back to one status per document, keeping only what was being served.

    Chunks of any generation that is not active are deleted, because the
    previous schema has nowhere to keep a second version of a document. The enum
    labels stay: PostgreSQL cannot drop one, and an unused label is harmless.
    """
    bind = op.get_bind()
    bind.execute(sa.text("""
            DELETE FROM document_chunks c
             USING document_index_generations g
             WHERE g.id = c.generation_id AND g.state <> 'active'
            """))
    op.drop_index("ix_document_chunks_generation_id", table_name="document_chunks")
    op.create_unique_constraint(
        "uq_document_chunks_tenant_id_document_id_ordinal",
        "document_chunks",
        ["tenant_id", "document_id", "ordinal"],
    )
    op.drop_constraint(
        "uq_document_chunks_tenant_id_generation_id_ordinal", "document_chunks", type_="unique"
    )
    op.create_foreign_key(
        "fk_document_chunks_tenant_document",
        "document_chunks",
        "documents",
        ["tenant_id", "document_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "fk_document_chunks_tenant_document_generation", "document_chunks", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_document_chunks_tenant_document_knowledge_base", "document_chunks", type_="foreignkey"
    )
    op.drop_column("document_chunks", "generation_id")

    # A document serving a generation is ready whatever its latest attempt did;
    # one without is what its latest attempt says.
    bind.execute(sa.text("""
            UPDATE documents d
               SET status = CASE
                   WHEN EXISTS (
                       SELECT 1 FROM document_index_generations g
                        WHERE g.document_id = d.id AND g.state = 'active'
                   ) THEN 'ready'::document_status
                   WHEN d.status = 'processing' THEN 'pending'::document_status
                   ELSE d.status
               END
            """))
    op.drop_table("document_index_generations")
    sa.Enum(name="document_generation_state").drop(op.get_bind(), checkfirst=True)
    op.drop_constraint("uq_documents_tenant_id_id_knowledge_base_id", "documents", type_="unique")
