"""The admin search preview and the confidence tiers (FX-3.9, FX-3.12, RV-21, RQ-13).

Service level on real PostgreSQL with the suite's fake embeddings; the route's
authorisation is in `test_knowledge_endpoints.py`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.db.models.usage import UsageEvent, UsageEventType
from app.schemas.knowledge import SearchRequest
from app.services.knowledge_preview_service import KnowledgePreviewService
from app.services.knowledge_service import KnowledgeService
from app.services.knowledge_settings_service import KnowledgeSettingsService
from app.services.retrieval_policy import RetrievalPolicy, resolve
from app.services.retrieval_service import RetrievalService
from tests.fake_embeddings import FAKE_MODEL, FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.ai_harness import settings_for
from tests.integration.isolation_gate import MARKER, build_twins
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration


async def _submit(indexing: Indexing, tenant_id: uuid.UUID, title: str, body: str) -> None:
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = await knowledge.ensure_default_knowledge_base()
        await knowledge.submit(knowledge_base_id=base.id, title=title, raw=body)


async def _preview(
    indexing: Indexing, tenant_id: uuid.UUID, request: SearchRequest, **settings: Any
) -> Any:
    async with indexing.database.session() as session, httpx.AsyncClient() as http:
        service = KnowledgePreviewService(
            session=session,
            tenant_id=tenant_id,
            settings=settings_for(indexing.url, **settings),
            http=http,
            embeddings=as_embeddings(FakeEmbeddings()),
        )
        return await service.search(request)


async def test_the_preview_shows_what_the_model_would_read_and_why(indexing: Indexing) -> None:
    tenant_id = await indexing.workspace()
    await _submit(indexing, tenant_id, "Price list", "Premium finishing costs 7900 EGP per metre.")
    await _submit(indexing, tenant_id, "Price list", "Premium finishing costs 7200 EGP per metre.")
    await _submit(indexing, tenant_id, "Astronomy", "Jupiter Saturn moons telescope night sky.")
    await indexing.drain()

    preview = await _preview(indexing, tenant_id, SearchRequest(query="premium finishing costs"))

    assert [p.document_title for p in preview.passages] == ["Price list", "Price list"]
    assert all(p.distance <= 0.75 and p.channels for p in preview.passages)
    outcomes = {d.document_title: d.outcome for d in preview.decisions}
    assert outcomes["Astronomy"] == "threshold"
    assert preview.dropped["threshold"] >= 1
    assert preview.candidates["vector"] >= 3
    # Two kept documents with one title: an old and a new version (RV-02).
    assert preview.duplicate_version_warnings
    # The exact JSON the model would read, with source numbers and no ids.
    context = json.loads(preview.model_context)
    assert [s["source"] for s in context["knowledge_sources"]] == [1, 2]
    assert all(str(p.document_id) not in preview.model_context for p in preview.passages)
    assert preview.match is None
    # Metered like a search (RV-21, IMP-26).
    async with indexing.database.session() as session:
        searches = await session.scalar(
            select(func.count(UsageEvent.id)).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.event_type == UsageEventType.RAG_QUERY,
            )
        )
    assert searches == 1


async def test_the_preview_returns_nothing_foreign(indexing: Indexing) -> None:
    twins = await build_twins(indexing)
    for query in ("7900 EGP premium finishing", MARKER):
        preview = await _preview(indexing, twins.viewer, SearchRequest(query=query))
        twins.assert_isolated(preview.model_dump(mode="json"))


async def test_tiers_are_off_unless_enabled_and_calibrated(indexing: Indexing) -> None:
    """M-R53: calibrated cut-offs are not applied while tiers are disabled."""
    url = indexing.url
    calibrated = {"rag_confidence_tiers": {FAKE_MODEL: (0.3, 0.6)}}
    assert not resolve(
        settings_for(url, **calibrated), None, embedding_model=FAKE_MODEL
    ).policy.tiers_active
    assert not resolve(
        settings_for(url, rag_confidence_tiers_enabled=True), None, embedding_model=FAKE_MODEL
    ).policy.tiers_active
    assert resolve(
        settings_for(url, rag_confidence_tiers_enabled=True, **calibrated),
        None,
        embedding_model=FAKE_MODEL,
    ).policy.tiers_active

    tenant_id = await indexing.workspace()
    await _submit(indexing, tenant_id, "Price list", "Premium finishing costs 7900 EGP per metre.")
    await indexing.drain()

    async def context(policy: RetrievalPolicy) -> dict[str, Any]:
        async with indexing.database.session() as session:
            found = await RetrievalService(
                session=session,
                tenant_id=tenant_id,
                embeddings=as_embeddings(FakeEmbeddings()),
                policy=policy,
            ).search(query="premium finishing costs")
        decoded: dict[str, Any] = json.loads(found.as_context())
        return decoded

    off = await context(RetrievalPolicy(strong_distance=0.3, partial_distance=0.6))
    on = await context(
        RetrievalPolicy(confidence_tiers=True, strong_distance=0.3, partial_distance=0.6)
    )
    assert "match" not in off["knowledge_sources"][0]
    assert on["knowledge_sources"][0]["match"] in {"strong", "partial"}


async def test_workspace_settings_override_the_defaults_and_are_audited(
    indexing: Indexing,
) -> None:
    from app.db.models.audit import AuditAction

    tenant_id = await indexing.workspace()
    settings = settings_for(indexing.url)
    async with indexing.database.session() as session:
        service = KnowledgeSettingsService(session=session, tenant_id=tenant_id, settings=settings)
        before = await service.view()
        after = await service.update({"number_grounding": "strict", "hybrid": False}, actor=None)
        cleared = await service.update({"hybrid": None}, actor=None)
    assert before["effective"]["number_grounding"] == "observe"
    assert after["effective"]["number_grounding"] == "strict"
    assert after["effective"]["hybrid"] is False
    assert cleared["overrides"] == {"number_grounding": "strict"}
    assert cleared["effective"]["hybrid"] is True
    assert len(await indexing.audit(tenant_id, AuditAction.KNOWLEDGE_SETTINGS_UPDATED)) == 2
