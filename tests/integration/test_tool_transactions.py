"""What the turn is holding while a tool waits on somebody else's API (TOOL-09).

ADR-080 removed one thing from the inference path: a pooled connection held
across a provider call. The effective concurrency of an agent turn is the queue
depth rather than `pool_size + max_overflow` precisely because `released`
commits and hands the connection back before every round.

Inside a tool, that had come back. `search_knowledge` embeds the question, and
when it followed a write in the same model response it made that HTTP call with
a checked-out connection, an open transaction and a `RowExclusiveLock` on
`leads` - for up to three attempts with backoff. On a small pool that is the
documented bottleneck; the held lock also blocks the very concurrent turn
TOOL-02 is about.

Measured from inside the fake embeddings provider, on a connection of its own,
which is the only place the question can be asked: "what is this application
holding *right now*, while it waits for OpenAI?"
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.agents.registry import RECORD_LEAD_TOOL, SEARCH_KNOWLEDGE_TOOL
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    embedding_response,
    scripted,
    text_response,
    tool_calls_response,
)

pytestmark = pytest.mark.integration

APPLICATION = "wasla"


async def _held(url: str) -> dict[str, int]:
    """What the application is holding at this instant, from outside its pool.

    A connection of its own, opened and closed per question, so the observer
    can never be mistaken for the thing observed. Activity statistics are
    snapshotted per transaction, so this takes a fresh one.
    """
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as observer:
            idle = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND application_name = :name "
                    "AND state = 'idle in transaction'"
                ),
                {"name": APPLICATION},
            )
            locks = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_locks l "
                    "JOIN pg_class c ON c.oid = l.relation "
                    "WHERE c.relname = 'leads' AND l.mode = 'RowExclusiveLock'"
                )
            )
        return {"idle_in_transaction": int(idle or 0), "lead_locks": int(locks or 0)}
    finally:
        await engine.dispose()


async def test_a_search_after_a_write_holds_no_connection_across_the_embedding_call(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The shape that broke it: a write, then a search, in one model response."""
    observed: dict[str, Any] = {}

    async def watching(body: dict[str, Any]) -> httpx.Response:
        observed.update(await _held(ai_turns.url))
        observed["entered"] = True
        return embedding_response(body)

    ai_providers.embeddings = watching
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL, SEARCH_KNOWLEDGE_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed; what do you charge?"])
    ai_providers.agent = scripted(
        tool_calls_response(
            (
                (RECORD_LEAD_TOOL, {"name": "Ahmed"}),
                (SEARCH_KNOWLEDGE_TOOL, {"query": "prices"}),
            )
        ),
        text_response("Here is what we charge."),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    # Non-vacuity: the embedding call genuinely happened, after a write that
    # genuinely took the lock this is about.
    assert observed.get("entered") is True, "the embedding provider was never reached"
    assert len(ai_providers.embedding_requests) == 1
    assert len(await ai_turns.leads(workspace.tenant_id)) == 1

    assert (
        observed["idle_in_transaction"] == 0
    ), "the turn held an open transaction across the embedding call"
    assert observed["lead_locks"] == 0, "a write lock on leads was held across the embedding call"

    # And the search still worked, which is the point of doing any of this: its
    # output reached the model on the next round. This workspace has indexed
    # nothing, so what it reached the model with is the explicit "nothing was
    # found" instruction rather than passages - which is the same path.
    outputs = [
        item["output"]
        for item in ai_providers.agent_requests[1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert any("knowledge base" in output for output in outputs)
