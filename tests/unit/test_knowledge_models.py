"""Metadata guarantees for the knowledge tables.

Read from the mapped metadata rather than a database, so they run in the unit
suite and catch drift against migration 0007 without PostgreSQL.
"""

from __future__ import annotations

from sqlalchemy import Table, UniqueConstraint

from app.db.models.knowledge import (
    EMBEDDING_DIMENSIONS,
    Document,
    DocumentChunk,
    DocumentSource,
    DocumentStatus,
    KnowledgeBase,
)


def _index_names(table: Table) -> set[str]:
    return {index.name for index in table.indexes if index.name is not None}


def _unique_columns(table: Table, name: str) -> tuple[str, ...]:
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and constraint.name == name:
            return tuple(column.name for column in constraint.columns)
    raise AssertionError(f"{table.name} has no unique constraint named {name}")


def test_knowledge_tables_declare_the_indexes_the_migrations_create():
    assert _index_names(KnowledgeBase.__table__) == {"ix_knowledge_bases_tenant_id"}
    assert _index_names(Document.__table__) == {
        "ix_documents_tenant_id",
        "ix_documents_tenant_id_status",
        "ix_documents_knowledge_base_id",
    }
    assert _index_names(DocumentChunk.__table__) == {
        "ix_document_chunks_tenant_id",
        "ix_document_chunks_document_id",
        "ix_document_chunks_tenant_id_knowledge_base_id",
        # Migration 0039, and declared here as well so autogenerate compares
        # against it. `tests/integration/test_vector_index.py` checks the part
        # this cannot see: that it is HNSW over `vector_cosine_ops`, which is
        # the half that decides whether the planner will ever use it.
        "ix_document_chunks_embedding_hnsw",
    }


def test_enum_values_match_the_migration_literals():
    assert [member.value for member in DocumentStatus] == [
        "pending",
        "processing",
        "ready",
        "failed",
    ]
    assert [member.value for member in DocumentSource] == ["text", "markdown", "pdf"]


def test_knowledge_base_names_are_unique_per_workspace():
    assert _unique_columns(KnowledgeBase.__table__, "uq_knowledge_bases_tenant_id_name") == (
        "tenant_id",
        "name",
    )


def test_ingestion_idempotency_is_keyed_on_the_content_hash():
    """The same bytes twice is a repeat, and the constraint is what enforces it."""
    assert _unique_columns(
        Document.__table__,
        "uq_documents_tenant_id_knowledge_base_id_content_hash",
    ) == ("tenant_id", "knowledge_base_id", "content_hash")


def test_a_chunk_ordinal_is_unique_within_its_document():
    assert _unique_columns(
        DocumentChunk.__table__,
        "uq_document_chunks_tenant_id_document_id_ordinal",
    ) == ("tenant_id", "document_id", "ordinal")


def test_every_knowledge_table_carries_its_own_tenant_column():
    """Including chunks.

    Similarity search reads the chunk table alone, so the tenant predicate has
    to be expressible on the row being scanned. A filter that depends on a join
    is a filter someone will eventually write without the join.
    """
    for table in (KnowledgeBase.__table__, Document.__table__, DocumentChunk.__table__):
        assert "tenant_id" in table.columns


def test_tenant_foreign_keys_cascade():
    for table in (KnowledgeBase.__table__, Document.__table__, DocumentChunk.__table__):
        (foreign_key,) = table.c.tenant_id.foreign_keys
        assert foreign_key.column.table.name == "tenants"
        assert foreign_key.ondelete == "CASCADE"


def test_chunks_die_with_their_document_and_their_knowledge_base():
    (document_key,) = DocumentChunk.__table__.c.document_id.foreign_keys
    assert document_key.column.table.name == "documents"
    assert document_key.ondelete == "CASCADE"

    (base_key,) = DocumentChunk.__table__.c.knowledge_base_id.foreign_keys
    assert base_key.column.table.name == "knowledge_bases"
    assert base_key.ondelete == "CASCADE"


def test_documents_die_with_their_knowledge_base():
    (foreign_key,) = Document.__table__.c.knowledge_base_id.foreign_keys
    assert foreign_key.column.table.name == "knowledge_bases"
    assert foreign_key.ondelete == "CASCADE"


def test_the_embedding_column_matches_the_declared_width():
    """A mismatch would fail as a driver error halfway through writing chunks."""
    assert DocumentChunk.__table__.c.embedding.type.dim == EMBEDDING_DIMENSIONS


def test_an_embedding_may_be_absent():
    """A chunk is written before its embedding is known.

    That is what lets ingestion fail partway without losing the chunking work.
    """
    assert DocumentChunk.__table__.c.embedding.nullable is True


def test_enum_defaults_are_application_side():
    """Migration 0007 declares no server default for the enum columns.

    A server_default here would put the metadata and the migration in
    disagreement, and env.py compares server defaults.
    """
    for column_name in ("status", "source"):
        column = Document.__table__.c[column_name]
        assert column.server_default is None
        assert column.default is not None


def test_audit_timestamps_have_server_defaults():
    for table in (KnowledgeBase.__table__, Document.__table__, DocumentChunk.__table__):
        assert table.c.created_at.server_default is not None
        assert table.c.updated_at.server_default is not None


def test_a_document_is_retrievable_only_when_ready():
    """Chunks written before a failure must not answer questions."""
    assert Document(status=DocumentStatus.READY).is_retrievable is True
    for status in (DocumentStatus.PENDING, DocumentStatus.PROCESSING, DocumentStatus.FAILED):
        assert Document(status=status).is_retrievable is False


def test_documents_are_deleted_rather_than_soft_deleted():
    """A soft-deleted row retrieval forgot to filter would keep answering."""
    assert "deleted_at" not in Document.__table__.columns
    assert "deleted_at" not in DocumentChunk.__table__.columns
