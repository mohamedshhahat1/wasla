"""The specific codes FE-6's screens tell apart (FEB-06, EC6-02).

Each raise site gained `error_code=` and nothing else: the status and the
sentence are asserted beside the code. FEB-40 - a knowledge base given a name
already used - answered 500 from the unique constraint; it is now a 409.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import SEARCH_KNOWLEDGE_TOOL
from app.core.config import Settings
from app.core.exceptions import ConflictError, ValidationError
from app.db.models.agent import AgentStatus
from app.db.models.tenant import Tenant
from app.repositories.knowledge_repository import KnowledgeBaseRepository
from app.services.agent_service import AgentService
from app.services.knowledge_service import KnowledgeService
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

PROMPT = "You answer questions about the test company."


async def _tenant(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(timezone=TEST_TIMEZONE, name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


def _agents(session: AsyncSession, tenant: Tenant, settings: Settings) -> AgentService:
    return AgentService(session=session, settings=settings, tenant_id=tenant.id)


async def test_a_used_agent_name_is_agent_name_exists(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session, slug="acme")
    service = _agents(db_session, tenant, settings)
    await service.create(name="Sales", system_prompt=PROMPT)
    other = await service.create(name="Support", system_prompt=PROMPT)

    with pytest.raises(ConflictError) as refused:
        await service.update(other.id, name="Sales")

    assert refused.value.error_code == "agent_name_exists"
    assert refused.value.status_code == 409
    assert refused.value.message == "An agent with that name already exists."


@pytest.mark.parametrize("state", [AgentStatus.DRAFT, AgentStatus.DISABLED])
async def test_making_an_inactive_agent_default_is_agent_not_active(
    db_session: AsyncSession, settings: Settings, state: AgentStatus
) -> None:
    tenant = await _tenant(db_session, slug="acme")
    service = _agents(db_session, tenant, settings)
    agent = await service.create(name="Sales", system_prompt=PROMPT)
    if state is AgentStatus.DISABLED:
        await service.update(agent.id, status=AgentStatus.DISABLED)

    with pytest.raises(ValidationError) as refused:
        await service.make_default(agent.id)

    assert refused.value.error_code == "agent_not_active"
    assert refused.value.status_code == 422
    assert refused.value.message == "Activate this agent before making it the default."


async def test_a_model_outside_the_list_is_agent_model_not_permitted(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session, slug="acme")
    tuned = settings.model_copy(
        update={"openai_model": "model-a", "openai_allowed_models": ["model-b"]}
    )
    service = _agents(db_session, tenant, tuned)

    with pytest.raises(ValidationError) as refused:
        await service.create(name="Sales", system_prompt=PROMPT, model="model-z")

    assert refused.value.error_code == "agent_model_not_permitted"
    assert refused.value.status_code == 422
    assert "Choose one of: model-a, model-b." in refused.value.message


async def test_output_tokens_above_the_ceiling_is_its_code(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session, slug="acme")
    service = _agents(db_session, tenant, settings)
    ceiling = settings.openai_max_output_tokens

    with pytest.raises(ValidationError) as refused:
        await service.create(name="Sales", system_prompt=PROMPT, max_output_tokens=ceiling + 1)

    assert refused.value.error_code == "agent_output_tokens_exceed_ceiling"
    assert refused.value.status_code == 422
    assert (
        refused.value.message == f"max_output_tokens may not exceed {ceiling} on this deployment."
    )


async def test_a_scope_naming_a_gone_base_is_knowledge_scope_invalid(
    db_session: AsyncSession, settings: Settings
) -> None:
    tenant = await _tenant(db_session, slug="acme")
    service = _agents(db_session, tenant, settings)
    agent = await service.create(name="Sales", system_prompt=PROMPT)
    gone = uuid.uuid4()

    with pytest.raises(ValidationError) as refused:
        await service.grant_tool(
            agent.id, name=SEARCH_KNOWLEDGE_TOOL, config={"knowledge_base_ids": [str(gone)]}
        )

    assert refused.value.error_code == "knowledge_scope_invalid"
    assert refused.value.status_code == 422
    assert refused.value.details == {"knowledge_base_id": str(gone)}


async def test_another_workspaces_base_reads_the_same_as_a_gone_one(
    db_session: AsyncSession, settings: Settings
) -> None:
    """No code names another workspace's base: the same code, sentence and details shape."""
    mine = await _tenant(db_session, slug="mine")
    theirs = await _tenant(db_session, slug="theirs")
    their_base = await KnowledgeService(
        session=db_session, tenant_id=theirs.id
    ).create_knowledge_base(name="Theirs")
    service = _agents(db_session, mine, settings)
    agent = await service.create(name="Sales", system_prompt=PROMPT)

    with pytest.raises(ValidationError) as refused:
        await service.grant_tool(
            agent.id,
            name=SEARCH_KNOWLEDGE_TOOL,
            config={"knowledge_base_ids": [str(their_base.id)]},
        )

    assert refused.value.error_code == "knowledge_scope_invalid"
    assert refused.value.message == (
        "knowledge_base_ids names a knowledge base this workspace does not have."
    )


async def test_a_used_knowledge_base_name_is_a_409_not_a_500(db_session: AsyncSession) -> None:
    """FEB-40: create and rename both answer knowledge_base_name_exists."""
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    await knowledge.create_knowledge_base(name="Prices")
    other = await knowledge.create_knowledge_base(name="Policies")

    with pytest.raises(ConflictError) as created:
        await knowledge.create_knowledge_base(name="Prices")
    with pytest.raises(ConflictError) as renamed:
        await knowledge.update_knowledge_base(other.id, name=" Prices ")

    for refused in (created, renamed):
        assert refused.value.error_code == "knowledge_base_name_exists"
        assert refused.value.status_code == 409
        assert refused.value.message == "A knowledge base with that name already exists."
    kept = await KnowledgeBaseRepository(db_session, tenant_id=tenant.id).get_by_id(other.id)
    assert kept is not None
    assert kept.name == "Policies"


async def test_two_workspaces_may_both_name_a_base_prices(db_session: AsyncSession) -> None:
    first = await _tenant(db_session, slug="first")
    second = await _tenant(db_session, slug="second")
    await KnowledgeService(session=db_session, tenant_id=first.id).create_knowledge_base(
        name="Prices"
    )
    base = await KnowledgeService(session=db_session, tenant_id=second.id).create_knowledge_base(
        name="Prices"
    )
    assert base.name == "Prices"


async def test_keeping_a_bases_own_name_is_not_a_conflict(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="acme")
    knowledge = KnowledgeService(session=db_session, tenant_id=tenant.id)
    base = await knowledge.create_knowledge_base(name="Prices")
    updated = await knowledge.update_knowledge_base(base.id, name="Prices", description="New")
    assert updated.description == "New"
