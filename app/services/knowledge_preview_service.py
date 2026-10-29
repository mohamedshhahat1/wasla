"""The admin search preview: what the agent would retrieve, and why (FX-3.9, RV-21, IMP-26).

Runs `RetrievalService.search` exactly as the answering agent's tool does - the
workspace's resolved policy, the answering agent's knowledge scope, the same
budget and packing - and returns the passages with their distances and
channels, every candidate with what became of it, the confidence tier when
tiers are on, a warning for two kept documents sharing a title (an old and a new
version that should have been a replacement, RV-02), and the exact JSON the
model would read.

Isolation is the search's own: the tenant comes from the request's active
workspace and every query runs under `served_predicates`.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import replace

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import SEARCH_KNOWLEDGE_TOOL
from app.core.config import Settings
from app.db.models.knowledge import EMBEDDING_DIMENSIONS
from app.integrations.openai.client import ResponsesClient
from app.integrations.openai.embeddings import EMBED_QUERY, EmbeddingsClient
from app.repositories.agent_repository import AgentRepository, AgentToolRepository
from app.schemas.knowledge import CandidateRead, PassageRead, SearchRequest, SearchResponse
from app.services.knowledge_rerank import LlmReranker
from app.services.knowledge_settings_service import KnowledgeSettingsService
from app.services.retrieval_policy import knowledge_scope
from app.services.retrieval_service import RetrievalService


async def answering_scope(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[uuid.UUID, ...] | None:
    """The answering agent's `search_knowledge` scope; None for the whole workspace."""
    agent = await AgentRepository(session, tenant_id=tenant_id).get_answering_default()
    if agent is None:
        return None
    grant = await AgentToolRepository(session, tenant_id=tenant_id).get(
        agent_id=agent.id, name=SEARCH_KNOWLEDGE_TOOL
    )
    return knowledge_scope(grant.config) if grant is not None else None


class KnowledgePreviewService:
    """Searches one workspace's knowledge the way its agent would, and shows the working."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        settings: Settings,
        http: httpx.AsyncClient,
        embeddings: EmbeddingsClient | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings
        self._http = http
        self._embeddings = embeddings or EmbeddingsClient(
            http=http,
            api_key=settings.openai_api_key or "",
            model=settings.openai_embedding_model,
            dimensions=EMBEDDING_DIMENSIONS,
            operation=EMBED_QUERY,
        )

    async def search(self, request: SearchRequest) -> SearchResponse:
        switches = await KnowledgeSettingsService(
            session=self._session, tenant_id=self._tenant_id, settings=self._settings
        ).switches()
        scope = await answering_scope(self._session, self._tenant_id)
        policy = switches.policy
        if scope is not None:
            policy = replace(policy, knowledge_base_ids=scope)
        reranker = (
            LlmReranker(
                client=ResponsesClient(
                    http=self._http, api_key=self._settings.openai_api_key or "-"
                ),
                model=switches.rerank_model,
                timeout_seconds=switches.rerank_timeout_seconds,
            )
            if policy.rerank and switches.rerank_model
            else None
        )
        found = await RetrievalService(
            session=self._session,
            tenant_id=self._tenant_id,
            embeddings=self._embeddings,
            policy=policy,
            reranker=reranker,
        ).search(
            query=request.query,
            alternate_query=request.alternate_query,
            top_k=request.top_k,
            knowledge_base_id=request.knowledge_base_id,
        )
        passages = [
            PassageRead(
                source=index,
                document_id=passage.document_id,
                document_title=passage.document_title,
                section=passage.heading_path,
                content=passage.content,
                distance=round(passage.distance, 4),
                channels=list(passage.channels),
                chunk_ordinals=list(passage.ordinals),
                updated_at=passage.updated_at,
            )
            for index, passage in enumerate(found.passages, start=1)
        ]
        titles = Counter(
            (passage.document_title, passage.document_id) for passage in found.passages
        )
        by_title = Counter(title for title, _ in titles)
        warnings = [
            f"Two documents titled {title!r} were both retrieved: if one replaces the other, "
            "replace its content instead of keeping both."
            for title, count in by_title.items()
            if count > 1
        ]
        diagnostics = found.diagnostics
        return SearchResponse(
            passages=passages,
            is_empty=found.is_empty,
            match=found.match,
            best_distance=(
                round(diagnostics.best_distance, 4)
                if diagnostics and diagnostics.best_distance is not None
                else None
            ),
            candidates=dict(diagnostics.candidates) if diagnostics else {},
            dropped=dict(diagnostics.dropped) if diagnostics else {},
            rerank=diagnostics.rerank if diagnostics else "disabled",
            decisions=[
                CandidateRead(
                    document_id=decision.document_id,
                    document_title=decision.document_title,
                    chunk_ordinal=decision.ordinal,
                    distance=round(decision.distance, 4),
                    channels=list(decision.channels),
                    outcome=decision.outcome,
                )
                for decision in (diagnostics.decisions if diagnostics else ())
            ],
            duplicate_version_warnings=warnings,
            model_context=found.as_context(),
        )


__all__ = ["KnowledgePreviewService", "answering_scope"]
