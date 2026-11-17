"""The e2e seed's knowledge states (FE-6, EC6-04): parsed before connecting, written as read."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import DocumentStatus
from app.db.models.tenant import Tenant
from app.services.knowledge_service import KnowledgeService
from tests.e2e_stack import knowledge_states
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "argv",
    [[], ["failed"], ["unknown", str(uuid.uuid4())], ["failed", "not-a-uuid"], ["a", "b", "c"]],
)
def test_an_incomplete_command_is_refused_with_the_usage(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        knowledge_states.parse(argv)
    assert refused.value.code == knowledge_states.USAGE


def test_every_command_parses() -> None:
    target = uuid.uuid4()
    for command in knowledge_states.COMMANDS:
        assert knowledge_states.parse([command, str(target)]) == (command, target)


async def _document(session: AsyncSession) -> uuid.UUID:
    tenant = Tenant(timezone=TEST_TIMEZONE, name="Acme", slug="acme")
    session.add(tenant)
    await session.flush()
    knowledge = KnowledgeService(session=session, tenant_id=tenant.id)
    base = await knowledge.create_knowledge_base(name="Facts")
    view, _ = await knowledge.submit(
        knowledge_base_id=base.id,
        title="Prices",
        raw="The price of a 150 m2 finishing is 500000 EGP.",
    )
    return view.document.id


async def test_failed_holds_its_explanation(db_session: AsyncSession) -> None:
    document_id = await _document(db_session)
    made = await knowledge_states.apply(db_session, "failed", document_id)
    assert made["status"] == DocumentStatus.FAILED.value
    assert made["error"] == knowledge_states.FAILED_ERROR


async def test_processing_and_warnings(db_session: AsyncSession) -> None:
    document_id = await _document(db_session)
    assert (await knowledge_states.apply(db_session, "processing", document_id))["status"] == (
        DocumentStatus.PROCESSING.value
    )
    made = await knowledge_states.apply(db_session, "warnings", document_id)
    assert made["extraction_warnings"] == knowledge_states.WARNINGS


async def test_a_serving_state_needs_a_ready_document(db_session: AsyncSession) -> None:
    """needs-reindex and reindexing change the served generation: none yet, so refused."""
    document_id = await _document(db_session)
    for command in ("needs-reindex", "reindexing"):
        with pytest.raises(SystemExit, match="no serving generation"):
            await knowledge_states.apply(db_session, command, document_id)


async def test_an_unknown_document_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(SystemExit, match="no document"):
        await knowledge_states.apply(db_session, "failed", uuid.uuid4())
