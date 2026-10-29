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

**An updated document is replaced, never duplicated** (RQ-06): `PUT
/documents/{id}/content` - or a submission whose source key already exists, with
`replace=true` - indexes the new version beside the old one and swaps them in
one transaction. Listings page by cursor (`next_cursor`, or the `X-Next-Cursor`
header on the two routes that predate it and still answer a bare list).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.dependencies import (
    ActiveWorkspaceDep,
    EntitlementServiceDep,
    KnowledgeServiceDep,
    TenantAdminDep,
)
from app.api.route import CommittingRoute
from app.db.models.billing import LimitKey
from app.db.models.knowledge import DocumentSource, DocumentStatus
from app.schemas.conversation import CursorPage
from app.schemas.knowledge import (
    DocumentCreateRequest,
    DocumentRead,
    DocumentReplaceRequest,
    DocumentSubmission,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseRead,
    KnowledgeBaseUpdateRequest,
)

router = APIRouter(route_class=CommittingRoute, prefix="/knowledge", tags=["knowledge"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]
CursorQuery = Annotated[str | None, Query(max_length=200)]
NEXT_CURSOR_HEADER = "X-Next-Cursor"


@router.get("/bases", response_model=list[KnowledgeBaseRead])
async def list_knowledge_bases(
    knowledge: KnowledgeServiceDep,
    workspace: ActiveWorkspaceDep,
    response: Response,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
) -> list[KnowledgeBaseRead]:
    """The knowledge bases this workspace owns, newest first.

    The next page's cursor, when there is one, is in `X-Next-Cursor`.
    """
    bases, next_cursor = await knowledge.list_knowledge_bases_page(limit=limit, cursor=cursor)
    if next_cursor is not None:
        response.headers[NEXT_CURSOR_HEADER] = next_cursor
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
        actor=admin.user,
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
    response: Response,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
    title: Annotated[str | None, Query(max_length=300)] = None,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    source: DocumentSource | None = None,
) -> list[DocumentRead]:
    """Documents in this knowledge base, most recently added first.

    Every document is reachable: the next page's cursor is in `X-Next-Cursor`
    (RV-13). `title` matches anywhere in the title, case-insensitively.
    """
    documents, next_cursor = await knowledge.list_documents_page(
        knowledge_base_id=knowledge_base_id,
        limit=limit,
        cursor=cursor,
        title=title,
        status=status_filter,
        source=source,
    )
    if next_cursor is not None:
        response.headers[NEXT_CURSOR_HEADER] = next_cursor
    return [DocumentRead.from_view(document) for document in documents]


@router.patch("/bases/{knowledge_base_id}", response_model=KnowledgeBaseRead)
async def update_knowledge_base(
    knowledge_base_id: uuid.UUID,
    payload: KnowledgeBaseUpdateRequest,
    knowledge: KnowledgeServiceDep,
    admin: TenantAdminDep,
) -> KnowledgeBaseRead:
    """Rename a knowledge base or change its description."""
    base = await knowledge.update_knowledge_base(
        knowledge_base_id, name=payload.name, description=payload.description, actor=admin.user
    )
    return KnowledgeBaseRead.from_model(base)


@router.delete(
    "/bases/{knowledge_base_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_knowledge_base(
    knowledge_base_id: uuid.UUID,
    knowledge: KnowledgeServiceDep,
    admin: TenantAdminDep,
    cascade: bool = False,
) -> None:
    """Delete a knowledge base. One that holds documents needs `cascade=true`.

    Refused with `knowledge_base_not_empty` otherwise: deleting it deletes what
    the agents answer from, and that should be said rather than discovered.
    """
    await knowledge.delete_knowledge_base(knowledge_base_id, cascade=cascade, actor=admin.user)


@router.get("/documents", response_model=CursorPage[DocumentRead])
async def list_all_documents(
    knowledge: KnowledgeServiceDep,
    workspace: ActiveWorkspaceDep,
    limit: LimitQuery = 50,
    cursor: CursorQuery = None,
    knowledge_base_id: uuid.UUID | None = None,
    title: Annotated[str | None, Query(max_length=300)] = None,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    source: DocumentSource | None = None,
) -> CursorPage[DocumentRead]:
    """Every document of the workspace, across knowledge bases, newest first (FX-2.4)."""
    documents, next_cursor = await knowledge.list_documents_page(
        knowledge_base_id=knowledge_base_id,
        limit=limit,
        cursor=cursor,
        title=title,
        status=status_filter,
        source=source,
    )
    return CursorPage[DocumentRead](
        items=[DocumentRead.from_view(document) for document in documents],
        next_cursor=next_cursor,
    )


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
    entitlements: EntitlementServiceDep,
    replace: bool = False,
) -> DocumentSubmission:
    """Record a document for ingestion.

    Answers `202`, not `201`: the document exists but is not yet retrievable.
    Poll the document until its status is `ready`. Submitting identical text
    twice returns the existing document with `created` false rather than
    duplicating it, and applies a changed title, filename or media type
    (`metadata_updated`).

    A document whose source key - `source_key`, or the filename - already
    exists is refused with `409 document_source_exists` naming it; with
    `replace=true` its content is replaced instead (RQ-07). Text another
    knowledge base of the workspace already holds is refused with `409
    document_exists_in_other_knowledge_base` (RQ-08).

    The text is extracted before the plan's document limit is consulted, and
    the limit's lock is taken only when a new document is created (RV-10): a
    repeat or a replacement never needs a free slot.
    """

    async def reserve_slot() -> object:
        return await entitlements.reserve_or_refuse(LimitKey.KNOWLEDGE_DOCUMENTS)

    document, created = await knowledge.submit(
        knowledge_base_id=knowledge_base_id,
        title=payload.title,
        raw=payload.content,
        source=payload.source,
        filename=payload.filename,
        media_type=payload.media_type,
        actor=admin.user,
        source_key=payload.source_key,
        replace=replace,
        valid_from=payload.valid_from,
        valid_until=payload.valid_until,
        supersedes_document_id=payload.supersedes_document_id,
        reserve_slot=reserve_slot,
        release_for_extraction=True,
    )
    return DocumentSubmission(document=DocumentRead.from_view(document), created=created)


@router.put(
    "/documents/{document_id}/content",
    response_model=DocumentSubmission,
    status_code=status.HTTP_202_ACCEPTED,
)
async def replace_document_content(
    document_id: uuid.UUID,
    payload: DocumentReplaceRequest,
    knowledge: KnowledgeServiceDep,
    admin: TenantAdminDep,
) -> DocumentSubmission:
    """Replace a document's content with a new version (RQ-06, RV-02).

    The current version keeps serving until the new one is indexed, then the
    two swap in one transaction: never both served, never neither. A failed
    replacement leaves the current version serving and shows the failure on the
    document. The same text as now changes only the title, filename or media
    type (`outcome: metadata_only`). Text another document of the workspace
    holds is refused with `409 document_content_exists`. No plan slot is used.
    """
    document, outcome = await knowledge.replace_content(
        document_id,
        raw=payload.content,
        source=payload.source,
        title=payload.title,
        filename=payload.filename,
        media_type=payload.media_type,
        actor=admin.user,
        release_for_extraction=True,
    )
    return DocumentSubmission(
        document=DocumentRead.from_view(document), created=False, outcome=outcome
    )


@router.get("/documents/{document_id}/similar", response_model=list[DocumentRead])
async def similar_documents(
    document_id: uuid.UUID,
    knowledge: KnowledgeServiceDep,
    workspace: ActiveWorkspaceDep,
) -> list[DocumentRead]:
    """Documents that look like other versions of this one - same source key,
    title or filename - for offering "Replace existing?" (FX-2.4)."""
    return [DocumentRead.from_view(view) for view in await knowledge.similar_documents(document_id)]


@router.get("/documents/{document_id}", response_model=DocumentRead)
async def get_document(
    document_id: uuid.UUID,
    knowledge: KnowledgeServiceDep,
    workspace: ActiveWorkspaceDep,
) -> DocumentRead:
    """One document: whether it serves, and what its latest indexing attempt is doing."""
    document = await knowledge.get_document(document_id)
    return DocumentRead.from_view(document)


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

    How a failed document is retried once its cause is fixed. Safe on a document
    that is ready: it keeps serving its current version until the new one is
    complete, and a failed re-index leaves that version serving. Coalesced - a
    request made while an attempt is already outstanding asks for nothing more.
    """
    document = await knowledge.reindex(document_id, actor=admin.user)
    return DocumentRead.from_view(document)


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
    await knowledge.delete_document(document_id, actor=admin.user)
