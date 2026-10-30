"""The query contract and the agent's knowledge scope, through the real round loop (RC-5, RC-6).

A scripted model at the Responses transport writes the tool call; everything
from there to the bytes the next round sends - argument validation, the tool,
the embedding batch, the repository search, the context - is production code.
The embeddings are the suite's fake at their transport.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import update

from app.core.exceptions import ValidationError
from app.db.models.agent import AgentTool
from app.services.agent_service import AgentService
from app.services.knowledge_service import KnowledgeService
from tests.fake_embeddings import FakeEmbeddings
from tests.fakes import as_embeddings
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    scripted,
    text_response,
    tool_call_response,
)

pytestmark = pytest.mark.integration

SEARCH = "search_knowledge"
PRICE_LIST = "Classic sofa - price 950 EGP - warranty two years"
ARABIC = "بكام الكنبة الكلاسيك؟"
HR = "HR handbook: sofa model 10 allowance for staff is 400 EGP price per year."
SALES = "Sales sheet: corner sofa model 22 - price 9,000 EGP - delivery included."


async def _index(
    runner: TurnRunner, tenant_id: uuid.UUID, *, base: str, title: str, body: str
) -> uuid.UUID:
    async with runner.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id)
        bases = {b.name: b for b in await knowledge.list_knowledge_bases()}
        kb = bases.get(base) or await knowledge.create_knowledge_base(name=base)
        view, _ = await knowledge.submit(knowledge_base_id=kb.id, title=title, raw=body)
        result = await knowledge.ingest(
            document_id=view.document.id, embeddings=as_embeddings(FakeEmbeddings())
        )
        assert result.chunks_written > 0
        return kb.id


def _outputs(providers: FakeProviders) -> list[str]:
    return [
        item["output"]
        for request in providers.agent_requests
        for item in request["input"]
        if item.get("type") == "function_call_output"
    ]


async def _turn(
    runner: TurnRunner, providers: FakeProviders, workspace: Any, **arguments: Any
) -> str:
    providers.agent = scripted(
        tool_call_response(SEARCH, arguments), text_response("Here is what I found.")
    )
    conversation_id, (message_id,) = await runner.write(workspace, ["price?"])
    await runner.answer(workspace, conversation_id, message_id)
    (output,) = _outputs(providers)
    return output


async def test_an_arabic_question_about_an_english_list_reaches_it_through_the_alternate_query(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """M-R51: the alternate query is embedded and searched, in one batched request.

    With the query-embedding cache off: on (RQ-26), the second turn's Arabic
    query is a hit and only the alternate is sent - covered by the cache's own
    tests.
    """
    ai_turns.configure(rag_query_embedding_cache=False)
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id, base="General", title="Prices", body=PRICE_LIST)

    arabic_only = await _turn(ai_turns, ai_providers, workspace, query=ARABIC)
    ai_providers.agent_requests.clear()
    ai_providers.embedding_requests.clear()
    both = await _turn(
        ai_turns,
        ai_providers,
        workspace,
        query=ARABIC,
        alternate_query="classic sofa price",
    )

    # Non-vacuity: the Arabic query alone does not reach the English row.
    assert "950 EGP" not in arabic_only
    assert "950 EGP" in both
    # One embedding request carrying both queries.
    (request,) = ai_providers.embedding_requests
    assert request["input"] == [ARABIC, "classic sofa price"]


async def test_the_tool_schema_carries_the_query_contract(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _turn(ai_turns, ai_providers, workspace, query="Sofa model 10 price")
    (tool,) = ai_providers.agent_requests[0]["tools"]
    properties = tool["parameters"]["properties"]
    assert "standalone" in properties["query"]["description"]
    assert (
        "alternate_query" in properties and "alternate_query" not in tool["parameters"]["required"]
    )
    assert properties["max_results"]["maximum"] == 6


async def test_a_scoped_agent_never_receives_another_knowledge_bases_passages(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """M-R35: the HR passage is the nearest one, so only the scope keeps it out."""
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id, base="HR", title="Handbook", body=HR)
    sales = await _index(ai_turns, workspace.tenant_id, base="Sales", title="Sheet", body=SALES)
    query = "sofa model 10 price"

    unscoped = await _turn(ai_turns, ai_providers, workspace, query=query)
    # Non-vacuity: unscoped, HR comes first.
    assert json.loads(unscoped)["knowledge_sources"][0]["title"] == "Handbook"

    async with ai_turns.database.session() as session:
        await AgentService(
            session=session, settings=ai_turns.settings, tenant_id=workspace.tenant_id
        ).grant_tool(workspace.agent_id, name=SEARCH, config={"knowledge_base_ids": [str(sales)]})
    ai_providers.agent_requests.clear()
    scoped = await _turn(ai_turns, ai_providers, workspace, query=query)

    titles = [s["title"] for s in json.loads(scoped)["knowledge_sources"]]
    assert titles == ["Sheet"]
    assert "allowance" not in scoped


async def test_a_scope_is_validated_against_the_workspace(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace(grants=[SEARCH])
    other = await ai_turns.workspace(grants=[SEARCH])
    foreign = await _index(ai_turns, other.tenant_id, base="Sales", title="Sheet", body=SALES)
    async with ai_turns.database.session() as session:
        agents = AgentService(
            session=session, settings=ai_turns.settings, tenant_id=workspace.tenant_id
        )
        for config, words in (
            ({"knowledge_base_ids": []}, "at least one"),
            ({"knowledge_base_ids": [str(foreign)]}, "does not have"),
            ({"knowledge_base_ids": [str(uuid.uuid4())]}, "does not have"),
            ({"knowledge_base_ids": ["not-an-id"]}, "knowledge base ids"),
            ({"top_k": 9}, "only setting"),
        ):
            with pytest.raises(ValidationError) as refused:
                await agents.grant_tool(workspace.agent_id, name=SEARCH, config=config)
            assert words in str(refused.value)
            # A foreign id and an unknown id read the same (G3).
            assert "another workspace" not in str(refused.value).lower()
        with pytest.raises(ValidationError):
            await agents.grant_tool(
                workspace.agent_id, name="request_human_handoff", config={"knowledge_base_ids": []}
            )


async def test_a_scope_written_by_hand_that_is_not_a_list_scopes_to_nothing(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace(grants=[SEARCH])
    await _index(ai_turns, workspace.tenant_id, base="General", title="Prices", body=PRICE_LIST)
    async with ai_turns.database.session() as session:
        await session.execute(
            update(AgentTool)
            .where(AgentTool.agent_id == workspace.agent_id, AgentTool.name == SEARCH)
            .values(config={"knowledge_base_ids": "everything"})
        )
        await session.commit()
    output = await _turn(ai_turns, ai_providers, workspace, query="classic sofa price")
    assert "No information about this was found" in output
