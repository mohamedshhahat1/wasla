"""Invariants the tool path must leave true in the database, whatever ran before.

Each query counts rows that must not exist. They are written against the whole
database rather than one test's workspace, so a run of the tool suites with
`WASLA_TEST_KEEP_AI_DATA=1` - which keeps every workspace those suites created -
sweeps everything they did.

**Presence before absence.** Every invariant here passes trivially against an
empty table, so the kept-data run asserts first that the interesting states are
actually present: successful executions, rejected ones, denied ones, duplicates,
handoffs, lead mutations, follow-ups, concurrent turns and lifecycle refusals. A
zero is only evidence when something was there to be counted.
"""

from __future__ import annotations

import os
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.orchestrator import MAX_TOOL_CALLS_PER_RESPONSE, MAX_TOOL_CALLS_PER_TURN

pytestmark = pytest.mark.integration

INVARIANTS: Final[dict[str, str]] = {
    # --------------------------------------------------------- tenant scope
    "tool executions without a workspace": (
        "SELECT count(*) FROM tool_executions WHERE tenant_id IS NULL"
    ),
    "tool executions linked to another workspace's turn": """
        SELECT count(*) FROM tool_executions e
          JOIN agent_turns t ON t.id = e.agent_turn_id
         WHERE t.tenant_id <> e.tenant_id
    """,
    "tool executions linked to another workspace's conversation": """
        SELECT count(*) FROM tool_executions e
          JOIN conversations c ON c.id = e.conversation_id
         WHERE c.tenant_id <> e.tenant_id
    """,
    "tool executions linked to another workspace's agent": """
        SELECT count(*) FROM tool_executions e
          JOIN agents a ON a.id = e.agent_id
         WHERE a.tenant_id <> e.tenant_id
    """,
    # ------------------------------------------------------ record integrity
    "successful executions that never say when they finished": """
        SELECT count(*) FROM tool_executions
         WHERE state = 'succeeded' AND finished_at IS NULL
    """,
    "terminal executions left in a running state": """
        SELECT count(*) FROM tool_executions
         WHERE finished_at IS NOT NULL
           AND state NOT IN ('succeeded', 'rejected', 'failed', 'duplicate', 'ambiguous')
    """,
    "closed executions with no finish time": """
        SELECT count(*) FROM tool_executions
         WHERE state IN ('succeeded', 'rejected', 'failed', 'duplicate', 'ambiguous')
           AND finished_at IS NULL
    """,
    "executions that ran without ever being authorised": """
        SELECT count(*) FROM tool_executions
         WHERE started_at IS NOT NULL AND authorized_at IS NULL
    """,
    "executions that succeeded without being authorised": """
        SELECT count(*) FROM tool_executions
         WHERE state = 'succeeded' AND authorized_at IS NULL
    """,
    "refused executions that were nonetheless started": """
        SELECT count(*) FROM tool_executions
         WHERE state IN ('rejected', 'duplicate') AND started_at IS NOT NULL
    """,
    "refused or duplicate executions with no reason recorded": """
        SELECT count(*) FROM tool_executions
         WHERE state IN ('rejected', 'failed', 'duplicate') AND reason_code IS NULL
    """,
    "executions recording an argument value rather than a field name": """
        SELECT count(*) FROM tool_executions
         WHERE argument_fields IS NOT NULL
           AND jsonb_typeof(argument_fields) <> 'array'
    """,
    # ------------------------------------------------------------- identity
    "more than one execution claiming one provider call of one turn": """
        SELECT count(*) FROM (
            SELECT tenant_id, agent_turn_id, provider_call_id
              FROM tool_executions
             WHERE agent_turn_id IS NOT NULL AND provider_call_id IS NOT NULL
             GROUP BY tenant_id, agent_turn_id, provider_call_id
            HAVING count(*) FILTER (WHERE state <> 'duplicate') > 1
        ) repeated
    """,
    "duplicate executions that were allowed to do something": """
        SELECT count(*) FROM tool_executions
         WHERE state = 'duplicate' AND (started_at IS NOT NULL OR authorized_at IS NOT NULL)
    """,
    # --------------------------------------------------------------- bounds
    "turns that ran more tool calls than the turn budget allows": """
        SELECT count(*) FROM (
            SELECT agent_turn_id FROM tool_executions
             WHERE agent_turn_id IS NOT NULL AND state = 'succeeded'
             GROUP BY agent_turn_id HAVING count(*) > :turn_budget
        ) over_budget
    """,
    "responses that ran more tool calls than the response budget allows": """
        SELECT count(*) FROM (
            SELECT agent_turn_id, round_number FROM tool_executions
             WHERE agent_turn_id IS NOT NULL AND state = 'succeeded'
             GROUP BY agent_turn_id, round_number HAVING count(*) > :response_budget
        ) over_budget
    """,
    # ------------------------------------------------- what tools may leave
    "pending agent follow-ups on a conversation a person owns": """
        SELECT count(*) FROM follow_ups f
          JOIN conversations c ON c.id = f.conversation_id
         WHERE f.status = 'pending' AND f.created_by_kind = 'agent' AND c.mode = 'human'
    """,
    "pending agent follow-ups in a workspace that is not served": """
        SELECT count(*) FROM follow_ups f
          JOIN tenants w ON w.id = f.tenant_id
         WHERE f.status = 'pending' AND f.created_by_kind = 'agent'
           AND (w.status <> 'active' OR w.deleted_at IS NOT NULL)
    """,
    "more than one pending follow-up per conversation": """
        SELECT count(*) FROM (
            SELECT conversation_id FROM follow_ups WHERE status = 'pending'
             GROUP BY conversation_id HAVING count(*) > 1
        ) duplicated
    """,
    "conversations with more than one agent handoff audit row": """
        SELECT count(*) FROM (
            SELECT meta->>'conversation_id' AS conversation
              FROM audit_logs
             WHERE action = 'agent_handoff_requested'
             GROUP BY 1 HAVING count(*) > 1
        ) duplicated
    """,
    "conversations with more than one agent handoff analytics event": """
        SELECT count(*) FROM (
            SELECT conversation_id FROM analytics_events
             WHERE event_type = 'handoff' AND source = 'agent'
             GROUP BY conversation_id HAVING count(*) > 1
        ) duplicated
    """,
    "agent handoff audit rows on a conversation still answered by the AI": """
        SELECT count(*) FROM audit_logs a
          JOIN conversations c ON c.id::text = a.meta->>'conversation_id'
         WHERE a.action = 'agent_handoff_requested' AND c.mode <> 'human'
    """,
    "agent audit rows pointing outside their own workspace": """
        SELECT count(*) FROM audit_logs a
          JOIN conversations c ON c.id::text = a.meta->>'conversation_id'
         WHERE a.action LIKE 'agent\\_%' AND a.tenant_id IS NOT NULL
           AND c.tenant_id <> a.tenant_id
    """,
    # ---------------------------------------------- the turn survives a tool
    "turns stranded engaged while a tool of theirs was recorded": """
        SELECT count(*) FROM agent_turns t
         WHERE t.state = 'engaged'
           AND t.engaged_at < now() - interval '900 seconds'
           AND EXISTS (SELECT 1 FROM tool_executions e WHERE e.agent_turn_id = t.id)
    """,
    "successful mutating tool calls with no execution record behind them": """
        SELECT count(*) FROM audit_logs a
         WHERE a.action IN ('agent_lead_recorded', 'agent_follow_up_scheduled',
                            'agent_handoff_requested')
           AND a.occurred_at > now() - interval '1 hour'
           AND NOT EXISTS (
                SELECT 1 FROM tool_executions e
                 WHERE e.tenant_id = a.tenant_id
                   AND e.conversation_id::text = a.meta->>'conversation_id'
                   AND e.state = 'succeeded'
           )
    """,
}

PARAMETERS: Final[dict[str, object]] = {
    "turn_budget": MAX_TOOL_CALLS_PER_TURN,
    "response_budget": MAX_TOOL_CALLS_PER_RESPONSE,
}


@pytest.mark.parametrize("name", list(INVARIANTS))
async def test_the_tool_path_leaves_no_violation(engine: AsyncEngine, name: str) -> None:
    query = INVARIANTS[name]
    async with engine.connect() as connection:
        violations = await connection.scalar(text(query), PARAMETERS)
    assert violations == 0, f"{name}: {violations}"


@pytest.mark.skipif(
    os.environ.get("WASLA_TEST_KEEP_AI_DATA") != "1",
    reason="only a run that kept the tool suites' data has something to prove was swept",
)
async def test_a_kept_run_really_had_something_to_sweep(engine: AsyncEngine) -> None:
    """Non-vacuity: every invariant above passes trivially on an empty table."""
    async with engine.connect() as connection:
        counts = {
            "executions": await connection.scalar(text("SELECT count(*) FROM tool_executions")),
            "succeeded": await connection.scalar(
                text("SELECT count(*) FROM tool_executions WHERE state = 'succeeded'")
            ),
            "rejected": await connection.scalar(
                text("SELECT count(*) FROM tool_executions WHERE state = 'rejected'")
            ),
            "denied": await connection.scalar(
                text(
                    "SELECT count(*) FROM tool_executions "
                    "WHERE reason_code IN ('not_granted', 'tool_disabled')"
                )
            ),
            "duplicate": await connection.scalar(
                text("SELECT count(*) FROM tool_executions WHERE state = 'duplicate'")
            ),
            "lifecycle refusals": await connection.scalar(
                text(
                    "SELECT count(*) FROM tool_executions WHERE reason_code IN "
                    "('workspace_suspended', 'workspace_deleted', 'agent_disabled', "
                    "'conversation_human')"
                )
            ),
            "bounded refusals": await connection.scalar(
                text(
                    "SELECT count(*) FROM tool_executions WHERE reason_code IN "
                    "('response_call_limit', 'turn_call_limit', 'round_limit')"
                )
            ),
            "distinct reasons": await connection.scalar(
                text("SELECT count(DISTINCT reason_code) FROM tool_executions")
            ),
            "distinct tools": await connection.scalar(
                text("SELECT count(DISTINCT tool_name) FROM tool_executions")
            ),
            "leads by an agent": await connection.scalar(
                text("SELECT count(*) FROM leads WHERE source = 'agent'")
            ),
            "follow-ups by an agent": await connection.scalar(
                text("SELECT count(*) FROM follow_ups WHERE created_by_kind = 'agent'")
            ),
            "handoffs by an agent": await connection.scalar(
                text("SELECT count(*) FROM audit_logs WHERE action = 'agent_handoff_requested'")
            ),
            "conversations with two turns": await connection.scalar(
                text(
                    "SELECT count(*) FROM (SELECT conversation_id FROM agent_turns "
                    "GROUP BY conversation_id HAVING count(*) > 1) raced"
                )
            ),
        }

    missing = [name for name, value in counts.items() if not value]
    assert not missing, f"nothing to sweep for: {missing} ({counts})"
    assert counts["distinct reasons"] and counts["distinct reasons"] >= 6, counts
    assert counts["distinct tools"] and counts["distinct tools"] >= 3, counts


def test_the_literals_in_these_queries_match_the_constants_they_restate() -> None:
    """The SQL is literal on purpose, and these are the numbers it restates."""
    assert PARAMETERS["turn_budget"] == MAX_TOOL_CALLS_PER_TURN
    assert PARAMETERS["response_budget"] == MAX_TOOL_CALLS_PER_RESPONSE
    assert (
        "interval '900 seconds'"
        in INVARIANTS["turns stranded engaged while a tool of theirs was recorded"]
    )
