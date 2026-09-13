"""Invariants the AI path must leave true in the database, whatever ran before.

Each query below counts rows that must not exist. They are written against the
whole database rather than one test's workspace, so a run of the AI suites with
`WASLA_TEST_KEEP_AI_DATA=1` - which keeps every workspace those suites created -
sweeps everything they did, and an ordinary run sweeps whatever is there.

The last two are the ones specific to the remediation: a reply only where a
turn says it replied, and exactly one customer turn charged per turn that
engaged a provider.
"""

from __future__ import annotations

import os
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.integrations.openai.types import MAX_REPORTED_TOKENS
from app.repositories.agent_turn_repository import STRANDED_TURN_AFTER

pytestmark = pytest.mark.integration

INVARIANTS: Final[dict[str, str]] = {
    "duplicate conversation sequence numbers": """
        SELECT count(*) FROM (
            SELECT conversation_id, sequence FROM messages
             GROUP BY conversation_id, sequence HAVING count(*) > 1
        ) duplicated
    """,
    "messages without a sequence": "SELECT count(*) FROM messages WHERE sequence IS NULL",
    "duplicate agent turns for one trigger": """
        SELECT count(*) FROM (
            SELECT tenant_id, trigger_message_id FROM agent_turns
             GROUP BY tenant_id, trigger_message_id HAVING count(*) > 1
        ) duplicated
    """,
    "agent turns referencing another workspace's conversation": """
        SELECT count(*) FROM agent_turns t
          JOIN conversations c ON c.id = t.conversation_id
         WHERE c.tenant_id <> t.tenant_id
    """,
    "engaged turns stranded past any healthy turn's length": """
        SELECT count(*) FROM agent_turns
         WHERE state = 'engaged'
           AND engaged_at < now() - interval '900 seconds'
    """,
    # `claimed_by` is set on every turn a worker claims and on no row a test
    # inserts directly, so it separates "the worker forgot to say how this ended"
    # from fixtures that model a turn's state by hand.
    "completed turns a worker claimed and finished with no outcome": """
        SELECT count(*) FROM agent_turns
         WHERE state = 'completed' AND outcome IS NULL AND claimed_by IS NOT NULL
    """,
    "agent replies with no idempotency key": """
        SELECT count(*) FROM messages
         WHERE direction = 'outbound' AND origin = 'agent' AND idempotency_key IS NULL
    """,
    "duplicate sentiment readings for one message": """
        SELECT count(*) FROM (
            SELECT message_id FROM message_sentiments
             GROUP BY message_id HAVING count(*) > 1
        ) duplicated
    """,
    "sentiment readings crossing a workspace": """
        SELECT count(*) FROM message_sentiments s
          JOIN messages m ON m.id = s.message_id
         WHERE m.tenant_id <> s.tenant_id
    """,
    "usage rows with a quantity of zero or less": (
        "SELECT count(*) FROM usage_events WHERE quantity <= 0"
    ),
    "token usage rows above any plausible provider count": """
        SELECT count(*) FROM usage_events
         WHERE event_type IN ('ai_input_token', 'ai_output_token')
           AND quantity > 2000000
    """,
    "agent turns surviving a purged workspace": """
        SELECT count(*) FROM agent_turns t
          JOIN tenants w ON w.id = t.tenant_id
         WHERE w.purged_at IS NOT NULL
    """,
    "automated replies in a suspended or deleted workspace": """
        SELECT count(*) FROM messages m
          JOIN tenants w ON w.id = m.tenant_id
         WHERE m.direction = 'outbound' AND m.origin = 'agent'
           AND (w.status <> 'active' OR w.deleted_at IS NOT NULL)
    """,
    "a reply sent for a turn whose outcome says it was not": """
        SELECT count(*) FROM agent_turns t
          JOIN messages m
            ON m.tenant_id = t.tenant_id
           AND m.idempotency_key = 'agent-turn:' || t.trigger_message_id::text
         WHERE t.outcome IN (
                'handed_off', 'escalated', 'quota_blocked', 'nothing_to_answer',
                'suppressed_human', 'suppressed_agent', 'suppressed_workspace',
                'suppressed_closed', 'suppressed_channel'
           )
    """,
    "customer turns charged other than once per engaged turn": """
        SELECT count(*) FROM (
            SELECT w.id,
                   (SELECT coalesce(sum(u.quantity), 0) FROM usage_events u
                     WHERE u.tenant_id = w.id AND u.event_type = 'ai_turn') AS charged,
                   (SELECT count(*) FROM agent_turns t
                     WHERE t.tenant_id = w.id AND t.engaged_at IS NOT NULL
                       AND t.outcome IS NOT NULL) AS engaged
              FROM tenants w
             WHERE EXISTS (SELECT 1 FROM agent_turns t
                            WHERE t.tenant_id = w.id AND t.outcome IS NOT NULL)
        ) per_workspace
         WHERE charged <> engaged
    """,
}


@pytest.mark.parametrize("name", list(INVARIANTS))
async def test_the_ai_path_leaves_no_violation(engine: AsyncEngine, name: str) -> None:
    async with engine.connect() as connection:
        violations = await connection.scalar(text(INVARIANTS[name]))
    assert violations == 0, f"{name}: {violations}"


@pytest.mark.skipif(
    os.environ.get("WASLA_TEST_KEEP_AI_DATA") != "1",
    reason="only a run that kept the AI suites' data has something to prove was swept",
)
async def test_a_kept_run_really_had_something_to_sweep(engine: AsyncEngine) -> None:
    """Non-vacuity: every invariant above passes trivially on an empty database."""
    async with engine.connect() as connection:
        turns = await connection.scalar(
            text("SELECT count(*) FROM agent_turns WHERE outcome IS NOT NULL")
        )
        replies = await connection.scalar(
            text("SELECT count(*) FROM messages WHERE origin = 'agent'")
        )
        charged = await connection.scalar(
            text("SELECT coalesce(sum(quantity), 0) FROM usage_events WHERE event_type = 'ai_turn'")
        )
        outcomes = await connection.scalar(
            text("SELECT count(DISTINCT outcome) FROM agent_turns WHERE outcome IS NOT NULL")
        )
    assert turns and turns >= 20, turns
    assert replies and replies >= 10, replies
    assert charged and charged >= 10, charged
    assert outcomes and outcomes >= 8, outcomes


def test_the_literals_in_these_queries_match_the_constants_they_restate() -> None:
    """The SQL is literal on purpose, and these are the numbers it restates."""
    assert STRANDED_TURN_AFTER.total_seconds() == 900
    assert MAX_REPORTED_TOKENS == 2_000_000
    assert (
        "'suppressed_closed'" in INVARIANTS["a reply sent for a turn whose outcome says it was not"]
    )
