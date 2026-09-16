"""Metadata guarantees for the knowledge tables.

Read from the mapped metadata rather than a database, so they run in the unit
suite and catch drift against migration 0007 without PostgreSQL.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, Index, Table, UniqueConstraint

from app.db.models.knowledge import (
    EMBEDDING_DIMENSIONS,
    Document,
    DocumentChunk,
    DocumentIndexGeneration,
    DocumentSource,
    DocumentStatus,
    GenerationState,
    KnowledgeBase,
)
from tests.fakes import as_table


def _index_names(table: Table) -> set[str]:
    return {index.name for index in table.indexes if index.name is not None}


def _unique_columns(table: Table, name: str) -> tuple[str, ...]:
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and constraint.name == name:
            return tuple(column.name for column in constraint.columns)
    raise AssertionError(f"{table.name} has no unique constraint named {name}")


def test_knowledge_tables_declare_the_indexes_the_migrations_create() -> None:
    assert _index_names(as_table(KnowledgeBase.__table__)) == {"ix_knowledge_bases_tenant_id"}
    assert _index_names(as_table(Document.__table__)) == {
        "ix_documents_tenant_id",
        "ix_documents_tenant_id_status",
        "ix_documents_knowledge_base_id",
    }
    assert _index_names(as_table(DocumentChunk.__table__)) == {
        "ix_document_chunks_tenant_id",
        "ix_document_chunks_document_id",
        "ix_document_chunks_tenant_id_knowledge_base_id",
        # Migration 0039, and declared here as well so autogenerate compares
        # against it. `tests/integration/test_vector_index.py` checks the part
        # this cannot see: that it is HNSW over `vector_cosine_ops`, which is
        # the half that decides whether the planner will ever use it.
        "ix_document_chunks_embedding_hnsw",
        # Migration 0063: what a superseded generation's chunks are deleted by.
        "ix_document_chunks_generation_id",
    }
    assert _index_names(as_table(DocumentIndexGeneration.__table__)) == {
        "ix_document_index_generations_tenant_id",
        "ix_document_index_generations_document_id",
        "ix_document_index_generations_outstanding",
        "uq_document_index_generations_active",
        "uq_document_index_generations_in_flight",
    }


def _partial_unique(table: Table, name: str) -> tuple[tuple[str, ...], str]:
    for index in table.indexes:
        if isinstance(index, Index) and index.name == name:
            assert index.unique, name
            where = index.dialect_options["postgresql"]["where"]
            return tuple(column.name for column in index.columns), str(where)
    raise AssertionError(f"{table.name} has no index named {name}")


def test_a_document_serves_at_most_one_generation() -> None:
    """The database, not the publish code, is what refuses two active versions."""
    columns, where = _partial_unique(
        as_table(DocumentIndexGeneration.__table__), "uq_document_index_generations_active"
    )
    assert columns == ("document_id",)
    assert where == "state = 'active'"


def test_a_document_has_at_most_one_attempt_outstanding() -> None:
    """What coalesces a burst of re-index requests into one (RAG-07)."""
    columns, where = _partial_unique(
        as_table(DocumentIndexGeneration.__table__), "uq_document_index_generations_in_flight"
    )
    assert columns == ("document_id",)
    assert where == "state IN ('pending', 'processing')"


def test_generation_states_match_the_migration_literals() -> None:
    assert [member.value for member in GenerationState] == [
        "pending",
        "processing",
        "active",
        "superseded",
        "failed",
    ]


def test_enum_values_match_the_migration_literals() -> None:
    assert [member.value for member in DocumentStatus] == [
        "pending",
        "processing",
        "ready",
        "failed",
    ]
    assert [member.value for member in DocumentSource] == ["text", "markdown", "pdf"]


def test_knowledge_base_names_are_unique_per_workspace() -> None:
    assert _unique_columns(
        as_table(KnowledgeBase.__table__), "uq_knowledge_bases_tenant_id_name"
    ) == (
        "tenant_id",
        "name",
    )


def test_ingestion_idempotency_is_keyed_on_the_content_hash() -> None:
    """The same bytes twice is a repeat, and the constraint is what enforces it."""
    assert _unique_columns(
        as_table(Document.__table__),
        "uq_documents_tenant_id_knowledge_base_id_content_hash",
    ) == ("tenant_id", "knowledge_base_id", "content_hash")


def test_a_chunk_ordinal_is_unique_within_its_generation() -> None:
    """Per generation: while a re-index publishes, two versions' ordinals coexist."""
    assert _unique_columns(
        as_table(DocumentChunk.__table__),
        "uq_document_chunks_tenant_id_generation_id_ordinal",
    ) == ("tenant_id", "generation_id", "ordinal")


def test_every_knowledge_table_carries_its_own_tenant_column() -> None:
    """Including chunks.

    Similarity search reads the chunk table alone, so the tenant predicate has
    to be expressible on the row being scanned. A filter that depends on a join
    is a filter someone will eventually write without the join.
    """
    for table in (
        KnowledgeBase.__table__,
        Document.__table__,
        DocumentChunk.__table__,
        DocumentIndexGeneration.__table__,
    ):
        assert "tenant_id" in table.columns


def test_tenant_foreign_keys_cascade() -> None:
    """`tenant_id` points at `tenants` and dies with it.

    Selected by target rather than unpacked as the only key, because since
    migration 0053 it is not the only one: `tenant_id` is also the first column
    of the composite keys that pin a document to a knowledge base and a chunk to
    both (ADR-100). Asking "the foreign key on this column" would have been an
    accurate question before those existed and is an ambiguous one now, so the
    test asks the question it actually means.
    """
    for mapped in (
        KnowledgeBase.__table__,
        Document.__table__,
        DocumentChunk.__table__,
        DocumentIndexGeneration.__table__,
    ):
        table = as_table(mapped)
        to_tenants = [
            key for key in table.c.tenant_id.foreign_keys if key.column.table.name == "tenants"
        ]
        assert len(to_tenants) == 1, table.name
        assert to_tenants[0].ondelete == "CASCADE"


def _composite(table: Table, name: str) -> ForeignKeyConstraint:
    for constraint in table.constraints:
        if isinstance(constraint, ForeignKeyConstraint) and constraint.name == name:
            return constraint
    raise AssertionError(f"{table.name} has no foreign key named {name}")


def test_chunks_die_with_their_document_generation_and_knowledge_base() -> None:
    chunks = as_table(DocumentChunk.__table__)

    # RAG-13: three columns, so a chunk's knowledge base must be its document's.
    document_key = _composite(chunks, "fk_document_chunks_tenant_document_knowledge_base")
    assert [element.target_fullname for element in document_key.elements] == [
        "documents.tenant_id",
        "documents.id",
        "documents.knowledge_base_id",
    ]
    assert document_key.ondelete == "CASCADE"

    generation_key = _composite(chunks, "fk_document_chunks_tenant_document_generation")
    assert [element.target_fullname for element in generation_key.elements] == [
        "document_index_generations.tenant_id",
        "document_index_generations.document_id",
        "document_index_generations.id",
    ]
    assert generation_key.ondelete == "CASCADE"

    base_key = _composite(chunks, "fk_document_chunks_tenant_knowledge_base")
    assert base_key.referred_table.name == "knowledge_bases"
    assert base_key.ondelete == "CASCADE"
    assert chunks.c.generation_id.nullable is False


def test_documents_die_with_their_knowledge_base() -> None:
    (foreign_key,) = as_table(Document.__table__).c.knowledge_base_id.foreign_keys
    assert foreign_key.column.table.name == "knowledge_bases"
    assert foreign_key.ondelete == "CASCADE"


def test_the_embedding_column_matches_the_declared_width() -> None:
    """A mismatch would fail as a driver error halfway through writing chunks."""
    column = as_table(DocumentChunk.__table__).c.embedding
    # `Vector.dim` is pgvector's, and pgvector ships no `py.typed`, so
    # SQLAlchemy's `TypeEngine` is as much as the checker knows here.
    assert column.type.dim == EMBEDDING_DIMENSIONS  # type: ignore[attr-defined]


def test_an_embedding_column_is_nullable_in_the_schema() -> None:
    """Nullable as migration 0007 created it; retrieval filters NULLs regardless.

    A generation's chunks are written with validated vectors in the transaction
    that publishes it, so no writer produces one without an embedding.
    """
    assert as_table(DocumentChunk.__table__).c.embedding.nullable is True


def test_enum_defaults_are_application_side() -> None:
    """Migration 0007 declares no server default for the enum columns.

    A server_default here would put the metadata and the migration in
    disagreement, and env.py compares server defaults.
    """
    for column_name in ("status", "source"):
        column = as_table(Document.__table__).c[column_name]
        assert column.server_default is None
        assert column.default is not None


def test_audit_timestamps_have_server_defaults() -> None:
    for table in (KnowledgeBase.__table__, Document.__table__, DocumentChunk.__table__):
        assert table.c.created_at.server_default is not None
        assert table.c.updated_at.server_default is not None


def test_a_document_is_retrievable_only_when_ready() -> None:
    """Chunks written before a failure must not answer questions."""
    assert Document(status=DocumentStatus.READY).is_retrievable is True
    for status in (DocumentStatus.PENDING, DocumentStatus.PROCESSING, DocumentStatus.FAILED):
        assert Document(status=status).is_retrievable is False


def test_documents_are_deleted_rather_than_soft_deleted() -> None:
    """A soft-deleted row retrieval forgot to filter would keep answering."""
    assert "deleted_at" not in as_table(Document.__table__).columns
    assert "deleted_at" not in as_table(DocumentChunk.__table__).columns
