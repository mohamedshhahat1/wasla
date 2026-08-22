"""Knowledge base administration endpoints.

Every route is workspace-scoped through the active workspace dependency, so a
knowledge base or document id belonging to another workspace answers not-found.

Reading is open to any member: the people staffing an inbox need to see what
their agents can answer from. Writing is restricted to administrators, because a
document added here is something the AI will state to customers as fact, which
is an authority ordinary members should not hold.

Submitting a document does not ingest it. The response reports a `pending`
document and ingestion happens in the background, because extraction, chunking
and embedding call a provider and no HTTP request should wait on that.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import (
    ActiveWorkspaceDep,
    DocumentSlotDep,
    KnowledgeServiceDep,
    TenantAdminDep,
)
from app.schemas.knowledge import (
    DocumentCreateRequest,
    DocumentRead,
    DocumentSubmission,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseRead,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("/bases", response_model=list[KnowledgeBaseRead])
async def list_knowledge_bases(
    knowledge: KnowledgeServiceDep,
    workspace: ActiveWorkspaceDep,
    limit: LimitQuery = 50,
) -> list[KnowledgeBaseRead]:
    """The knowledge bases this workspace owns."""
    bases = await knowledge.list_knowledge_bases(limit=limit)
    return [KnowledgeBaseRead.from_model(base) for base in bases]


@router.post(
    "/bases",
    response_model=KnowledgeBaseRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base(
    payload: KnowledgeBaseCreateRequest,
    knowledge: KnowledgeServiceDep,
    admin: TenantAdminDep,
) -> KnowledgeBaseRead:
    """Create a knowledge base. Names are unique within a workspace."""
    base = await knowledge.create_knowledge_base(
        name=payload.name,
        description=payload.description,
    )
    return KnowledgeBaseRead.from_model(base)


@router.get("/bases/{knowledge_base_id}", response_model=KnowledgeBaseRead)
async def get_knowledge_base(
    knowledge_base_id: uuid.UUID,
    knowledge: KnowledgeServiceDep,
    workspace: ActiveWorkspaceDep,
) -> KnowledgeBaseRead:
    base = await knowledge.get_knowledge_base(knowledge_base_id)
    return KnowledgeBaseRead.from_model(base)


@router.get(
    "/bases/{knowledge_base_id}/documents",
    response_model=list[DocumentRead],
)
async def list_documents(
    knowledge_base_id: uuid.UUID,
    knowledge: KnowledgeServiceDep,
    workspace: ActiveWorkspaceDep,
    limit: LimitQuery = 50,
) -> list[DocumentRead]:
    """Documents in this knowledge base, most recently added first."""
    documents = await knowledge.list_documents(
        knowledge_base_id=knowledge_base_id,
        limit=limit,
    )
    return [DocumentRead.from_model(document) for document in documents]


@router.post(
    "/bases/{knowledge_base_id}/documents",
    response_model=DocumentSubmission,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_document(
    knowledge_base_id: uuid.UUID,
    payload: DocumentCreateRequest,
    knowledge: KnowledgeServiceDep,
    admin: TenantAdminDep,
    slot: DocumentSlotDep,
) -> DocumentSubmission:
    """Record a document for ingestion.

    Answers `202`, not `201`: the document exists but is not yet retrievable.
    Poll the document until its status is `ready`. Submitting identical text
    twice returns the existing document with `created` false rather than
    duplicating it.
    """
    document, created = await knowledge.submit(
        knowledge_base_id=knowledge_base_id,
        title=payload.title,
        raw=payload.content,
        source=payload.source,
        filename=payload.filename,
        media_type=payload.media_type,
    )
    return DocumentSubmission(document=DocumentRead.from_model(document), created=created)


@router.get("/documents/{document_id}", response_model=DocumentRead)
async def get_document(
    document_id: uuid.UUID,
    knowledge: KnowledgeServiceDep,
    workspace: ActiveWorkspaceDep,
) -> DocumentRead:
    """One document and its ingestion state, including why it failed."""
    document = await knowledge.get_document(document_id)
    return DocumentRead.from_model(document)


@router.post(
    "/documents/{document_id}/ingest",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reingest_document(
    document_id: uuid.UUID,
    knowledge: KnowledgeServiceDep,
    admin: TenantAdminDep,
) -> DocumentRead:
    """Queue a document to be indexed again.

    How a failed ingestion is recovered once its cause is fixed. Safe to call on
    a document that is already ready: re-ingestion replaces its chunks rather
    than appending, so the worst case is some wasted embedding calls.
    """
    document = await knowledge.reingest(document_id)
    return DocumentRead.from_model(document)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # See the note on the logout route: a 204 must declare no model.
    response_model=None,
)
async def delete_document(
    document_id: uuid.UUID,
    knowledge: KnowledgeServiceDep,
    admin: TenantAdminDep,
) -> None:
    """Remove a document and every chunk derived from it.

    A hard delete. A workspace removing a document is usually removing something
    that should no longer be said to customers, and a soft-deleted row that
    retrieval forgot to filter would keep saying it.
    """
    await knowledge.delete_document(document_id)
