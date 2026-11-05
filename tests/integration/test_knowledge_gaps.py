"""The knowledge-gap report (RQ-27, FX-4.10, IMP-25): off by default, tenant-scoped, short-lived.

Enabled in these tests only. Through the real round loop with a scripted
provider: an empty search the agent ran is recorded with the query the model
wrote; a search that found something is not; the pre-retrieval search, whose
query is the customer's own message, never is. The admin route reads one
workspace's rows; the recovery sweep's retention pass and the workspace purge
delete them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.knowledge import KnowledgeSearchGap
from app.db.models.tenant import Tenant
from app.main import create_app
from app.services.knowledge_service import KnowledgeService
from app.services.workspace_purge_service import WorkspacePurgeService
from tests.conftest import AllowingEntitlements, FakeDependency
from tests.fake_embeddings import FAKE_MODEL, FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    scripted,
    text_response,
    tool_call_response,
    tool_calls_response,
)
from tests.integration.rag_harness import Indexing
from tests.integration.test_tenant_isolation import (  # noqa: F401 - fixtures
    Workspace,
    attacker,
    http,
    isolation_app,
    victim,
)

pytestmark = pytest.mark.integration

SEARCH = "search_knowledge"
PRICES = "Premium finishing costs 7900 EGP per square metre including materials."
MESSAGE = "my personal question about hinges and my flat in Zamalek"


async def _index(runner: TurnRunner, tenant_id: Any) -> None:
    async with runner.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id)
        base = await knowledge.ensure_default_knowledge_base()
        view, _ = await knowledge.submit(knowledge_base_id=base.id, title="Prices", raw=PRICES)
        await knowledge.ingest(
            document_id=view.document.id, embeddings=as_embeddings(FakeEmbeddings())
        )


async def _gaps(session_source: Any, tenant_id: uuid.UUID) -> list[KnowledgeSearchGap]:
    async with session_source.session() as session:
        return list(
            await session.scalars(
                select(KnowledgeSearchGap).where(KnowledgeSearchGap.tenant_id == tenant_id)
            )
        )


async def test_an_empty_agent_search_is_recorded_with_the_models_query_and_a_found_one_is_not(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    ai_turns.configure(rag_gap_report_enabled=True)
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id)
    ai_providers.agent = scripted(
        tool_calls_response(
            [
                (SEARCH, {"query": "warranty on cabinet hinges"}),
                (SEARCH, {"query": "premium finishing price per square metre"}),
            ]
        ),
        text_response("Let me pass the warranty question to a colleague."),
    )
    conversation_id, (message_id,) = await ai_turns.write(workspace, [MESSAGE])
    await ai_turns.answer(workspace, conversation_id, message_id)

    (gap,) = await _gaps(ai_turns.database, workspace.tenant_id)
    assert gap.query == "warranty on cabinet hinges"
    assert gap.language == "en"
    assert "Zamalek" not in gap.query  # the customer's message is never stored


async def test_nothing_is_recorded_while_the_report_is_off(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id)
    ai_providers.agent = scripted(
        tool_call_response(SEARCH, {"query": "warranty on cabinet hinges"}),
        text_response("I will check."),
    )
    conversation_id, (message_id,) = await ai_turns.write(workspace, [MESSAGE])
    await ai_turns.answer(workspace, conversation_id, message_id)
    assert await _gaps(ai_turns.database, workspace.tenant_id) == []


async def test_the_pre_retrieval_search_with_the_customers_message_is_never_recorded(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    ai_turns.configure(
        rag_gap_report_enabled=True,
        rag_preretrieval_enabled=True,
        rag_confidence_tiers_enabled=True,
        rag_confidence_tiers={FAKE_MODEL: (0.01, 0.02)},
    )
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id)
    embedded_before = len(ai_providers.embedding_requests)
    ai_providers.agent = scripted(text_response("Let me check."))
    conversation_id, (message_id,) = await ai_turns.write(workspace, [MESSAGE])
    await ai_turns.answer(workspace, conversation_id, message_id)
    # The pre-retrieval search did run (and found nothing close enough)...
    assert len(ai_providers.embedding_requests) > embedded_before
    # ...and recorded nothing.
    assert await _gaps(ai_turns.database, workspace.tenant_id) == []


# ------------------------------------------------------------- the admin route


@pytest.fixture
def gap_app(
    settings: Settings, db_session: AsyncSession, fake_redis: FakeDependency
) -> Iterator[FastAPI]:
    application = create_app(settings.model_copy(update={"rag_gap_report_enabled": True}))
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = fake_redis

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def gap_http(gap_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=gap_app), base_url="http://wasla.test"
    ) as client:
        yield client


async def test_an_admin_reads_only_their_own_workspaces_gaps(
    gap_http: AsyncClient,
    db_session: AsyncSession,
    victim: Workspace,  # noqa: F811
    attacker: Workspace,  # noqa: F811
) -> None:
    """M-R61: the report is read under the workspace's predicate."""
    for _ in range(3):
        db_session.add(
            KnowledgeSearchGap(
                tenant_id=victim.tenant.id, query="SECRET-VICTIM delivery to Siwa", language="en"
            )
        )
    db_session.add(
        KnowledgeSearchGap(
            tenant_id=attacker.tenant.id, query="opening hours on Friday", language="en"
        )
    )
    await db_session.flush()
    mine = await gap_http.get("/api/v1/knowledge/gaps", headers=attacker.headers)
    assert mine.status_code == 200
    assert [g["query"] for g in mine.json()["gaps"]] == ["opening hours on Friday"]
    assert "SECRET-VICTIM" not in mine.text
    theirs = await gap_http.get("/api/v1/knowledge/gaps", headers=victim.headers)
    assert theirs.json()["gaps"][0]["searches"] == 3


async def test_the_report_is_not_found_while_disabled(
    http: AsyncClient, victim: Workspace  # noqa: F811
) -> None:
    response = await http.get("/api/v1/knowledge/gaps", headers=victim.headers)
    assert response.status_code == 404


# ------------------------------------------------- retention and the purge


async def test_rows_past_the_retention_are_deleted_by_the_recovery_sweep(
    indexing: Indexing,
) -> None:
    """M-R62."""
    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        old = KnowledgeSearchGap(tenant_id=tenant_id, query="old question", language="en")
        new = KnowledgeSearchGap(tenant_id=tenant_id, query="new question", language="en")
        session.add_all([old, new])
        await session.flush()
        await session.execute(
            update(KnowledgeSearchGap)
            .where(KnowledgeSearchGap.id == old.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=31))
        )
    await indexing.recovery().run_once()
    assert [g.query for g in await _gaps(indexing.database, tenant_id)] == ["new question"]


async def test_the_workspace_purge_erases_the_report(indexing: Indexing) -> None:
    """M-R63."""
    tenant_id = await indexing.workspace()
    moment = datetime.now(UTC)
    async with indexing.database.session() as session:
        session.add(KnowledgeSearchGap(tenant_id=tenant_id, query="a question", language="en"))
        await session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(deleted_at=moment - timedelta(days=40), purge_due_at=moment - timedelta(days=1))
        )
    async with indexing.database.session() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        await WorkspacePurgeService(session).purge(tenant, now=moment)
    async with indexing.database.session() as session:
        left = await session.scalar(
            select(func.count(KnowledgeSearchGap.id)).where(
                KnowledgeSearchGap.tenant_id == tenant_id
            )
        )
        # The purge's committed audit entry would outlive this test and be read
        # by suites that count purges; the trail itself is not under test here.
        await session.execute(
            delete(AuditLog).where(
                AuditLog.action == AuditAction.WORKSPACE_PURGED, AuditLog.target_id == tenant_id
            )
        )
    assert left == 0
