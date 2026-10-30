"""Changing the embedding model without the knowledge base going dark (RQ-25, FX-4.1, RV-11, P-R17).

P-R17 at the base head: a search after `OPENAI_EMBEDDING_MODEL` changed returned
nothing, and the tool told the model "No information about this was found".
The proof here is not that the switch completes: it is that **every document is
found by its own text at every step** - before the switch, at 0 %, 50 % and
99 % coverage, after the flip and after retirement - by real `RetrievalService`
searches over the real repository, through shadows the real worker built.

The fake transport returns the same vector for the same text whichever model
is asked, so both spaces rank alike; which space a query was embedded in is
read from the `model` of the request the transport received.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from app.core.embedding_space import OPENAI_PROVIDER, EmbeddingSpace
from app.db.models.knowledge import (
    EMBEDDING_DIMENSIONS,
    DocumentIndexGeneration,
    DocumentSource,
    GenerationState,
)
from app.integrations.openai.embeddings import EmbeddingsClient
from app.services.embedding_switch import EmbeddingSwitch, SwitchError
from app.services.knowledge_service import KnowledgeService
from app.services.retrieval_service import RetrievalService
from scripts.rag_invariants import rag_violations
from tests.fake_embeddings import FAKE_MODEL
from tests.integration.ai_harness import embedding_response
from tests.integration.rag_harness import Indexing
from tests.knowledge_files import csv_text, xlsx_bytes

pytestmark = pytest.mark.integration

LARGE = "text-embedding-3-large"
SMALL_SPACE = EmbeddingSpace(
    provider=OPENAI_PROVIDER, model=FAKE_MODEL, dimensions=EMBEDDING_DIMENSIONS
)
LARGE_SPACE = EmbeddingSpace(provider=OPENAI_PROVIDER, model=LARGE, dimensions=EMBEDDING_DIMENSIONS)

DOCUMENTS: list[tuple[str, DocumentSource, str, str]] = [
    (
        "Refunds",
        DocumentSource.TEXT,
        "Refunds\n\nItems may be returned within fourteen days.",
        "returned within fourteen days",
    ),
    (
        "Delivery",
        DocumentSource.MARKDOWN,
        "# Delivery\n\nDelivery to Aswan takes four days.",
        "delivery to Aswan",
    ),
    (
        "Warranty",
        DocumentSource.TEXT,
        "Warranty\n\nThe warranty covers hinges for five years.",
        "warranty covers hinges",
    ),
    (
        "Prices",
        DocumentSource.CSV,
        csv_text([("BK-35", "Bookshelf model 35", "4,500 EGP")]),
        "Bookshelf model 35",
    ),
    ("Catalogue", DocumentSource.XLSX, "", "Walnut desk"),
    (
        "Showroom",
        DocumentSource.TEXT,
        "Showroom\n\nThe Maadi showroom opens at ten.",
        "Maadi showroom opens",
    ),
]


class Requests:
    """The model of every embeddings request the transport received, in order."""

    def __init__(self, indexing: Indexing) -> None:
        self.indexing = indexing

    def models(self) -> list[str]:
        return [str(body.get("model")) for body in self.indexing.provider.requests]


def _client(indexing: Indexing) -> EmbeddingsClient:
    http = httpx.AsyncClient(transport=indexing.provider.transport())
    return EmbeddingsClient(
        http=http,
        api_key="sk-test-not-real",
        model=FAKE_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        next_space=EmbeddingsClient(
            http=http, api_key="sk-test-not-real", model=LARGE, dimensions=EMBEDDING_DIMENSIONS
        ),
    )


async def _seed(indexing: Indexing) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    tenant_id = await indexing.workspace()
    ids: dict[str, uuid.UUID] = {}
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = await knowledge.ensure_default_knowledge_base()
        for title, source, body, _ in DOCUMENTS:
            raw = body
            if source is DocumentSource.XLSX:
                import base64

                raw = base64.b64encode(
                    xlsx_bytes(rows=[("WD-1", "Walnut desk", "3,200 EGP")])
                ).decode()
            view, _ = await knowledge.submit(
                knowledge_base_id=base.id, title=title, raw=raw, source=source
            )
            ids[title] = view.document.id
    await indexing.drain()
    return tenant_id, ids


async def _search_every_document(
    indexing: Indexing, tenant_id: uuid.UUID, ids: dict[str, uuid.UUID]
) -> dict[str, Any]:
    """Each document searched by its own words: (found, served generations, query model)."""
    client = _client(indexing)
    empty: list[str] = []
    generations: dict[uuid.UUID, set[int]] = {}
    seen: dict[tuple[uuid.UUID, int], int] = {}
    doubled: list[tuple[uuid.UUID, int]] = []
    models: set[str] = set()
    for title, _, _, query in DOCUMENTS:
        before = len(indexing.provider.requests)
        async with indexing.database.session() as session:
            found = await RetrievalService(
                session=session, tenant_id=tenant_id, embeddings=client
            ).search(query=query, top_k=6)
        models.update(str(b["model"]) for b in indexing.provider.requests[before:])
        mine = [p for p in found.passages if p.document_id == ids[title]]
        if not mine:
            empty.append(title)
        # Never two: one candidate per (document, ordinal) - a shadow or an
        # old generation served beside the active one would be a second.
        assert found.diagnostics is not None
        for decision in found.diagnostics.decisions:
            key = (decision.document_id, decision.ordinal)
            seen[key] = seen.get(key, 0) + 1
            generations.setdefault(decision.document_id, set()).add(decision.ordinal)
        doubled.extend(key for key, count in seen.items() if count > 1)
        seen.clear()
    return {"empty": empty, "doubled": doubled, "models": models}


async def _step(indexing: Indexing, tenant_id: uuid.UUID, action: str, **kwargs: Any) -> Any:
    async with indexing.database.session() as session:
        switch = EmbeddingSwitch(
            session, tenant_id=tenant_id, default_space=SMALL_SPACE, queue=indexing.queue()
        )
        if action == "start":
            return await switch.start(LARGE_SPACE)
        if action == "shadows":
            created = await switch.request_shadows(limit=kwargs["limit"])
            await switch.enqueue(created)
            return created
        if action == "coverage":
            return await switch.coverage()
        if action == "flip":
            return await switch.flip()
        if action == "retire":
            return await switch.retire()
        if action == "cancel":
            return await switch.cancel()
    raise AssertionError(action)


async def _active_spaces(indexing: Indexing, tenant_id: uuid.UUID) -> dict[str, int]:
    async with indexing.database.session() as session:
        rows = await session.execute(
            select(DocumentIndexGeneration.embedding_model, func.count())
            .where(
                DocumentIndexGeneration.tenant_id == tenant_id,
                DocumentIndexGeneration.state == GenerationState.ACTIVE,
            )
            .group_by(DocumentIndexGeneration.embedding_model)
        )
        return {str(model): int(count) for model, count in rows.tuples()}


def _assert_served_once(result: dict[str, Any]) -> None:
    assert result["doubled"] == [], result


async def test_a_switch_never_leaves_a_document_unfindable(indexing: Indexing) -> None:
    indexing.configure(openai_embedding_model_next=LARGE)
    tenant_id, ids = await _seed(indexing)
    steps: dict[str, Any] = {}

    steps["before"] = await _search_every_document(indexing, tenant_id, ids)
    await _step(indexing, tenant_id, "start")
    steps["0%"] = await _search_every_document(indexing, tenant_id, ids)

    await _step(indexing, tenant_id, "shadows", limit=3)
    await indexing.drain()
    coverage = await _step(indexing, tenant_id, "coverage")
    assert (coverage.covered, coverage.ready) == (3, 6)
    steps["50%"] = await _search_every_document(indexing, tenant_id, ids)

    await _step(indexing, tenant_id, "shadows", limit=2)
    await indexing.drain()
    coverage = await _step(indexing, tenant_id, "coverage")
    assert (coverage.covered, coverage.ready) == (5, 6)
    steps["99%"] = await _search_every_document(indexing, tenant_id, ids)
    with pytest.raises(SwitchError):
        await _step(indexing, tenant_id, "flip")
    steps["99% after a refused flip"] = await _search_every_document(indexing, tenant_id, ids)

    await _step(indexing, tenant_id, "shadows", limit=10)
    await indexing.drain()
    coverage = await _step(indexing, tenant_id, "coverage")
    assert coverage.complete
    steps["100%"] = await _search_every_document(indexing, tenant_id, ids)
    assert await _active_spaces(indexing, tenant_id) == {FAKE_MODEL: 6}

    await _step(indexing, tenant_id, "flip")
    steps["flipped"] = await _search_every_document(indexing, tenant_id, ids)
    assert await _active_spaces(indexing, tenant_id) == {LARGE: 6}
    deleted = await _step(indexing, tenant_id, "retire")
    assert deleted > 0
    steps["retired"] = await _search_every_document(indexing, tenant_id, ids)

    for name, result in steps.items():
        assert result["empty"] == [], (name, result["empty"])
        _assert_served_once(result)
    for name in ("before", "0%", "50%", "99%", "99% after a refused flip", "100%"):
        assert steps[name]["models"] == {FAKE_MODEL}, name
    for name in ("flipped", "retired"):
        assert steps[name]["models"] == {LARGE}, name

    async with indexing.database.engine.connect() as connection:
        violations = await rag_violations(connection)
    assert {k: v for k, v in violations.items() if v} == {}


async def test_a_shadow_is_never_served_before_the_flip(indexing: Indexing) -> None:
    """M-R54: shadows are a state retrieval does not read, and one active generation each."""
    indexing.configure(openai_embedding_model_next=LARGE)
    tenant_id, ids = await _seed(indexing)
    await _step(indexing, tenant_id, "start")
    await _step(indexing, tenant_id, "shadows", limit=10)
    await indexing.drain()
    async with indexing.database.session() as session:
        rows = await session.execute(
            select(DocumentIndexGeneration.state, func.count())
            .where(DocumentIndexGeneration.tenant_id == tenant_id)
            .group_by(DocumentIndexGeneration.state)
        )
        states: dict[GenerationState, int] = {state: int(count) for state, count in rows.tuples()}
    assert states[GenerationState.SHADOW] == 6 and states[GenerationState.ACTIVE] == 6
    result = await _search_every_document(indexing, tenant_id, ids)
    assert result["empty"] == []
    _assert_served_once(result)
    assert result["models"] == {FAKE_MODEL}
    assert await _active_spaces(indexing, tenant_id) == {FAKE_MODEL: 6}


async def test_a_permanently_failed_shadow_blocks_the_flip_and_is_listed(
    indexing: Indexing,
) -> None:
    indexing.configure(openai_embedding_model_next=LARGE)
    tenant_id, ids = await _seed(indexing)

    async def refuse_the_showroom_in_the_new_model(body: dict[str, Any]) -> httpx.Response:
        if body.get("model") == LARGE and "Maadi" in json.dumps(body["input"]):
            return httpx.Response(400, json={"error": {"code": "invalid_request"}})
        return embedding_response(body)

    indexing.provider.handler = refuse_the_showroom_in_the_new_model
    await _step(indexing, tenant_id, "start")
    await _step(indexing, tenant_id, "shadows", limit=10)
    await indexing.drain()
    coverage = await _step(indexing, tenant_id, "coverage")
    assert (coverage.covered, coverage.ready) == (5, 6)
    assert [b.document_id for b in coverage.blocked] == [ids["Showroom"]]
    # Not rebuilt automatically: only a person can decide what to do with it.
    assert await _step(indexing, tenant_id, "shadows", limit=10) == []
    with pytest.raises(SwitchError) as refused:
        await _step(indexing, tenant_id, "flip")
    assert str(ids["Showroom"]) in (refused.value.details or {})["blocked"]
    result = await _search_every_document(indexing, tenant_id, ids)
    assert result["empty"] == [] and result["models"] == {FAKE_MODEL}
    document = await indexing.document(ids["Showroom"])
    assert document is not None and str(document.status) == "ready" and document.error is None


async def test_a_document_replaced_mid_switch_gets_a_new_shadow_and_only_its_new_text_flips(
    indexing: Indexing,
) -> None:
    indexing.configure(openai_embedding_model_next=LARGE)
    tenant_id, ids = await _seed(indexing)
    await _step(indexing, tenant_id, "start")
    await _step(indexing, tenant_id, "shadows", limit=10)
    await indexing.drain()
    async with indexing.database.session() as session:
        await KnowledgeService(
            session=session, tenant_id=tenant_id, queue=indexing.queue()
        ).replace_content(
            ids["Refunds"], raw="Refunds\n\nItems may be returned within thirty days."
        )
    await indexing.drain()
    coverage = await _step(indexing, tenant_id, "coverage")
    assert (coverage.covered, coverage.owed) == (5, 1)
    await _step(indexing, tenant_id, "shadows", limit=10)
    await indexing.drain()
    await _step(indexing, tenant_id, "flip")
    await _step(indexing, tenant_id, "retire")
    client = _client(indexing)
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=client
        ).search(query="returned within days", top_k=6)
    served = " ".join(p.content for p in found.passages if p.document_id == ids["Refunds"])
    assert "thirty days" in served and "fourteen" not in served


async def test_a_generation_built_in_the_old_space_is_put_back_after_a_flip(
    indexing: Indexing,
) -> None:
    """A document still being indexed when its workspace flips is embedded again, not hidden."""
    indexing.configure(openai_embedding_model_next=LARGE)
    tenant_id, ids = await _seed(indexing)
    await _step(indexing, tenant_id, "start")
    await _step(indexing, tenant_id, "shadows", limit=10)
    await indexing.drain()
    late = await indexing.submit(tenant_id, "Opening hours\n\nThe Zamalek branch opens at noon.")
    indexing.provider.gate = asyncio.Event()
    indexing.provider.entered = asyncio.Event()
    worker = asyncio.create_task(indexing.drain(budget=1))
    await asyncio.wait_for(indexing.provider.entered.wait(), 10)
    assert indexing.provider.requests[-1]["model"] == FAKE_MODEL
    await _step(indexing, tenant_id, "flip")  # the late document is not ready: not counted
    indexing.provider.gate.set()
    await worker
    generations = await indexing.generations(late)
    assert [(g.state, g.last_error_code) for g in generations] == [
        (GenerationState.PENDING, "space_changed")
    ]
    assert generations[0].attempts == 0
    indexing.provider.gate = None
    await indexing.make_due(late)
    await indexing.recovery().run_once()
    await indexing.drain()
    generations = await indexing.generations(late)
    assert generations[-1].state is GenerationState.ACTIVE
    assert generations[-1].embedding_model == LARGE
    client = _client(indexing)
    async with indexing.database.session() as session:
        found = await RetrievalService(
            session=session, tenant_id=tenant_id, embeddings=client
        ).search(query="Zamalek branch opens", top_k=3)
    assert any(p.document_id == late for p in found.passages)


async def test_each_workspace_is_queried_in_its_own_space(indexing: Indexing) -> None:
    """M-R56: a workspace not flipped is never queried in the target space."""
    indexing.configure(openai_embedding_model_next=LARGE)
    flipped, flipped_ids = await _seed(indexing)
    waiting, waiting_ids = await _seed(indexing)
    for tenant_id in (flipped, waiting):
        await _step(indexing, tenant_id, "start")
    await _step(indexing, flipped, "shadows", limit=10)
    await _step(indexing, waiting, "shadows", limit=2)
    await indexing.drain()
    await _step(indexing, flipped, "flip")
    first = await _search_every_document(indexing, flipped, flipped_ids)
    second = await _search_every_document(indexing, waiting, waiting_ids)
    assert first["models"] == {LARGE} and first["empty"] == []
    assert second["models"] == {FAKE_MODEL} and second["empty"] == []


async def test_cancelling_a_switch_leaves_the_workspace_as_it_was(indexing: Indexing) -> None:
    indexing.configure(openai_embedding_model_next=LARGE)
    tenant_id, ids = await _seed(indexing)
    await _step(indexing, tenant_id, "start")
    await _step(indexing, tenant_id, "shadows", limit=4)
    await indexing.drain()
    await _step(indexing, tenant_id, "cancel")
    await _step(indexing, tenant_id, "retire")
    result = await _search_every_document(indexing, tenant_id, ids)
    assert result["empty"] == [] and result["models"] == {FAKE_MODEL}
    async with indexing.database.engine.connect() as connection:
        violations = await rag_violations(connection)
    assert {k: v for k, v in violations.items() if v} == {}
    async with indexing.database.session() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM workspace_embedding_spaces WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ) == 0


async def test_the_background_rebuild_is_throttled_and_fair_across_workspaces(
    indexing: Indexing,
) -> None:
    """The throttled re-index: one document per workspace per turn, at the rate asked."""
    from app.services.embedding_switch import build

    indexing.configure(openai_embedding_model_next=LARGE)
    first, _ = await _seed(indexing)
    second, _ = await _seed(indexing)
    for tenant_id in (first, second):
        await _step(indexing, tenant_id, "start")
    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    report = await build(
        indexing.database,
        default_space=SMALL_SPACE,
        queue=indexing.queue(),
        rate_per_minute=120,
        limit=6,
        pause=pause,
    )
    assert report.requested == report.queued == 6
    assert pauses == [0.5] * 6
    per_workspace = {
        tenant_id: (await _step(indexing, tenant_id, "coverage")).building
        for tenant_id in (first, second)
    }
    assert per_workspace == {first: 3, second: 3}
    # Resumable: a second run continues from what the database says is owed.
    again = await build(
        indexing.database, default_space=SMALL_SPACE, queue=indexing.queue(), rate_per_minute=0
    )
    assert again.requested == 6
    await indexing.drain()
    final = await build(
        indexing.database,
        default_space=SMALL_SPACE,
        queue=indexing.queue(),
        rate_per_minute=0,
        flip_when_complete=True,
        until_complete=True,
        pause=pause,
        idle_seconds=0,
    )
    assert set(final.flipped) == {first, second}
