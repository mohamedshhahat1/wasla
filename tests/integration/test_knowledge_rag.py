"""Ingestion and retrieval against real PostgreSQL and pgvector.

The embedding *model* is faked (see `tests/fake_embeddings`); the vector search
is not. Chunks go into a real `vector` column and come back ordered by real
cosine distance, which is the only way to prove the property that matters most
here: that one company's question cannot reach another company's documents.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ExternalServiceError, TenantIsolationError, ValidationError
from app.db.models.knowledge import DocumentSource, DocumentStatus
from app.db.models.tenant import Tenant
from app.repositories.knowledge_repository import DocumentChunkRepository
from app.services.knowledge_service import KnowledgeService
from app.services.retrieval_service import RetrievalService
from tests.fake_embeddings import BrokenEmbeddings, FakeEmbeddings
from tests.fakes import as_embeddings

pytestmark = pytest.mark.integration

FINISHING_PRICES = """
Apartment finishing prices

Economy finishing costs 4500 EGP per square metre and covers plaster, paint,
ceramic flooring and basic electrical work.

Premium finishing costs 7200 EGP per square metre and adds imported sanitary
fittings, gypsum ceiling detail and full air conditioning installation.
"""

RETURNS_POLICY = """
Returns and refunds

A customer may return any undamaged item within fourteen days of delivery for a
full refund. Damaged items are replaced rather than refunded.

Refunds reach the original payment card within five working days of the return
being received at the warehouse.
"""

COMPETITOR_DOCUMENT = """
Wedding photography packages

The silver package covers six hours of coverage and two hundred edited
photographs. The gold package covers a full day and includes an album.
"""


async def _tenant(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _ingested(
    session: AsyncSession,
    *,
    tenant: Tenant,
    title: str,
    text: str,
    embeddings: FakeEmbeddings | BrokenEmbeddings | None = None,
) -> tuple[Any, ...]:
    """Submit and ingest a document, returning it ready to retrieve."""
    knowledge = KnowledgeService(session=session, tenant_id=tenant.id)
    base = await knowledge.ensure_default_knowledge_base()
    document, _ = await knowledge.submit(
        knowledge_base_id=base.id,
        title=title,
        raw=text,
    )
    await knowledge.ingest(
        document_id=document.id,
        embeddings=as_embeddings(embeddings or FakeEmbeddings()),
    )
    return base, document


def _service(
    session: AsyncSession,
    tenant: Tenant,
    embeddings: FakeEmbeddings | BrokenEmbeddings | None = None,
) -> RetrievalService:
    return RetrievalService(
        session=session,
        tenant_id=tenant.id,
        embeddings=as_embeddings(embeddings or FakeEmbeddings()),
    )


async def test_a_document_becomes_retrievable_chunks(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")

    _, document = await _ingested(
        db_session,
        tenant=tenant,
        title="Prices",
        text=FINISHING_PRICES,
    )

    assert document.status is DocumentStatus.READY
    assert document.chunk_count > 0
    assert document.ingested_at is not None
    chunks = await DocumentChunkRepository(db_session, tenant_id=tenant.id).list_for_document(
        document_id=document.id
    )
    assert len(chunks) == document.chunk_count
    assert all(chunk.embedding is not None for chunk in chunks)
    # Ordinals are contiguous from zero, so a passage can be read back in order.
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


async def test_a_question_finds_the_passage_that_answers_it(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    await _ingested(db_session, tenant=tenant, title="Prices", text=FINISHING_PRICES)
    await _ingested(db_session, tenant=tenant, title="Returns", text=RETURNS_POLICY)

    found = await _service(db_session, tenant).search(
        query="premium finishing cost per square metre",
    )

    assert not found.is_empty
    assert "7200" in found.passages[0].content
    assert found.passages[0].document_title == "Prices"


async def test_the_nearest_passage_comes_first(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    await _ingested(db_session, tenant=tenant, title="Prices", text=FINISHING_PRICES)
    await _ingested(db_session, tenant=tenant, title="Returns", text=RETURNS_POLICY)

    found = await _service(db_session, tenant).search(query="refund payment card warehouse")

    assert found.passages[0].document_title == "Returns"
    distances = [passage.distance for passage in found.passages]
    assert distances == sorted(distances)


async def test_a_question_about_nothing_stored_finds_nothing(db_session: AsyncSession) -> None:
    """The distance threshold is what stops a least-bad match being returned."""
    tenant = await _tenant(db_session, slug="acme")
    await _ingested(db_session, tenant=tenant, title="Prices", text=FINISHING_PRICES)

    found = await _service(db_session, tenant).search(
        query="wedding photography album silver package",
    )

    assert found.is_empty


async def test_an_empty_knowledge_base_answers_empty_rather_than_failing(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    await knowledge.ensure_default_knowledge_base()

    found = await _service(db_session, tenant).search(query="anything at all")

    assert found.is_empty
    assert found.passages == ()


async def test_an_empty_result_tells_the_model_not_to_guess(db_session: AsyncSession) -> None:
    """The wording is load-bearing.

    A model handed an empty string fills the silence from its training data. It
    has to be told, in words, that the knowledge base has nothing.
    """
    tenant = await _tenant(db_session, slug="acme")

    context = (await _service(db_session, tenant).search(query="anything")).as_context()

    assert context.strip() != ""
    assert "no information about this was found" in context.lower()
    assert "do not have that information" in context.lower()
    assert "rather than guessing" in context.lower()


async def test_one_workspace_cannot_retrieve_anothers_documents(db_session: AsyncSession) -> None:
    """The single most important test in this file."""
    mine = await _tenant(db_session, slug="mine")
    theirs = await _tenant(db_session, slug="theirs")
    await _ingested(
        db_session,
        tenant=theirs,
        title="Their prices",
        text=FINISHING_PRICES,
    )

    # The exact query that finds it for its owner.
    found = await _service(db_session, mine).search(
        query="premium finishing cost per square metre",
    )

    assert found.is_empty


async def test_a_search_returns_only_the_asking_workspaces_chunks(db_session: AsyncSession) -> None:
    """Both workspaces hold the same text; each sees only its own copy."""
    mine = await _tenant(db_session, slug="mine")
    theirs = await _tenant(db_session, slug="theirs")
    _, my_document = await _ingested(
        db_session,
        tenant=mine,
        title="My prices",
        text=FINISHING_PRICES,
    )
    _, their_document = await _ingested(
        db_session,
        tenant=theirs,
        title="Their prices",
        text=FINISHING_PRICES,
    )

    found = await _service(db_session, mine).search(query="economy finishing plaster paint")

    assert not found.is_empty
    assert {passage.document_title for passage in found.passages} == {"My prices"}
    assert my_document.id != their_document.id


async def test_a_knowledge_base_filter_stays_inside_the_workspace(db_session: AsyncSession) -> None:
    mine = await _tenant(db_session, slug="mine")
    theirs = await _tenant(db_session, slug="theirs")
    _, _ = await _ingested(db_session, tenant=mine, title="Mine", text=RETURNS_POLICY)
    their_base, _ = await _ingested(
        db_session,
        tenant=theirs,
        title="Theirs",
        text=FINISHING_PRICES,
    )

    # Naming their knowledge base id directly still reaches nothing.
    found = await _service(db_session, mine).search(
        query="premium finishing cost",
        knowledge_base_id=their_base.id,
    )

    assert found.is_empty


async def test_submitting_the_same_text_twice_is_one_document(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    base = await knowledge.ensure_default_knowledge_base()

    first, created_first = await knowledge.submit(
        knowledge_base_id=base.id,
        title="Prices",
        raw=FINISHING_PRICES,
    )
    second, created_second = await knowledge.submit(
        knowledge_base_id=base.id,
        title="Prices again",
        raw=FINISHING_PRICES,
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


async def test_ingesting_twice_does_not_double_the_chunks(db_session: AsyncSession) -> None:
    """Re-ingestion replaces chunks; a duplicated job must change nothing."""
    tenant = await _tenant(db_session, slug="acme")
    _, document = await _ingested(
        db_session,
        tenant=tenant,
        title="Prices",
        text=FINISHING_PRICES,
    )
    first_count = document.chunk_count

    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    await knowledge.reingest(document.id)
    await knowledge.ingest(document_id=document.id, embeddings=as_embeddings(FakeEmbeddings()))

    chunks = DocumentChunkRepository(db_session, tenant_id=tenant.id)
    assert await chunks.count_for_document(document_id=document.id) == first_count
    assert document.chunk_count == first_count


async def test_a_document_already_ready_is_left_alone(db_session: AsyncSession) -> None:
    """What makes a duplicated queue job harmless."""
    tenant = await _tenant(db_session, slug="acme")
    _, document = await _ingested(
        db_session,
        tenant=tenant,
        title="Prices",
        text=FINISHING_PRICES,
    )
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    embeddings = FakeEmbeddings()

    result = await knowledge.ingest(document_id=document.id, embeddings=as_embeddings(embeddings))

    assert result.reused is True
    assert result.chunks_written == 0
    # Nothing was re-embedded, so the repeat cost nothing.
    assert embeddings.calls == 0


async def test_a_provider_failure_marks_the_document_failed(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    base = await knowledge.ensure_default_knowledge_base()
    document, _ = await knowledge.submit(
        knowledge_base_id=base.id,
        title="Prices",
        raw=FINISHING_PRICES,
    )

    with pytest.raises(ExternalServiceError):
        await knowledge.ingest(
            document_id=document.id,
            embeddings=as_embeddings(
                BrokenEmbeddings(ExternalServiceError("The AI provider is unavailable."))
            ),
        )

    assert document.status is DocumentStatus.FAILED
    assert document.error is not None
    # Zeroed, because a failed run leaves nothing retrievable and claiming
    # otherwise would advertise knowledge that is not there.
    assert document.chunk_count == 0


async def test_a_failed_document_is_not_retrievable(db_session: AsyncSession) -> None:
    """Chunks written before a failure must not answer questions."""
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    base = await knowledge.ensure_default_knowledge_base()
    document, _ = await knowledge.submit(
        knowledge_base_id=base.id,
        title="Prices",
        raw=FINISHING_PRICES,
    )
    await knowledge.ingest(document_id=document.id, embeddings=as_embeddings(FakeEmbeddings()))

    # Now break it: the chunks still exist, but the document is not READY.
    await knowledge.reingest(document.id)
    with pytest.raises(ExternalServiceError):
        await knowledge.ingest(
            document_id=document.id,
            embeddings=as_embeddings(
                BrokenEmbeddings(ExternalServiceError("The AI provider is unavailable."))
            ),
        )

    found = await _service(db_session, tenant).search(query="premium finishing cost")

    assert found.is_empty


async def test_a_failed_document_can_be_retried_once_the_cause_is_fixed(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    base = await knowledge.ensure_default_knowledge_base()
    document, _ = await knowledge.submit(
        knowledge_base_id=base.id,
        title="Prices",
        raw=FINISHING_PRICES,
    )
    with pytest.raises(ExternalServiceError):
        await knowledge.ingest(
            document_id=document.id,
            embeddings=as_embeddings(BrokenEmbeddings(ExternalServiceError("down"))),
        )

    await knowledge.reingest(document.id)
    result = await knowledge.ingest(
        document_id=document.id, embeddings=as_embeddings(FakeEmbeddings())
    )

    assert result.chunks_written > 0
    assert document.status is DocumentStatus.READY
    # The stale explanation must not outlive the problem.
    assert document.error is None


async def test_a_document_with_no_indexable_text_is_refused(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    base = await knowledge.ensure_default_knowledge_base()

    with pytest.raises(ValidationError):
        await knowledge.submit(knowledge_base_id=base.id, title="Empty", raw="   \n\n  ")


async def test_pdf_is_refused_explicitly_rather_than_silently_empty(
    db_session: AsyncSession,
) -> None:
    """Better a clear refusal than a document that looks ingested and answers nothing."""
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    base = await knowledge.ensure_default_knowledge_base()

    with pytest.raises(ValidationError):
        await knowledge.submit(
            knowledge_base_id=base.id,
            title="Brochure",
            raw="%PDF-1.7 binary",
            source=DocumentSource.PDF,
        )


async def test_deleting_a_document_removes_its_chunks(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    _, document = await _ingested(
        db_session,
        tenant=tenant,
        title="Prices",
        text=FINISHING_PRICES,
    )
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)

    await knowledge.delete_document(document.id)

    chunks = DocumentChunkRepository(db_session, tenant_id=tenant.id)
    assert await chunks.count_for_document(document_id=document.id) == 0
    found = await _service(db_session, tenant).search(query="premium finishing cost")
    assert found.is_empty


async def test_another_workspaces_document_is_not_found(db_session: AsyncSession) -> None:
    mine = await _tenant(db_session, slug="mine")
    theirs = await _tenant(db_session, slug="theirs")
    _, hidden = await _ingested(
        db_session,
        tenant=theirs,
        title="Theirs",
        text=COMPETITOR_DOCUMENT,
    )
    knowledge = KnowledgeService(session=db_session, tenant_id=mine.id)

    with pytest.raises(TenantIsolationError):
        await knowledge.get_document(hidden.id)
    with pytest.raises(TenantIsolationError):
        await knowledge.delete_document(hidden.id)


async def test_two_workspaces_may_name_a_knowledge_base_the_same(db_session: AsyncSession) -> None:
    mine = await _tenant(db_session, slug="mine")
    theirs = await _tenant(db_session, slug="theirs")

    first = await KnowledgeService(
        session=db_session,
        tenant_id=mine.id,
    ).create_knowledge_base(name="Products")
    second = await KnowledgeService(
        session=db_session,
        tenant_id=theirs.id,
    ).create_knowledge_base(name="Products")

    assert first.id != second.id


async def test_an_unknown_document_id_is_not_found(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)

    with pytest.raises(TenantIsolationError):
        await knowledge.get_document(uuid.uuid4())
