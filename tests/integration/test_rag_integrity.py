"""What the database itself refuses, and what knowledge administration records.

**Integrity** (RAG-13, PD-RAG-1). Each forged row is written with raw SQL, the
way a buggy writer or a hand-run statement would, and each is paired with the
correctly-shaped row that the same statement accepts - so a refusal is the
constraint speaking, not a typo in the probe.

**Audit** (RAG-17). A document is something agents state to customers as the
business's own fact, so adding, re-indexing and removing one are recorded with
the person who did it - and never with the document's text or title.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.knowledge import DocumentIndexGeneration, GenerationState
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.knowledge_service import KnowledgeService
from app.services.workspace_purge_service import PURGED_TABLES
from tests.fake_embeddings import FakeEmbeddings
from tests.fakes import as_embeddings

pytestmark = pytest.mark.integration

SECRET_TITLE = "Confidential pricing sheet"
SECRET_TEXT = "Wholesale margin is 42 percent. CONFIDENTIAL_BODY_MARKER."


async def _tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name="Integrity", slug=f"int-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _indexed(session: AsyncSession, tenant: Tenant, *, base_name: str = "KB1") -> Any:
    knowledge = KnowledgeService(session=session, tenant_id=tenant.id)
    base = await knowledge.create_knowledge_base(name=base_name)
    view, _ = await knowledge.submit(
        knowledge_base_id=base.id, title="Doc " + base_name, raw=f"{base_name} text\n\nBody."
    )
    await knowledge.ingest(document_id=view.document.id, embeddings=as_embeddings(FakeEmbeddings()))
    return await knowledge.get_document(view.document.id)


async def _refused(session: AsyncSession, statement: str, parameters: dict[str, Any]) -> str:
    with pytest.raises(IntegrityError) as raised:
        async with session.begin_nested():
            await session.execute(text(statement), parameters)
    return str(raised.value.orig)


INSERT_CHUNK = (
    "INSERT INTO document_chunks (id, tenant_id, document_id, knowledge_base_id, "
    "generation_id, ordinal, content, token_estimate, created_at, updated_at) "
    "VALUES (:id, :tenant, :document, :base, :generation, :ordinal, 'probe', 1, now(), now())"
)


async def test_a_chunk_cannot_claim_a_knowledge_base_its_document_is_not_in(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    first = await _indexed(db_session, tenant, base_name="KB1")
    second = await _indexed(db_session, tenant, base_name="KB2")
    values = {
        "tenant": tenant.id,
        "document": first.document.id,
        "generation": first.active.id,
        "ordinal": 900,
    }

    # Presence: the correct knowledge base is accepted by the same statement.
    async with db_session.begin_nested():
        await db_session.execute(
            text(INSERT_CHUNK),
            {**values, "id": uuid.uuid4(), "base": first.document.knowledge_base_id},
        )

    error = await _refused(
        db_session,
        INSERT_CHUNK,
        {
            **values,
            "id": uuid.uuid4(),
            "ordinal": 901,
            "base": second.document.knowledge_base_id,
        },
    )
    assert "fk_document_chunks_tenant_document_knowledge_base" in error


async def test_a_chunk_cannot_belong_to_another_documents_generation(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    first = await _indexed(db_session, tenant, base_name="KB1")
    second = await _indexed(db_session, tenant, base_name="KB2")

    error = await _refused(
        db_session,
        INSERT_CHUNK,
        {
            "id": uuid.uuid4(),
            "tenant": tenant.id,
            "document": first.document.id,
            "base": first.document.knowledge_base_id,
            "generation": second.active.id,
            "ordinal": 902,
        },
    )
    assert "fk_document_chunks_tenant_document_generation" in error


INSERT_GENERATION = (
    "INSERT INTO document_index_generations (id, tenant_id, document_id, number, state, "
    "trigger, attempts, chunk_count, published_at, embedding_provider, embedding_model, "
    "embedding_dimensions, embedding_schema_version, claim_token, claimed_at, created_at, "
    "updated_at) VALUES (:id, :tenant, :document, :number, CAST(:state AS "
    "document_generation_state), 'reindex', 0, 0, :published, :provider, :model, :dims, "
    ":version, :token, :claimed, now(), now())"
)


def _generation(tenant: Tenant, document_id: uuid.UUID, number: int, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant": tenant.id,
        "document": document_id,
        "number": number,
        "state": "pending",
        "published": None,
        "provider": None,
        "model": None,
        "dims": None,
        "version": None,
        "token": None,
        "claimed": None,
    }
    values.update(overrides)
    return values


async def test_a_document_cannot_serve_two_generations(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    view = await _indexed(db_session, tenant)
    published = {
        "state": "active",
        "published": view.active.published_at,
        "provider": "openai",
        "model": "text-embedding-3-small",
        "dims": 1536,
        "version": 1,
    }

    error = await _refused(
        db_session, INSERT_GENERATION, _generation(tenant, view.document.id, 2, **published)
    )
    assert "uq_document_index_generations_active" in error

    # Presence: the same row as `superseded` is fine.
    async with db_session.begin_nested():
        await db_session.execute(
            text(INSERT_GENERATION),
            _generation(tenant, view.document.id, 3, **{**published, "state": "superseded"}),
        )


async def test_a_document_cannot_have_two_attempts_outstanding(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    view = await _indexed(db_session, tenant)
    await KnowledgeService(session=db_session, tenant_id=tenant.id).reindex(view.document.id)

    error = await _refused(db_session, INSERT_GENERATION, _generation(tenant, view.document.id, 9))
    assert "uq_document_index_generations_in_flight" in error


async def test_a_processing_generation_must_carry_its_claim(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    view = await _indexed(db_session, tenant)

    error = await _refused(
        db_session,
        INSERT_GENERATION,
        _generation(tenant, view.document.id, 5, state="processing"),
    )
    assert "ck_document_index_generations_processing_is_claimed" in error


async def test_a_published_generation_must_name_its_embedding_space(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    view = await _indexed(db_session, tenant)

    error = await _refused(
        db_session,
        INSERT_GENERATION,
        _generation(
            tenant,
            view.document.id,
            6,
            state="superseded",
            published=view.active.published_at,
        ),
    )
    assert "ck_document_index_generations_published_has_identity" in error


async def test_another_workspace_cannot_own_a_generation_of_this_document(
    db_session: AsyncSession,
) -> None:
    mine = await _tenant(db_session)
    theirs = await _tenant(db_session)
    view = await _indexed(db_session, mine)

    error = await _refused(
        db_session, INSERT_GENERATION, _generation(theirs, view.document.id, 7, state="failed")
    )
    assert "fk_document_index_generations_tenant_document" in error


def test_generations_are_erased_with_the_workspace() -> None:
    assert "document_index_generations" in PURGED_TABLES
    assert (
        PURGED_TABLES.index("document_chunks")
        < PURGED_TABLES.index("document_index_generations")
        < PURGED_TABLES.index("documents")
    )


# ------------------------------------------------------------------ RAG-17


async def _actor(session: AsyncSession) -> User:
    user = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com", is_active=True)
    session.add(user)
    await session.flush()
    return user


async def test_knowledge_administration_is_audited_without_its_content(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    admin = await _actor(db_session)
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)

    base = await knowledge.create_knowledge_base(name="Pricing", actor=admin)
    view, _ = await knowledge.submit(
        knowledge_base_id=base.id, title=SECRET_TITLE, raw=SECRET_TEXT, actor=admin
    )
    await knowledge.reindex(view.document.id, actor=admin)
    await knowledge.delete_document(view.document.id, actor=admin)
    await db_session.flush()

    rows = list(
        await db_session.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tenant.id).order_by(AuditLog.occurred_at)
        )
    )
    actions = [row.action for row in rows]
    assert actions == [
        AuditAction.KNOWLEDGE_BASE_CREATED,
        AuditAction.KNOWLEDGE_DOCUMENT_SUBMITTED,
        AuditAction.KNOWLEDGE_DOCUMENT_REINDEX_REQUESTED,
        AuditAction.KNOWLEDGE_DOCUMENT_DELETED,
    ]
    for row in rows:
        assert row.actor_id == admin.id
        assert row.actor_kind is AuditActorKind.USER
        rendered = repr((row.target_label, row.meta))
        assert "CONFIDENTIAL_BODY_MARKER" not in rendered
        assert SECRET_TITLE not in rendered
    submitted = rows[1]
    assert submitted.target_id == view.document.id
    assert submitted.meta == {
        "knowledge_base_id": str(base.id),
        "source": "text",
        "created": True,
        "generation": 1,
    }
    assert rows[3].meta is not None and rows[3].meta["knowledge_base_id"] == str(base.id)


async def test_the_default_generation_state_matches_its_meaning(db_session: AsyncSession) -> None:
    """A freshly submitted document owns exactly one pending generation, number 1."""
    tenant = await _tenant(db_session)
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    base = await knowledge.ensure_default_knowledge_base()
    view, created = await knowledge.submit(knowledge_base_id=base.id, title="T", raw="Body text.")

    rows = list(
        await db_session.scalars(
            select(DocumentIndexGeneration).where(
                DocumentIndexGeneration.document_id == view.document.id
            )
        )
    )
    assert created
    assert [(row.number, row.state, row.trigger) for row in rows] == [
        (1, GenerationState.PENDING, "submitted")
    ]
