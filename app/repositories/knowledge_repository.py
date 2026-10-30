"""Data access for knowledge bases, documents, generations and chunks.

The similarity search lives here for the same reason every other query does: it
is the one place the tenant predicate can be guaranteed. A vector search that
forgot it would return another company's private documents to a customer, which
is the worst failure this system has available to it (ADR-008).
"""

from __future__ import annotations

import base64
import typing
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Table,
    Text,
    and_,
    bindparam,
    case,
    cast,
    delete,
    false,
    func,
    insert,
    literal,
    null,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace
from app.core.exceptions import ConflictError, ValidationError
from app.db.models.agent import Agent, AgentStatus, AgentTool
from app.db.models.enums import TenantStatus
from app.db.models.knowledge import (
    Document,
    DocumentChunk,
    DocumentIndexGeneration,
    DocumentSource,
    DocumentStatus,
    GenerationState,
    KnowledgeBase,
    WorkspaceEmbeddingSpace,
)
from app.db.models.tenant import Tenant
from app.repositories.base import BaseRepository, TenantScopedRepository

# Why a generation exists. Short constants, stored on the row.
# The knowledge tool's name, as `app.agents.registry` registers it; restated so
# the repository layer does not import the agent runtime (a unit test pins the two).
SEARCH_KNOWLEDGE_TOOL: Final = "search_knowledge"

# The most rows any one search query may read, whatever its caller asks.
MAX_SEARCH_ROWS: Final = 50

TRIGGER_SUBMITTED: Final = "submitted"
TRIGGER_RESUBMITTED: Final = "resubmitted"
TRIGGER_REINDEX: Final = "reindex"
TRIGGER_STALE_EMBEDDING: Final = "stale_embedding"
TRIGGER_MIGRATED: Final = "migrated"
# A new version of the document's content (RQ-06): the generation carries it.
TRIGGER_REPLACED: Final = "replaced"
# A shadow generation, built in an embedding-model switch's target space (RQ-25).
TRIGGER_SHADOW: Final = "embedding_switch"
GENERATION_TRIGGERS: Final = frozenset(
    {
        TRIGGER_SUBMITTED,
        TRIGGER_RESUBMITTED,
        TRIGGER_REINDEX,
        TRIGGER_STALE_EMBEDDING,
        TRIGGER_MIGRATED,
        TRIGGER_REPLACED,
        TRIGGER_SHADOW,
    }
)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The content and metadata a replacement generation carries (RQ-06)."""

    content: str
    content_hash: str
    title: str
    filename: str | None
    media_type: str | None
    byte_size: int
    source: DocumentSource
    extraction_warnings: dict[str, int] | None = None
    original_object_key: str | None = None


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of a keyset-ordered listing and the cursor for the next."""

    items: list[T]
    next_cursor: str | None


_ESCAPE: Final = chr(92)


def _contains(value: str) -> str:
    """A LIKE pattern matching `value` anywhere, its wildcards taken literally."""
    for special in (_ESCAPE, "%", "_"):
        value = value.replace(special, _ESCAPE + special)
    return f"%{value}%"


def encode_cursor(moment: datetime, row_id: uuid.UUID) -> str:
    """An opaque position after `(created_at, id)` in newest-first order."""
    raw = f"{moment.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """The position a cursor names; a malformed one is a validation error."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        moment, row_id = base64.urlsafe_b64decode(padded).decode().split("|", 1)
        return datetime.fromisoformat(moment), uuid.UUID(row_id)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError("That cursor is not valid.") from error


def _page[T: (KnowledgeBase, Document)](rows: list[T], limit: int) -> Page[T]:
    if len(rows) <= limit:
        return Page(items=rows, next_cursor=None)
    last = rows[limit - 1]
    return Page(items=rows[:limit], next_cursor=encode_cursor(last.created_at, last.id))


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """A retrieved passage and how close it was.

    `distance` is cosine distance as pgvector reports it: 0 is identical and 2
    is opposite. It is exposed rather than converted to a similarity score
    because the threshold that decides "close enough" is a retrieval policy, and
    policy belongs above the repository.
    """

    chunk: DocumentChunk
    distance: float
    document_title: str
    # When the document's served version was published (FX-2.5): how a model
    # tells a newer source from an older one when they disagree.
    document_updated_at: datetime | None = None
    # The exact-word channel's rank (RQ-15); None for a vector-channel row.
    lexical_rank: float | None = None
    # The document's format: a catalogue row (CSV, XLSX) is a whole answer,
    # never joined with its neighbours (RQ-21, RR-27).
    document_source: DocumentSource | None = None


class KnowledgeBaseRepository(TenantScopedRepository[KnowledgeBase]):
    """Knowledge bases of one workspace."""

    model = KnowledgeBase

    def _tenant_filter(self) -> ColumnElement[bool]:
        return KnowledgeBase.tenant_id == self.tenant_id

    async def get_by_id(self, knowledge_base_id: uuid.UUID) -> KnowledgeBase | None:
        return await self._first(self._select().where(KnowledgeBase.id == knowledge_base_id))

    async def require_by_id(self, knowledge_base_id: uuid.UUID) -> KnowledgeBase:
        return await self._require(self._select().where(KnowledgeBase.id == knowledge_base_id))

    async def get_by_name(self, name: str) -> KnowledgeBase | None:
        return await self._first(self._select().where(KnowledgeBase.name == name))

    async def page(self, *, limit: int, cursor: str | None = None) -> Page[KnowledgeBase]:
        """Knowledge bases newest first, a page at a time (FX-2.4)."""
        query = self._select().order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id.desc())
        if cursor is not None:
            moment, last = decode_cursor(cursor)
            query = query.where(
                or_(
                    KnowledgeBase.created_at < moment,
                    and_(KnowledgeBase.created_at == moment, KnowledgeBase.id < last),
                )
            )
        rows = await self._all(query.limit(limit + 1))
        return _page(rows, limit)

    async def list_all(self, *, limit: int = 50) -> list[KnowledgeBase]:
        return await self._all(self._select().order_by(KnowledgeBase.name).limit(limit))

    async def create(self, *, name: str, description: str | None = None) -> KnowledgeBase:
        """Create a knowledge base, refusing a duplicate name in this workspace.

        The unique constraint is the real guarantee; this check exists to return
        a useful message rather than a driver error.
        """
        if await self.get_by_name(name) is not None:
            raise ConflictError("A knowledge base with that name already exists.")
        return self.add(KnowledgeBase(tenant_id=self.tenant_id, name=name, description=description))


class DocumentRepository(TenantScopedRepository[Document]):
    """Documents of one workspace."""

    model = Document

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Document.tenant_id == self.tenant_id

    async def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        return await self._first(self._select().where(Document.id == document_id))

    async def require_by_id(self, document_id: uuid.UUID) -> Document:
        return await self._require(self._select().where(Document.id == document_id))

    async def lock(self, document_id: uuid.UUID) -> Document | None:
        """The document, row-locked for the rest of this transaction, or None.

        Every writer that changes a document's generations takes this lock
        first, and always before locking a generation, so the claim, the
        publish, a failure, a re-index request and a delete serialise on one
        row in one order and cannot deadlock one another. `populate_existing`
        because the row may already be in this session from an earlier read,
        and the decision about to be made has to be made on the locked version.
        """
        result = await self.session.execute(
            self._select()
            .where(Document.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def get_by_hash(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        content_hash: str,
    ) -> Document | None:
        return await self._first(
            self._select().where(
                Document.knowledge_base_id == knowledge_base_id,
                Document.content_hash == content_hash,
            )
        )

    async def get_by_hash_in_workspace(
        self, *, content_hash: str, exclude_document_id: uuid.UUID | None = None
    ) -> Document | None:
        """Any document of *this* workspace holding exactly this text (RQ-08).

        Never global: a lookup across workspaces would tell one company that
        another holds the same document (G3).
        """
        query = self._select().where(Document.content_hash == content_hash)
        if exclude_document_id is not None:
            query = query.where(Document.id != exclude_document_id)
        return await self._first(query.order_by(Document.created_at, Document.id))

    async def get_by_source_key(
        self, *, knowledge_base_id: uuid.UUID, source_key: str
    ) -> Document | None:
        return await self._first(
            self._select().where(
                Document.knowledge_base_id == knowledge_base_id,
                Document.source_key == source_key,
            )
        )

    async def page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        knowledge_base_id: uuid.UUID | None = None,
        title: str | None = None,
        status: DocumentStatus | None = None,
        source: DocumentSource | None = None,
    ) -> Page[Document]:
        """Documents newest first, one page at a time, optionally filtered (FX-2.4, RV-13)."""
        query = self._select().order_by(Document.created_at.desc(), Document.id.desc())
        if knowledge_base_id is not None:
            query = query.where(Document.knowledge_base_id == knowledge_base_id)
        if title:
            query = query.where(Document.title.ilike(_contains(title), escape=_ESCAPE))
        if status is not None:
            query = query.where(Document.status == status)
        if source is not None:
            query = query.where(Document.source == source)
        if cursor is not None:
            moment, last = decode_cursor(cursor)
            query = query.where(
                or_(
                    Document.created_at < moment,
                    and_(Document.created_at == moment, Document.id < last),
                )
            )
        rows = await self._all(query.limit(limit + 1))
        return _page(rows, limit)

    async def similar(self, document: Document, *, limit: int = 20) -> list[Document]:
        """Documents of this workspace that look like other versions of `document`.

        Same source key, same title (case-insensitively) or same filename - the
        candidates a person means by "replace the old one" (FX-2.4).
        """
        matches = [func.lower(Document.title) == document.title.lower()]
        if document.source_key:
            matches.append(Document.source_key == document.source_key)
        if document.filename:
            matches.append(Document.filename == document.filename)
        return await self._all(
            self._select()
            .where(Document.id != document.id, or_(*matches))
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(limit)
        )

    async def count_in_knowledge_base(self, knowledge_base_id: uuid.UUID) -> int:
        return int(
            await self.session.scalar(
                select(func.count(Document.id)).where(
                    self._tenant_filter(), Document.knowledge_base_id == knowledge_base_id
                )
            )
            or 0
        )

    async def list_for_knowledge_base(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        limit: int = 50,
    ) -> list[Document]:
        return await self._all(
            self._select()
            .where(Document.knowledge_base_id == knowledge_base_id)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(limit)
        )

    async def list_pending(self, *, limit: int = 50) -> list[Document]:
        """Documents with nothing served and an attempt outstanding, oldest first."""
        return await self._all(
            self._select()
            .where(Document.status.in_((DocumentStatus.PENDING, DocumentStatus.PROCESSING)))
            .order_by(Document.created_at, Document.id)
            .limit(limit)
        )

    def create(
        self,
        *,
        knowledge_base_id: uuid.UUID,
        title: str,
        content_hash: str,
        source: DocumentSource = DocumentSource.TEXT,
        filename: str | None = None,
        media_type: str | None = None,
        byte_size: int = 0,
        content: str | None = None,
    ) -> Document:
        return self.add(
            Document(
                tenant_id=self.tenant_id,
                knowledge_base_id=knowledge_base_id,
                title=title,
                source=source,
                status=DocumentStatus.PENDING,
                content_hash=content_hash,
                filename=filename,
                media_type=media_type,
                byte_size=byte_size,
                content=content,
                chunk_count=0,
            )
        )


class GenerationRepository(TenantScopedRepository[DocumentIndexGeneration]):
    """Indexing generations of one workspace's documents."""

    model = DocumentIndexGeneration

    def _tenant_filter(self) -> ColumnElement[bool]:
        return DocumentIndexGeneration.tenant_id == self.tenant_id

    async def in_flight(
        self, *, document_id: uuid.UUID, lock: bool = False
    ) -> DocumentIndexGeneration | None:
        """The one pending or processing generation of a document, if any."""
        statement = self._select().where(
            DocumentIndexGeneration.document_id == document_id,
            DocumentIndexGeneration.state.in_(
                (GenerationState.PENDING, GenerationState.PROCESSING)
            ),
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self._first(statement)

    async def active(
        self, *, document_id: uuid.UUID, lock: bool = False
    ) -> DocumentIndexGeneration | None:
        """The generation a document serves, if any."""
        statement = self._select().where(
            DocumentIndexGeneration.document_id == document_id,
            DocumentIndexGeneration.state == GenerationState.ACTIVE,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self._first(statement)

    async def latest(self, *, document_id: uuid.UUID) -> DocumentIndexGeneration | None:
        """The most recently created generation of a document, a switch's shadows aside.

        A shadow (RQ-25) is the switch's attempt, not the document's: a failed
        shadow is not a failed document, and a document's latest word is never
        one.
        """
        return await self._first(
            self._select()
            .where(
                DocumentIndexGeneration.document_id == document_id,
                DocumentIndexGeneration.trigger != TRIGGER_SHADOW,
            )
            .order_by(DocumentIndexGeneration.number.desc())
        )

    async def shadows(self, *, document_id: uuid.UUID) -> list[DocumentIndexGeneration]:
        """A document's published shadow generations, locked (RQ-25). Normally one."""
        return await self._all(
            self._select()
            .where(
                DocumentIndexGeneration.document_id == document_id,
                DocumentIndexGeneration.state == GenerationState.SHADOW,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def claimed(
        self, *, generation_id: uuid.UUID, token: uuid.UUID
    ) -> DocumentIndexGeneration | None:
        """A generation still held under `token`, locked, or None.

        None is the fencing answer: the generation was deleted with its
        document, superseded, or re-claimed after this holder's lease lapsed.
        Whatever the holder built is no longer wanted.
        """
        result = await self.session.execute(
            self._select()
            .where(
                DocumentIndexGeneration.id == generation_id,
                DocumentIndexGeneration.claim_token == token,
                DocumentIndexGeneration.state == GenerationState.PROCESSING,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def renew(self, *, generation_id: uuid.UUID, token: uuid.UUID, now: datetime) -> bool:
        """Extend a live claim. False if it is no longer this holder's."""
        result = await self.session.execute(
            update(DocumentIndexGeneration)
            .where(
                DocumentIndexGeneration.tenant_id == self.tenant_id,
                DocumentIndexGeneration.id == generation_id,
                DocumentIndexGeneration.claim_token == token,
                DocumentIndexGeneration.state == GenerationState.PROCESSING,
            )
            .values(claimed_at=now)
            .returning(DocumentIndexGeneration.id)
        )
        return result.first() is not None

    async def next_number(self, *, document_id: uuid.UUID) -> int:
        highest = await self.session.scalar(
            select(func.max(DocumentIndexGeneration.number)).where(
                DocumentIndexGeneration.tenant_id == self.tenant_id,
                DocumentIndexGeneration.document_id == document_id,
            )
        )
        return int(highest or 0) + 1

    def create(
        self,
        *,
        document_id: uuid.UUID,
        number: int,
        trigger: str,
        snapshot: Snapshot | None = None,
    ) -> DocumentIndexGeneration:
        if trigger not in GENERATION_TRIGGERS:
            raise ValueError(f"unknown generation trigger {trigger!r}")
        generation = DocumentIndexGeneration(
            tenant_id=self.tenant_id,
            document_id=document_id,
            number=number,
            state=GenerationState.PENDING,
            trigger=trigger,
            attempts=0,
            chunk_count=0,
        )
        if snapshot is not None:
            generation.content = snapshot.content
            generation.content_hash = snapshot.content_hash
            generation.title = snapshot.title
            generation.filename = snapshot.filename
            generation.media_type = snapshot.media_type
            generation.byte_size = snapshot.byte_size
            generation.source = snapshot.source
            generation.extraction_warnings = snapshot.extraction_warnings
            generation.original_object_key = snapshot.original_object_key
        return self.add(generation)

    async def original_keys(self, *, document_id: uuid.UUID) -> list[str]:
        """Every original file a document's generations hold in the object store."""
        rows = await self.session.scalars(
            select(DocumentIndexGeneration.original_object_key).where(
                self._tenant_filter(),
                DocumentIndexGeneration.document_id == document_id,
                DocumentIndexGeneration.original_object_key.is_not(None),
            )
        )
        return sorted({key for key in rows if key})


class DocumentChunkRepository(TenantScopedRepository[DocumentChunk]):
    """Chunks of one workspace, and the similarity search over them."""

    model = DocumentChunk

    def _tenant_filter(self) -> ColumnElement[bool]:
        return DocumentChunk.tenant_id == self.tenant_id

    async def list_for_document(self, *, document_id: uuid.UUID) -> list[DocumentChunk]:
        """The chunks a document serves: its active generation's, in order."""
        return await self._all(
            self._select()
            .join(
                DocumentIndexGeneration,
                and_(
                    DocumentIndexGeneration.id == DocumentChunk.generation_id,
                    DocumentIndexGeneration.state == GenerationState.ACTIVE,
                ),
            )
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.ordinal)
        )

    async def count_for_document(self, *, document_id: uuid.UUID) -> int:
        """Every chunk a document owns, in any generation."""
        result = await self.session.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(
                DocumentChunk.tenant_id == self.tenant_id,
                DocumentChunk.document_id == document_id,
            )
        )
        return int(result.scalar_one())

    async def clear_for_document(self, *, document_id: uuid.UUID) -> None:
        """Delete every chunk of one document, whatever generation owns it."""
        await self.session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.tenant_id == self.tenant_id,
                DocumentChunk.document_id == document_id,
            )
        )

    async def clear_for_generation(self, *, generation_id: uuid.UUID) -> None:
        """Delete one generation's chunks, which is how a superseded one retires.

        A generation is replaced wholesale rather than merged: a document whose
        text or chunking changed has different boundaries, so matching old chunks
        to new ones is guesswork, and a stale chunk left behind would be
        retrievable text that no longer appears in the source.
        """
        await self.session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.tenant_id == self.tenant_id,
                DocumentChunk.generation_id == generation_id,
            )
        )

    def add_chunk(
        self,
        *,
        document_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        generation_id: uuid.UUID,
        ordinal: int,
        content: str,
        token_estimate: int,
        embedding: list[float] | None,
        heading_path: str | None = None,
        search_text: str | None = None,
        lexemes: Sequence[str] | None = None,
    ) -> DocumentChunk:
        return self.add(
            DocumentChunk(
                tenant_id=self.tenant_id,
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
                generation_id=generation_id,
                ordinal=ordinal,
                content=content,
                token_estimate=token_estimate,
                embedding=embedding,
                heading_path=heading_path,
                search_text=search_text,
                # Lexemes as given, not re-parsed: `'simple'` would split an
                # identifier like `bk-35` (RR-16).
                search_tsv=(
                    func.array_to_tsvector(cast(list(lexemes), ARRAY(Text))) if lexemes else None
                ),
            )
        )

    async def add_chunks(
        self,
        *,
        document_id: uuid.UUID,
        knowledge_base_id: uuid.UUID,
        generation_id: uuid.UUID,
        rows: Sequence[NewChunk],
    ) -> None:
        """One generation's chunks in one multi-row insert (FX-4.8).

        The lexemes travel as one string per row, joined on a separator no
        lexeme holds, and become the `tsvector` in the statement - so every row
        binds the same SQL and the driver sends them together. Not re-parsed by
        `'simple'`, which would split `bk-35` (RR-16).
        """
        if not rows:
            return
        table = typing.cast(Table, DocumentChunk.__table__)
        lexemes = func.array_to_tsvector(
            func.string_to_array(bindparam("lexemes_joined", type_=Text), LEXEME_SEPARATOR)
        )
        statement = insert(table).values(
            search_tsv=case(
                (bindparam("lexemes_joined", type_=Text).is_(None), null()), else_=lexemes
            )
        )
        await self.session.execute(
            statement,
            [
                {
                    "id": uuid.uuid4(),
                    "tenant_id": self.tenant_id,
                    "document_id": document_id,
                    "knowledge_base_id": knowledge_base_id,
                    "generation_id": generation_id,
                    "ordinal": row.ordinal,
                    "content": row.content,
                    "token_estimate": row.token_estimate,
                    "embedding": row.embedding,
                    "heading_path": row.heading_path,
                    "search_text": row.search_text,
                    "lexemes_joined": (LEXEME_SEPARATOR.join(row.lexemes) if row.lexemes else None),
                }
                for row in rows
            ],
        )

    def served_predicates(
        self,
        *,
        space: EmbeddingSpace,
        knowledge_base_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[ColumnElement[bool]]:
        """Every predicate a served passage satisfies, for every query that reads one.

        **One builder for the vector, lexical and neighbour queries** (RQ-15):
        a second query path that wrote its own list could forget one of these,
        and the one it forgot would be the leak or the stale version. The
        statement must join `DocumentIndexGeneration` and `Document` on the
        chunk, which `_served_select` does.

        - `tenant_id` on the chunk, the generation *and* the document. Any one
          is sufficient, so removing one is safe and removing all is a
          cross-tenant leak; the suite is written against the property, which
          is why this note exists for whoever is tempted to delete "the
          redundant filter".
        - the generation `ACTIVE`, so a generation still building, one that
          failed and one that was replaced contribute nothing (PD-RAG-1).
        - the generation's embedding space equal to the query's (RAG-06).
        - a non-null embedding.
        - the document inside its effective dates (RQ-24).
        - the agent's knowledge scope (RQ-18), when it has one. An empty scope
          matches nothing; it is refused when the grant is saved, and this is
          the backstop.
        """
        predicates: list[ColumnElement[bool]] = [
            DocumentChunk.tenant_id == self.tenant_id,
            DocumentIndexGeneration.tenant_id == self.tenant_id,
            Document.tenant_id == self.tenant_id,
            DocumentIndexGeneration.state == GenerationState.ACTIVE,
            DocumentIndexGeneration.embedding_provider == space.provider,
            DocumentIndexGeneration.embedding_model == space.model,
            DocumentIndexGeneration.embedding_dimensions == space.dimensions,
            DocumentIndexGeneration.embedding_schema_version == space.schema_version,
            DocumentChunk.embedding.is_not(None),
            or_(Document.valid_from.is_(None), Document.valid_from <= func.now()),
            or_(Document.valid_until.is_(None), Document.valid_until > func.now()),
        ]
        if knowledge_base_ids is not None:
            predicates.append(
                DocumentChunk.knowledge_base_id.in_(list(knowledge_base_ids))
                if knowledge_base_ids
                else false()
            )
        return predicates

    @staticmethod
    def _served_select(*columns: Any) -> Any:
        return (
            select(*columns)
            .join(
                DocumentIndexGeneration,
                DocumentIndexGeneration.id == DocumentChunk.generation_id,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
        )

    async def search(
        self,
        *,
        embedding: list[float],
        space: EmbeddingSpace,
        limit: int = 5,
        knowledge_base_id: uuid.UUID | None = None,
        knowledge_base_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[ScoredChunk]:
        """Nearest chunks to an embedding, within this workspace.

        The filters are `served_predicates`, and none of them is optional.

        Ordering is by cosine distance, which suits normalised embeddings such
        as OpenAI's and is what `ix_document_chunks_embedding_hnsw` is built
        for. The caller bounds `limit`; so does this, because an unbounded
        `LIMIT` from a mistaken caller would be an unbounded read.
        """
        await self._prepare_the_planner()
        distance = DocumentChunk.embedding.cosine_distance(embedding)
        scope = knowledge_base_ids
        if knowledge_base_id is not None:
            scope = (
                [knowledge_base_id]
                if scope is None
                else [kb for kb in scope if kb == knowledge_base_id]
            )
        query = (
            self._served_select(
                DocumentChunk,
                distance.label("distance"),
                Document.title,
                Document.ingested_at,
                Document.source,
            )
            .where(*self.served_predicates(space=space, knowledge_base_ids=scope))
            .order_by(distance)
            .limit(max(1, min(limit, MAX_SEARCH_ROWS)))
        )
        result = await self.session.execute(query)
        return [
            ScoredChunk(
                chunk=chunk,
                distance=float(value),
                document_title=title,
                document_updated_at=updated,
                document_source=source,
            )
            for chunk, value, title, updated, source in result.all()
        ]

    async def lexical_search(
        self,
        *,
        tsquery: str,
        embedding: list[float],
        space: EmbeddingSpace,
        limit: int = 20,
        knowledge_base_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[ScoredChunk]:
        """Chunks holding the query's exact words, within this workspace (RQ-15).

        The same `served_predicates` as the vector search - the same builder,
        so the two cannot drift - plus a match on the chunk's lexemes
        (`search_tsv`, GIN-indexed). Ranked by `ts_rank`; each row also carries
        its cosine distance to the query, so the keep rule and the tiers judge
        a lexical hit on the same scale as a vector one.

        `tsquery` is bound as a parameter and cast, never interpolated: it is an
        OR of quoted lexemes `retrieval_ranking.tsquery_text` built.
        """
        query_value = cast(literal(tsquery, Text), TSQUERY)
        rank = func.ts_rank(DocumentChunk.search_tsv, query_value)
        distance = DocumentChunk.embedding.cosine_distance(embedding)
        query = (
            self._served_select(
                DocumentChunk,
                distance.label("distance"),
                Document.title,
                Document.ingested_at,
                rank.label("rank"),
                Document.source,
            )
            .where(
                *self.served_predicates(space=space, knowledge_base_ids=knowledge_base_ids),
                DocumentChunk.search_tsv.op("@@")(query_value),
            )
            .order_by(rank.desc(), distance, DocumentChunk.id)
            .limit(max(1, min(limit, MAX_SEARCH_ROWS)))
        )
        result = await self.session.execute(query)
        return [
            ScoredChunk(
                chunk=chunk,
                distance=float(value),
                document_title=title,
                document_updated_at=updated,
                lexical_rank=float(score),
                document_source=source,
            )
            for chunk, value, title, updated, score, source in result.all()
        ]

    async def neighbours(
        self,
        *,
        generation_id: uuid.UUID,
        ordinals: Sequence[int],
        space: EmbeddingSpace,
        knowledge_base_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[DocumentChunk]:
        """Chunks of one served generation at the given ordinals (FX-3.11).

        Under `served_predicates`, so a neighbour of a passage is served on
        exactly the terms the passage was: same workspace, an active generation
        - this one, never another generation of the same document - its space,
        the scope and the validity window. Exact lookups on
        `uq_document_chunks_tenant_id_generation_id_ordinal`.
        """
        if not ordinals:
            return []
        query = (
            self._served_select(DocumentChunk)
            .where(
                *self.served_predicates(space=space, knowledge_base_ids=knowledge_base_ids),
                DocumentChunk.generation_id == generation_id,
                DocumentChunk.ordinal.in_(list(ordinals)),
            )
            .order_by(DocumentChunk.ordinal)
        )
        return list((await self.session.scalars(query)).all())

    async def neighbours_of(
        self,
        *,
        wanted: Sequence[tuple[uuid.UUID, int]],
        space: EmbeddingSpace,
        knowledge_base_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[DocumentChunk]:
        """`neighbours` for several passages in one round trip (FX-4.7, RR-25).

        `wanted` is `(generation_id, ordinal)` pairs; the same
        `served_predicates`, so a neighbour is served on exactly the terms its
        passage was.
        """
        if not wanted:
            return []
        query = (
            self._served_select(DocumentChunk)
            .where(
                *self.served_predicates(space=space, knowledge_base_ids=knowledge_base_ids),
                tuple_(DocumentChunk.generation_id, DocumentChunk.ordinal).in_(list(wanted)),
            )
            .order_by(DocumentChunk.generation_id, DocumentChunk.ordinal)
        )
        return list((await self.session.scalars(query)).all())

    async def _prepare_the_planner(self) -> None:
        """Two settings without which the index below is worse than useless.

        Both are `SET LOCAL`, so they last for this unit of work rather than
        for whatever the pooled connection is asked to do next, and both cost
        one round trip against a retrieval they turn from hundreds of
        milliseconds into single digits (ADR-079).

        **`hnsw.iterative_scan` stops the index answering with fewer passages
        than were asked for.** None of the filters above is in the vector
        index - pgvector indexes one column - so on the approximate path they
        are applied *after* it. By default the scan visits `ef_search`
        candidates in global distance order, discards the ones belonging to
        other workspaces, and answers with whatever survived. For a workspace
        holding a small share of the corpus that is reliably nothing: the
        measured case returned **zero passages out of five** for a workspace
        with 200 chunks, and the agent was then told, as far as it could tell
        truthfully, that the knowledge base had no answer. `strict_order`
        makes the scan resume instead of stopping, and - unlike
        `relaxed_order` - hands the rows back in distance order, which is what
        the `ORDER BY` above promises its caller.

        **`plan_cache_mode` stops PostgreSQL settling on a plan built without
        knowing which workspace is asking.** This statement is prepared once
        per pooled connection and PostgreSQL compares a generic plan against
        the custom ones after five executions. The generic plan cannot know the
        tenant, so it estimates the filter from the average workspace, decides
        a nested loop over every document is cheap, and is kept for the life of
        the connection. Measured through this repository on a 45,000-chunk
        workspace: searches one to five took 7ms, and every search after that
        took 250ms - the *same* query, on the same connection, having simply
        been run often enough. Forcing a custom plan puts the workspace's real
        size back in front of the planner, which is the whole basis on which it
        chooses the approximate index at all.
        """
        # `set_config(..., is_local => true)` rather than two `SET LOCAL`
        # statements: it means the same thing and fits in one round trip, and a
        # prepared statement may carry only one command.
        await self.session.execute(
            text(
                "SELECT set_config('hnsw.iterative_scan', 'strict_order', true),"
                " set_config('plan_cache_mode', 'force_custom_plan', true)"
            )
        )


# Joins one chunk's lexemes for `add_chunks`: a control character, which
# `for_lexical` text never holds.
LEXEME_SEPARATOR: Final = "\x1f"


@dataclass(frozen=True, slots=True)
class NewChunk:
    """One chunk to insert with `DocumentChunkRepository.add_chunks`."""

    ordinal: int
    content: str
    token_estimate: int
    embedding: list[float]
    heading_path: str | None
    search_text: str | None
    lexemes: tuple[str, ...]


def space_of(provider: str, model: str, dimensions: int, schema_version: int) -> EmbeddingSpace:
    return EmbeddingSpace(
        provider=provider, model=model, dimensions=dimensions, schema_version=schema_version
    )


def generation_space(generation: DocumentIndexGeneration) -> EmbeddingSpace | None:
    """The space a generation's vectors were made in, once it has been claimed."""
    if (
        generation.embedding_provider is None
        or generation.embedding_model is None
        or generation.embedding_dimensions is None
        or generation.embedding_schema_version is None
    ):
        return None
    return space_of(
        generation.embedding_provider,
        generation.embedding_model,
        generation.embedding_dimensions,
        generation.embedding_schema_version,
    )


def query_space_of(row: WorkspaceEmbeddingSpace | None, default: EmbeddingSpace) -> EmbeddingSpace:
    """The space a workspace's searches embed in: its row's, else the deployment's."""
    if row is None:
        return default
    return space_of(
        row.query_provider, row.query_model, row.query_dimensions, row.query_schema_version
    )


def target_space_of(row: WorkspaceEmbeddingSpace | None) -> EmbeddingSpace | None:
    """The space a workspace's switch is building in, while one is under way."""
    if (
        row is None
        or row.target_provider is None
        or row.target_model is None
        or row.target_dimensions is None
        or row.target_schema_version is None
    ):
        return None
    return space_of(
        row.target_provider, row.target_model, row.target_dimensions, row.target_schema_version
    )


class EmbeddingSpaceRepository(TenantScopedRepository[WorkspaceEmbeddingSpace]):
    """One workspace's query space and its embedding-model switch (RQ-25)."""

    model = WorkspaceEmbeddingSpace

    def _tenant_filter(self) -> ColumnElement[bool]:
        return WorkspaceEmbeddingSpace.tenant_id == self.tenant_id

    async def row(self, *, lock: str | None = None) -> WorkspaceEmbeddingSpace | None:
        """The workspace's row; `lock` is `share` (a publish) or `update` (a flip).

        The lock order every writer keeps: this row, then the document, then
        its generations. A publish takes it shared, so publishes do not wait
        for one another; a flip takes it exclusively, so no publish lands in
        the space being left while the flip is deciding.
        """
        statement = self._select()
        if lock == "share":
            statement = statement.with_for_update(read=True)
        elif lock == "update":
            statement = statement.with_for_update()
        if lock is not None:
            statement = statement.execution_options(populate_existing=True)
        return await self._first(statement)

    async def query_space(self, default: EmbeddingSpace) -> EmbeddingSpace:
        """The space this workspace's searches embed in."""
        return query_space_of(await self.row(), default)

    def create(self, *, query: EmbeddingSpace) -> WorkspaceEmbeddingSpace:
        return self.add(
            WorkspaceEmbeddingSpace(
                tenant_id=self.tenant_id,
                query_provider=query.provider,
                query_model=query.model,
                query_dimensions=query.dimensions,
                query_schema_version=query.schema_version,
            )
        )


@dataclass(frozen=True, slots=True)
class DueGeneration:
    """One generation the recovery sweep may hand to a worker."""

    tenant_id: uuid.UUID
    document_id: uuid.UUID
    generation_id: uuid.UUID
    state: GenerationState
    attempts: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IndexingBacklog:
    """What the gauges report about indexing across every workspace."""

    unindexed: int = 0
    oldest_unindexed_seconds: float = 0.0
    processing: int = 0
    oldest_processing_seconds: float = 0.0
    retry_waiting: int = 0
    serving: int = 0
    failed: int = 0
    exhausted: int = 0
    stale_embedding: int = 0
    # Ready documents beyond the first with the same text in one workspace
    # (RV-07): each is a passage served twice. Tenant-free, a total.
    duplicate_documents: int = 0
    # Served workspaces with ready documents and no answering agent able to
    # search them (RC-7).
    unreachable_workspaces: int = 0
    # Across workspaces with an embedding-model switch under way (RQ-25): ready
    # documents holding a shadow of their current text, over ready documents.
    # 1.0 when no switch is under way.
    switch_coverage: float = 1.0
    # Shadow generations by state: pending, processing, shadow (published),
    # failed.
    shadow_generations: dict[str, int] = field(default_factory=dict)
    # Ready documents whose extraction flagged lines it may have re-ordered
    # (RQ-04, FX-4.4).
    bidi_warning_documents: int = 0


def _served_workspace() -> ColumnElement[bool]:
    """A workspace that is active and not tombstoned (PD-RAG-2, PD-RAG-3)."""
    return and_(Tenant.status == TenantStatus.ACTIVE, Tenant.deleted_at.is_(None))


class IndexingSweep(BaseRepository[DocumentIndexGeneration]):
    """The unscoped reads over indexing work outstanding across every workspace.

    Deliberately not workspace-scoped, and deliberately its own class so that is
    visible - the same exception, for the same reason, that `InboundEventSweep`
    makes over the inbound log. A backlog is a platform-wide condition: the Redis
    outage or the provider outage that produced it did not choose a workspace.

    Nothing here is reachable from an API route. The callers are
    `IngestionRecoveryWorker`, the metrics exposition and the operator commands,
    and all of them are counting or finishing work rather than answering a
    person. None of them returns a document's content.
    """

    model = DocumentIndexGeneration

    async def claim_due(
        self,
        *,
        now: datetime,
        unenqueued_before: datetime,
        lease_expired_before: datetime,
        max_attempts: int,
        limit: int,
        requeue_before: datetime | None = None,
    ) -> list[DueGeneration]:
        """Generations owed a worker, locked so one sweeper publishes each.

        Eligible, and nothing else (RAG-01, RAG-10):

        - a `PENDING` generation never retried, older than the grace period -
          the upload committed and its job never reached Redis;
        - a `PENDING` generation whose `next_retry_at` has passed - a transient
          failure whose backoff is over;
        - a `PROCESSING` generation whose lease has lapsed - a worker died
          holding it. Eligible *whatever* its attempt count, so the claim that
          follows can end it as exhausted rather than leaving it held for ever.

        And only in a workspace that is active and not deleted. A suspended
        workspace's outstanding work waits, unspent, until it is reactivated; a
        deleted one's waits for the purge. A `FAILED` generation is never
        eligible - only a person asking for a re-index starts a new one.

        And not one this sweep already queued since `requeue_before` (FX-1.9,
        RV-18): a job that is on the queue, or was taken and is still within its
        visibility timeout, is coming; queueing it again every minute only grew
        the queue by up to a batch a minute during a backlog.

        `FOR UPDATE OF ... SKIP LOCKED`: two sweepers divide the backlog rather
        than both publishing it. Duplicates would be harmless - the claim admits
        one worker per generation - but they would double queue depth during the
        recovery that is already behind.
        """
        pending = DocumentIndexGeneration.state == GenerationState.PENDING
        recently_queued = (
            and_(
                DocumentIndexGeneration.last_enqueued_at.is_not(None),
                DocumentIndexGeneration.last_enqueued_at >= requeue_before,
            )
            if requeue_before is not None
            else false()
        )
        rows = await self.session.execute(
            select(
                DocumentIndexGeneration.tenant_id,
                DocumentIndexGeneration.document_id,
                DocumentIndexGeneration.id,
                DocumentIndexGeneration.state,
                DocumentIndexGeneration.attempts,
                DocumentIndexGeneration.created_at,
            )
            .join(Tenant, Tenant.id == DocumentIndexGeneration.tenant_id)
            .where(
                _served_workspace(),
                or_(
                    and_(
                        pending,
                        DocumentIndexGeneration.attempts < max_attempts,
                        or_(
                            and_(
                                DocumentIndexGeneration.next_retry_at.is_(None),
                                DocumentIndexGeneration.created_at < unenqueued_before,
                            ),
                            DocumentIndexGeneration.next_retry_at <= now,
                        ),
                    ),
                    and_(
                        DocumentIndexGeneration.state == GenerationState.PROCESSING,
                        DocumentIndexGeneration.claimed_at < lease_expired_before,
                    ),
                ),
                ~recently_queued,
            )
            .order_by(DocumentIndexGeneration.created_at, DocumentIndexGeneration.id)
            .limit(limit)
            .with_for_update(of=DocumentIndexGeneration, skip_locked=True)
        )
        return [
            DueGeneration(
                tenant_id=tenant_id,
                document_id=document_id,
                generation_id=generation_id,
                state=state,
                attempts=attempts,
                created_at=created_at,
            )
            for tenant_id, document_id, generation_id, state, attempts, created_at in rows.all()
        ]

    async def list_outstanding(self, *, limit: int) -> list[DocumentIndexGeneration]:
        """In-flight generations, oldest first, for an operator. A plain read."""
        return await self._all(
            self._select()
            .where(
                DocumentIndexGeneration.state.in_(
                    (GenerationState.PENDING, GenerationState.PROCESSING)
                )
            )
            .order_by(DocumentIndexGeneration.created_at, DocumentIndexGeneration.id)
            .limit(limit)
        )

    async def mark_enqueued(self, generation_ids: list[uuid.UUID], *, now: datetime) -> None:
        """Record that the sweep queued these generations (FX-1.9)."""
        if generation_ids:
            await self.session.execute(
                update(DocumentIndexGeneration)
                .where(DocumentIndexGeneration.id.in_(generation_ids))
                .values(last_enqueued_at=now)
            )

    async def list_stale(
        self,
        *,
        space: EmbeddingSpace,
        limit: int,
        after: str | None = None,
    ) -> list[DocumentIndexGeneration]:
        """Active generations made in a different embedding space (RAG-06), a page at a time.

        Not a document that already has an attempt in flight - it is being
        re-indexed - and not one whose latest attempt since this one published
        has failed - re-trying it is a decision about that document, not part of
        a sweep (FX-1.8, RV-12). Ordered by `(published_at, id)`; `after` is the
        cursor of the last row of the previous page, so consecutive runs select
        disjoint sets.
        """
        newer = DocumentIndexGeneration.__table__.alias("newer_generation")
        query = (
            self._select()
            .outerjoin(
                WorkspaceEmbeddingSpace,
                WorkspaceEmbeddingSpace.tenant_id == DocumentIndexGeneration.tenant_id,
            )
            .where(
                DocumentIndexGeneration.state == GenerationState.ACTIVE,
                _not_in_query_space(space),
                ~select(newer.c.id)
                .where(
                    newer.c.document_id == DocumentIndexGeneration.document_id,
                    newer.c.number > DocumentIndexGeneration.number,
                    newer.c.state.in_(
                        (
                            GenerationState.PENDING,
                            GenerationState.PROCESSING,
                            GenerationState.FAILED,
                        )
                    ),
                )
                .exists(),
            )
            .order_by(DocumentIndexGeneration.published_at, DocumentIndexGeneration.id)
        )
        if after is not None:
            moment, last = decode_cursor(after)
            query = query.where(
                or_(
                    DocumentIndexGeneration.published_at > moment,
                    and_(
                        DocumentIndexGeneration.published_at == moment,
                        DocumentIndexGeneration.id > last,
                    ),
                )
            )
        return await self._all(query.limit(limit))

    async def backlog(
        self,
        *,
        now: datetime,
        unindexed_before: datetime,
        space: EmbeddingSpace | None = None,
    ) -> IndexingBacklog:
        """The counts the gauges and alerts read, in one round trip each.

        No labels come out of here but states: which workspace is a question
        for the operator command, never a time series per customer.
        """
        generation = DocumentIndexGeneration
        pending = generation.state == GenerationState.PENDING
        processing = generation.state == GenerationState.PROCESSING
        # Documents with nothing served, waiting longer than the grace period.
        # The table is read twice - the attempt and whether the document is
        # already serving something - so both sides are aliased and neither can
        # be mistaken for the other.
        outer = DocumentIndexGeneration.__table__.alias("outer_generation")
        inner = DocumentIndexGeneration.__table__.alias("inner_generation")
        # Only in served workspaces: a suspended workspace's queue is paused on
        # purpose (PD-RAG-2), and an alert on it would fire for as long as the
        # suspension lasts.
        unindexed = (
            select(func.count(outer.c.id), func.min(outer.c.created_at))
            .select_from(outer.join(Tenant, Tenant.id == outer.c.tenant_id))
            .where(_served_workspace())
        ).where(
            outer.c.state.in_((GenerationState.PENDING, GenerationState.PROCESSING)),
            outer.c.created_at < unindexed_before,
            ~select(inner.c.id)
            .where(
                inner.c.document_id == outer.c.document_id,
                inner.c.state == GenerationState.ACTIVE,
            )
            .exists(),
        )
        count, oldest = (await self.session.execute(unindexed)).one()

        held = (
            await self.session.execute(
                select(func.count(generation.id), func.min(generation.claimed_at)).where(processing)
            )
        ).one()
        waiting = await self.session.scalar(
            select(func.count(generation.id)).where(pending, generation.next_retry_at.is_not(None))
        )
        serving = await self.session.scalar(
            select(func.count(generation.id)).where(generation.state == GenerationState.ACTIVE)
        )
        # Failed attempts that are still a document's latest word: a later
        # generation of any kind means somebody already asked again.
        latest_failed = (
            select(outer.c.last_error_code)
            .where(
                outer.c.state == GenerationState.FAILED,
                ~select(inner.c.id)
                .where(
                    inner.c.document_id == outer.c.document_id,
                    inner.c.number > outer.c.number,
                )
                .exists(),
            )
            .subquery()
        )
        failures = (
            await self.session.execute(
                select(
                    func.count().filter(latest_failed.c.last_error_code == "retry_exhausted"),
                    func.count(),
                ).select_from(latest_failed)
            )
        ).one()
        stale = 0
        if space is not None:
            stale = int(
                await self.session.scalar(
                    select(func.count(generation.id))
                    .outerjoin(
                        WorkspaceEmbeddingSpace,
                        WorkspaceEmbeddingSpace.tenant_id == generation.tenant_id,
                    )
                    .where(generation.state == GenerationState.ACTIVE, _not_in_query_space(space))
                )
                or 0
            )
        switching = await self.session.execute(
            select(
                func.count(Document.id),
                func.count(Document.id).filter(
                    select(generation.id)
                    .where(
                        generation.document_id == Document.id,
                        generation.state == GenerationState.SHADOW,
                        generation.content_hash == Document.content_hash,
                    )
                    .exists()
                ),
            )
            .join(WorkspaceEmbeddingSpace, WorkspaceEmbeddingSpace.tenant_id == Document.tenant_id)
            .where(
                WorkspaceEmbeddingSpace.target_model.is_not(None),
                Document.status == DocumentStatus.READY,
            )
        )
        switch_ready, switch_covered = switching.one()
        shadow_rows = await self.session.execute(
            select(generation.state, func.count(generation.id))
            .where(
                generation.trigger == TRIGGER_SHADOW,
                generation.state.in_(
                    (
                        GenerationState.PENDING,
                        GenerationState.PROCESSING,
                        GenerationState.SHADOW,
                        GenerationState.FAILED,
                    )
                ),
            )
            .group_by(generation.state)
        )
        shadows = {str(state): int(count) for state, count in shadow_rows.all()}
        bidi = int(
            await self.session.scalar(
                select(func.count(Document.id)).where(
                    Document.status == DocumentStatus.READY,
                    Document.extraction_warnings.is_not(None),
                )
            )
            or 0
        )

        copies = (
            select(func.count().label("copies"))
            .select_from(Document)
            .where(Document.status == DocumentStatus.READY)
            .group_by(Document.tenant_id, Document.content_hash)
            .having(func.count() > 1)
            .subquery()
        )
        duplicates = await self.session.scalar(select(func.sum(copies.c.copies - 1)))
        unreachable = await self.session.scalar(
            select(func.count(func.distinct(Document.tenant_id)))
            .select_from(Document)
            .join(Tenant, Tenant.id == Document.tenant_id)
            .where(_served_workspace(), Document.status == DocumentStatus.READY)
            .where(~_answering_agent_can_search(Document.tenant_id))
        )

        def age(moment: datetime | None) -> float:
            if moment is None:
                return 0.0
            return max((now - moment).total_seconds(), 0.0)

        exhausted, failed = int(failures[0] or 0), int(failures[1] or 0)
        return IndexingBacklog(
            unindexed=int(count or 0),
            oldest_unindexed_seconds=age(oldest) if count else 0.0,
            processing=int(held[0] or 0),
            oldest_processing_seconds=age(held[1]) if held[0] else 0.0,
            retry_waiting=int(waiting or 0),
            serving=int(serving or 0),
            failed=failed - exhausted,
            exhausted=exhausted,
            stale_embedding=stale,
            duplicate_documents=int(duplicates or 0),
            unreachable_workspaces=int(unreachable or 0),
            switch_coverage=(
                float(switch_covered or 0) / float(switch_ready) if switch_ready else 1.0
            ),
            shadow_generations=shadows,
            bidi_warning_documents=bidi,
        )


async def knowledge_unreachable(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Whether this workspace has ready documents no answering agent can search (RC-7).

    Scoped by `tenant_id` in both halves: the documents and the grant.
    """
    found = await session.scalar(
        select(
            select(Document.id)
            .where(Document.tenant_id == tenant_id, Document.status == DocumentStatus.READY)
            .exists()
            & ~_answering_agent_can_search(tenant_id)
        )
    )
    return bool(found)


def _answering_agent_can_search(tenant_id: Any) -> ColumnElement[bool]:
    """Whether the workspace's answering agent holds an enabled knowledge grant (RC-7).

    Only the ACTIVE agent marked default answers a workspace (`plan_turn`), so
    its grant - not any agent's - decides whether knowledge is reachable.
    """
    return (
        select(Agent.id)
        .join(AgentTool, AgentTool.agent_id == Agent.id)
        .where(
            Agent.tenant_id == tenant_id,
            AgentTool.tenant_id == tenant_id,
            Agent.status == AgentStatus.ACTIVE,
            Agent.is_default.is_(True),
            AgentTool.name == SEARCH_KNOWLEDGE_TOOL,
            AgentTool.enabled.is_(True),
        )
        .exists()
    )


def _not_in_query_space(default: EmbeddingSpace) -> ColumnElement[bool]:
    """A generation outside the space its workspace is queried in (RQ-25, RR-09).

    The workspace's own query space when it has a row - joined by the caller -
    else the deployment's. A workspace mid-switch is queried in its old space,
    so its active generations are not stale while its shadows build.
    """
    row = WorkspaceEmbeddingSpace
    return or_(
        and_(row.tenant_id.is_(None), _not_in(default)),
        and_(
            row.tenant_id.is_not(None),
            or_(
                DocumentIndexGeneration.embedding_provider != row.query_provider,
                DocumentIndexGeneration.embedding_model != row.query_model,
                DocumentIndexGeneration.embedding_dimensions != row.query_dimensions,
                DocumentIndexGeneration.embedding_schema_version != row.query_schema_version,
            ),
        ),
    )


def _not_in(space: EmbeddingSpace) -> ColumnElement[bool]:
    return or_(
        DocumentIndexGeneration.embedding_provider != space.provider,
        DocumentIndexGeneration.embedding_model != space.model,
        DocumentIndexGeneration.embedding_dimensions != space.dimensions,
        DocumentIndexGeneration.embedding_schema_version != space.schema_version,
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "GENERATION_TRIGGERS",
    "TRIGGER_MIGRATED",
    "TRIGGER_REINDEX",
    "TRIGGER_RESUBMITTED",
    "TRIGGER_STALE_EMBEDDING",
    "TRIGGER_SUBMITTED",
    "DocumentChunkRepository",
    "DocumentRepository",
    "DueGeneration",
    "GenerationRepository",
    "IndexingBacklog",
    "IndexingSweep",
    "KnowledgeBaseRepository",
    "ScoredChunk",
    "utcnow",
]
