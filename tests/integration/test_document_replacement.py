"""An updated document is replaced, never duplicated (RV-02, RQ-06, P-R05).

What is asserted is what `RetrievalService.search` returns over the real
repository after the real worker ran - never a row count. That is how the
review found RV-02 (7900 EGP **and** 7200 EGP), and the only way to prove it
closed. Embeddings are faked at the HTTP transport; everything above it is
production code.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from app.core.exceptions import ConflictError, PlanLimitExceededError
from app.core.storage import LocalMediaStorage
from app.db.models.audit import AuditAction
from app.db.models.knowledge import (
    Document,
    DocumentIndexGeneration,
    DocumentSource,
    GenerationState,
)
from app.db.models.media_purge import MediaPurgeObject
from app.services.document_indexing import MAX_INDEXING_ATTEMPTS
from app.services.knowledge_service import (
    REPLACEMENT_OUTCOME_METADATA_ONLY,
    REPLACEMENT_OUTCOME_STARTED,
    KnowledgeService,
    content_hash,
)
from app.services.retrieval_service import RetrievalService
from tests.fake_embeddings import FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.rag_harness import Indexing, respond

pytestmark = pytest.mark.integration

V1 = "Price list 2026\n\nPremium finishing costs 7200 EGP per square metre including materials."
V2 = "Price list 2026\n\nPremium finishing costs 7900 EGP per square metre including materials."
V3 = "Price list 2026\n\nPremium finishing costs 8100 EGP per square metre including materials."
QUERY = "premium finishing costs per square metre"
PDFS = Path(__file__).resolve().parents[1] / "fixtures" / "rag" / "pdf"


def _service(
    indexing: Indexing, session: Any, tenant_id: uuid.UUID, **kwargs: Any
) -> KnowledgeService:
    return KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue(), **kwargs)


async def _served(indexing: Indexing, tenant_id: uuid.UUID, query: str = QUERY) -> list[str]:
    """The prices a search hands the model, in rank order."""
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query=query, top_k=10)
    return [
        price
        for passage in found.passages
        for price in ("7200", "7900", "8100")
        if f"{price} EGP" in passage.content
    ]


async def _upload(
    indexing: Indexing, tenant_id: uuid.UUID, body: str = V1, **kwargs: Any
) -> uuid.UUID:
    async with indexing.database.session() as session:
        knowledge = _service(indexing, session, tenant_id)
        base = await knowledge.ensure_default_knowledge_base()
        view, _ = await knowledge.submit(
            knowledge_base_id=base.id, title=kwargs.pop("title", "Price list"), raw=body, **kwargs
        )
        return view.document.id


async def _replace(
    indexing: Indexing, tenant_id: uuid.UUID, document_id: uuid.UUID, body: str, **kwargs: Any
) -> tuple[Any, str]:
    async with indexing.database.session() as session:
        return await _service(indexing, session, tenant_id).replace_content(
            document_id, raw=body, **kwargs
        )


async def _active_generations(indexing: Indexing, document_id: uuid.UUID) -> int:
    async with indexing.database.session() as session:
        return int(
            await session.scalar(
                select(func.count(DocumentIndexGeneration.id)).where(
                    DocumentIndexGeneration.document_id == document_id,
                    DocumentIndexGeneration.state == GenerationState.ACTIVE,
                )
            )
            or 0
        )


async def test_a_replacement_serves_v1_until_the_swap_and_only_v2_after(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id)
    await indexing.drain()
    assert await _served(indexing, tenant_id) == ["7200"]

    view, outcome = await _replace(
        indexing, tenant_id, document_id, V2, title="Price list (current)"
    )
    assert outcome == REPLACEMENT_OUTCOME_STARTED
    # Nothing has changed for retrieval, or on the document, until it publishes.
    assert await _served(indexing, tenant_id) == ["7200"]
    stored = await indexing.document(document_id)
    assert stored is not None and "7200" in (stored.content or "") and stored.title == "Price list"

    indexing.provider.gate = asyncio.Event()
    indexing.provider.entered = asyncio.Event()
    worker = asyncio.create_task(indexing.drain())
    await asyncio.wait_for(indexing.provider.entered.wait(), 10)
    assert await _served(indexing, tenant_id) == ["7200"]  # v2 is embedding: v1 still serves
    indexing.provider.gate.set()
    await worker

    assert await _served(indexing, tenant_id) == ["7900"]
    stored = await indexing.document(document_id)
    assert stored is not None
    assert "7900" in (stored.content or "")
    assert stored.title == "Price list (current)"
    assert stored.content_hash == content_hash(V2)
    assert await _active_generations(indexing, document_id) == 1

    # Re-index now rebuilds the *current* text.
    await indexing.reindex(tenant_id, document_id)
    await indexing.drain()
    assert await _served(indexing, tenant_id) == ["7900"]


async def test_the_review_demo_returns_only_the_new_price(indexing: Indexing) -> None:
    """P-R05: v1 -> v2 -> search. Before: 7900 *and* 7200."""
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id)
    await indexing.drain()
    await _replace(indexing, tenant_id, document_id, V2)
    await indexing.drain()
    async with indexing.database.session() as session:
        documents = list(
            await session.scalars(select(Document).where(Document.tenant_id == tenant_id))
        )
    assert len(documents) == 1
    assert await _served(indexing, tenant_id) == ["7900"]


@pytest.mark.parametrize("status", [404, 503])
async def test_a_failed_replacement_leaves_v1_serving(indexing: Indexing, status: int) -> None:
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id)
    await indexing.drain()
    indexing.provider.handler = respond(status)
    await _replace(indexing, tenant_id, document_id, V2)
    await indexing.drain()
    for _ in range(MAX_INDEXING_ATTEMPTS):  # permanent fails at once; transient spends its budget
        await indexing.make_due(document_id)
        await indexing.recovery().run_once()
        await indexing.drain()
    assert await _served(indexing, tenant_id) == ["7200"]
    async with indexing.database.session() as session:
        view = await _service(indexing, session, tenant_id).get_document(document_id)
    assert view.latest is not None and view.latest.state is GenerationState.FAILED
    assert view.active is not None and view.document.content_hash == content_hash(V1)


async def test_two_replacements_in_a_row_leave_one_attempt_and_the_newest_wins(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id)
    await indexing.drain()
    await _replace(indexing, tenant_id, document_id, V2)
    await _replace(indexing, tenant_id, document_id, V3)
    generations = await indexing.generations(document_id)
    in_flight = [
        g for g in generations if g.state in (GenerationState.PENDING, GenerationState.PROCESSING)
    ]
    assert len(in_flight) == 1
    cancelled = [g for g in generations if g.last_error_code == "replaced"]
    assert len(cancelled) == 1 and cancelled[0].state is GenerationState.FAILED
    await indexing.drain()
    assert await _served(indexing, tenant_id) == ["8100"]


async def test_concurrent_replacements_coalesce_without_deadlock(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id)
    await indexing.drain()
    results = await asyncio.wait_for(
        asyncio.gather(
            _replace(indexing, tenant_id, document_id, V2),
            _replace(indexing, tenant_id, document_id, V3),
        ),
        30,
    )
    assert {outcome for _, outcome in results} == {REPLACEMENT_OUTCOME_STARTED}
    generations = await indexing.generations(document_id)
    assert len([g for g in generations if g.state is GenerationState.PENDING]) == 1
    newest = max(generations, key=lambda g: g.number)
    await indexing.drain()
    expected = "7900" if "7900" in (newest.content or "") else "8100"
    assert await _served(indexing, tenant_id) == [expected]


async def test_a_replacement_fences_out_a_worker_already_embedding_the_old_attempt(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id)
    await indexing.drain()
    await _replace(indexing, tenant_id, document_id, V2)
    indexing.provider.gate = asyncio.Event()
    indexing.provider.entered = asyncio.Event()
    held = asyncio.create_task(indexing.drain(budget=1))
    await asyncio.wait_for(indexing.provider.entered.wait(), 10)
    # v2 is being embedded; v3 arrives.
    await _replace(indexing, tenant_id, document_id, V3)
    indexing.provider.gate.set()
    await held
    assert await _served(indexing, tenant_id) == ["7200"]  # the fenced worker published nothing
    await indexing.drain()
    assert await _served(indexing, tenant_id) == ["8100"]


async def test_the_same_text_updates_metadata_only(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id)
    await indexing.drain()
    calls = len(indexing.provider.requests)
    view, outcome = await _replace(indexing, tenant_id, document_id, V1, title="Price list 2026")
    assert outcome == REPLACEMENT_OUTCOME_METADATA_ONLY and view.metadata_updated
    assert len(await indexing.generations(document_id)) == 1
    await indexing.drain()
    assert len(indexing.provider.requests) == calls
    stored = await indexing.document(document_id)
    assert stored is not None and stored.title == "Price list 2026"


async def test_another_documents_text_is_refused_naming_it(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    first = await _upload(indexing, tenant_id)
    second = await _upload(indexing, tenant_id, V2, title="Another")
    with pytest.raises(ConflictError) as refused:
        await _replace(indexing, tenant_id, first, V2)
    assert refused.value.error_code == "document_content_exists"
    assert refused.value.details == {
        "document_id": str(second),
        "knowledge_base_id": refused.value.details["knowledge_base_id"],  # type: ignore[index]
    }


async def test_a_replacement_needs_no_free_plan_slot(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    asked: list[str] = []

    async def full() -> object:
        asked.append("slot")
        raise PlanLimitExceededError("The plan's document limit is reached.")

    document_id = await _upload(indexing, tenant_id, source_key="pricing.pdf")
    await indexing.drain()
    async with indexing.database.session() as session:
        knowledge = _service(indexing, session, tenant_id)
        base = await knowledge.ensure_default_knowledge_base()
        with pytest.raises(PlanLimitExceededError):
            await knowledge.submit(
                knowledge_base_id=base.id, title="New", raw="A new document.", reserve_slot=full
            )
    async with indexing.database.session() as session:
        knowledge = _service(indexing, session, tenant_id)
        base = await knowledge.ensure_default_knowledge_base()
        # A repeat, a replacement by source key, a PUT: none asks for a slot.
        await knowledge.submit(
            knowledge_base_id=base.id, title="Price list", raw=V1, reserve_slot=full
        )
        await knowledge.submit(
            knowledge_base_id=base.id,
            title="Price list",
            raw=V2,
            source_key="pricing.pdf",
            replace=True,
            reserve_slot=full,
        )
    assert asked == ["slot"]
    await indexing.drain()
    assert await _served(indexing, tenant_id) == ["7900"]
    assert document_id


async def test_a_delete_during_a_replacement_build_removes_the_document_at_once(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id)
    await indexing.drain()
    await _replace(indexing, tenant_id, document_id, V2)
    indexing.provider.gate = asyncio.Event()
    indexing.provider.entered = asyncio.Event()
    held = asyncio.create_task(indexing.drain(budget=1))
    await asyncio.wait_for(indexing.provider.entered.wait(), 10)
    async with indexing.database.session() as session:
        await asyncio.wait_for(
            _service(indexing, session, tenant_id).delete_document(document_id), 5
        )
    assert await _served(indexing, tenant_id) == []
    indexing.provider.gate.set()
    await held
    assert await indexing.document(document_id) is None
    assert await _served(indexing, tenant_id) == []


# ---------------------------------------------------------------- source keys (RQ-07)


async def test_the_same_file_uploaded_twice_is_refused_or_replaced(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    first = await _upload(indexing, tenant_id, filename="Pricing.PDF")
    await indexing.drain()
    with pytest.raises(ConflictError) as refused:
        await _upload(indexing, tenant_id, V2, filename="pricing.pdf")
    assert refused.value.error_code == "document_source_exists"
    assert refused.value.details == {
        "document_id": str(first),
        "replace_url": f"/api/v1/knowledge/documents/{first}/content",
    }
    await _upload(indexing, tenant_id, V2, filename="pricing.pdf", replace=True)
    await indexing.drain()
    async with indexing.database.session() as session:
        count = await session.scalar(
            select(func.count(Document.id)).where(Document.tenant_id == tenant_id)
        )
    assert count == 1
    assert await _served(indexing, tenant_id) == ["7900"]


async def test_resubmitting_identical_text_updates_its_title(indexing: Indexing) -> None:
    """RV-16, P-R05 step 5: before, `created=false` and the old title kept."""
    tenant_id = await indexing.workspace()
    document_id = await _upload(indexing, tenant_id)
    async with indexing.database.session() as session:
        knowledge = _service(indexing, session, tenant_id)
        base = await knowledge.ensure_default_knowledge_base()
        view, created = await knowledge.submit(
            knowledge_base_id=base.id, title="Price list 2026 (current)", raw=V1
        )
    assert created is False and view.metadata_updated
    stored = await indexing.document(document_id)
    assert stored is not None and stored.title == "Price list 2026 (current)"
    (row,) = await indexing.audit(tenant_id, AuditAction.KNOWLEDGE_DOCUMENT_METADATA_UPDATED)
    assert row.meta == {"knowledge_base_id": str(stored.knowledge_base_id), "fields": ["title"]}


async def test_a_pdfs_byte_size_is_its_file_size(indexing: Indexing) -> None:
    """RV-17, P-R13: before, the base64 length (about a third more)."""
    tenant_id = await indexing.workspace()
    pdf = (PDFS / "word-EN-1.pdf").read_bytes()
    document_id = await _upload(
        indexing, tenant_id, base64.b64encode(pdf).decode(), source=DocumentSource.PDF
    )
    stored = await indexing.document(document_id)
    assert stored is not None and stored.byte_size == len(pdf)


# ---------------------------------------------------------------- dedupe (RQ-08)


async def test_the_same_text_in_another_knowledge_base_is_refused(indexing: Indexing) -> None:
    """P-R05 step 4: before, 7900, 7900, 7200."""
    tenant_id = await indexing.workspace()
    first = await _upload(indexing, tenant_id, V2)
    async with indexing.database.session() as session:
        knowledge = _service(indexing, session, tenant_id)
        sales = await knowledge.create_knowledge_base(name="Sales")
        with pytest.raises(ConflictError) as refused:
            await knowledge.submit(knowledge_base_id=sales.id, title="Price list", raw=V2)
    assert refused.value.error_code == "document_exists_in_other_knowledge_base"
    assert refused.value.details is not None and refused.value.details["document_id"] == str(first)


async def test_a_legacy_copy_is_served_once(indexing: Indexing) -> None:
    """The retrieval-time safety net: a copy stored before the refusal existed."""
    tenant_id = await indexing.workspace()
    await _upload(indexing, tenant_id, V2)
    async with indexing.database.session() as session:
        knowledge = _service(indexing, session, tenant_id)
        sales = await knowledge.create_knowledge_base(name="Sales")
        copy, _ = await knowledge.submit(
            knowledge_base_id=sales.id, title="Copy", raw="placeholder"
        )
        copy.document.content = V2
        copy.document.content_hash = content_hash(V2)
    await indexing.drain()
    assert await _served(indexing, tenant_id) == ["7900"]


async def test_an_identical_document_in_another_workspace_triggers_nothing(
    indexing: Indexing,
) -> None:
    mine, theirs = await indexing.workspace(), await indexing.workspace()
    await _upload(indexing, theirs, V2)
    await _upload(indexing, mine, V2)  # no 409: the lookup is this workspace's only (G3)


# ---------------------------------------------------------------- dates and recency


async def test_a_document_outside_its_window_is_never_retrieved(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    now = datetime.now(UTC)
    await _upload(indexing, tenant_id, V1, valid_until=now - timedelta(days=1))
    await _upload(indexing, tenant_id, V3, title="Next year", valid_from=now + timedelta(days=30))
    await _upload(indexing, tenant_id, V2, title="Current", valid_from=now - timedelta(days=30))
    await indexing.drain()
    assert await _served(indexing, tenant_id) == ["7900"]


async def test_each_source_carries_its_publish_date_and_section(indexing: Indexing) -> None:
    import json

    tenant_id = await indexing.workspace()
    await _upload(indexing, tenant_id)
    await indexing.drain()
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=as_embeddings(FakeEmbeddings())
        ).search(query=QUERY)
    (source,) = json.loads(found.as_context())["knowledge_sources"]
    assert source["updated_at"] == datetime.now(UTC).date().isoformat()
    assert source["section"] == "Price list 2026"


# ---------------------------------------------------------------- originals (RQ-23)


async def test_originals_are_kept_and_owed_a_delete_with_their_document(
    indexing: Indexing, tmp_path: Path
) -> None:
    storage = LocalMediaStorage(tmp_path)
    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        knowledge = _service(indexing, session, tenant_id, storage=storage)
        base = await knowledge.ensure_default_knowledge_base()
        view, _ = await knowledge.submit(knowledge_base_id=base.id, title="Price list", raw=V1)
        document_id = view.document.id
    await indexing.drain()
    async with indexing.database.session() as session:
        knowledge = _service(indexing, session, tenant_id, storage=storage)
        await knowledge.replace_content(document_id, raw=V2)
    await indexing.drain()
    async with indexing.database.session() as session:
        knowledge = _service(indexing, session, tenant_id, storage=storage)
        view = await knowledge.get_document(document_id)
        assert view.original_available
        keys = [
            g.original_object_key
            for g in await indexing.generations(document_id)
            if g.original_object_key is not None
        ]
        assert len(keys) == 2 and all(key.startswith(f"{tenant_id}/") for key in keys)
        assert await storage.get(keys[-1]) == V2.encode()
        await knowledge.delete_document(document_id)
    async with indexing.database.session() as session:
        owed = set(
            await session.scalars(
                select(MediaPurgeObject.storage_key).where(MediaPurgeObject.tenant_id == tenant_id)
            )
        )
    assert owed == set(keys)
    # The harness deletes the workspace at the end; the ledger restricts that.
    async with indexing.database.session() as session:
        await session.execute(
            delete(MediaPurgeObject).where(MediaPurgeObject.tenant_id == tenant_id)
        )
