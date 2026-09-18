"""What a tool call leaves in the logs and in the metrics (TOOL-10, TOOL-11, TOOL-15).

Two failures, both of which made an incident harder to see rather than easier.

**A failing tool published its SQL parameters.** The worker logged the exception
with its traceback, the JSON formatter included the formatted exception, and the
engine was built without `hide_parameters` - so `[parameters: (...)]` carried a
customer's name, an agent's handoff sentence and a follow-up body into the log
store. That is exactly the content the audit trail is designed never to copy
(ADR-052), reaching it by another route, with a different retention story, on
precisely the failures an operator greps.

**Nothing counted tool calls at all.** A crashed turn caused by a tool, an
authorization denial and a duplicate suppression were visible only as a generic
worker failure or a log line, so a grant being abused or one workspace's tool
failing could not be graphed, let alone alerted on.

The privacy test uses sentinels and proves the sentinel *reached the database*
before asserting it did not reach the logs. A test that only asserted absence
would pass against a tool that never ran.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.agents.registry import RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL
from app.core.telemetry import REDIS_COUNTERS, REDIS_HISTOGRAMS, set_counter_sink
from app.db.models.tenant import Tenant
from app.db.models.tool_execution import ToolExecutionState
from app.db.session import Database
from tests.integration.ai_harness import (
    REDIS_URL,
    FakeProviders,
    TurnRunner,
    scripted,
    settings_for,
    text_response,
    tool_calls_response,
)

pytestmark = pytest.mark.integration

TOOL_COUNTER = "wasla_agent_tool_executions_total"
TOOL_DURATION = "wasla_agent_tool_execution_duration_seconds"

# Values a customer or a model would recognise, chosen so a substring search
# over the whole log stream cannot match them by accident.
SENTINEL_NAME = "Zeynab-Sentinel-4f2a91"
SENTINEL_BODY = "Sentinel-follow-up-6b7c33"


async def test_a_tools_values_never_reach_the_log_stream(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A customer's name is on their lead, and nowhere an operator greps."""
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    ai_providers.agent = scripted(
        tool_calls_response(
            (
                (RECORD_LEAD_TOOL, {"name": SENTINEL_NAME}),
                (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": SENTINEL_BODY}),
            )
        ),
        text_response("Noted."),
    )

    with caplog.at_level(logging.DEBUG):
        await ai_turns.answer(workspace, conversation_id, ids[0])

    # Non-vacuity: the sentinels genuinely reached the database as bound
    # parameters, so their absence below is about the logs and not about a tool
    # that never ran.
    assert [lead.name for lead in await ai_turns.leads(workspace.tenant_id)] == [SENTINEL_NAME]
    assert [row.body for row in await ai_turns.follow_ups(workspace.tenant_id)] == [SENTINEL_BODY]

    written = "\n".join(
        record.getMessage() + json.dumps(record.__dict__, default=str) for record in caplog.records
    )
    assert SENTINEL_NAME not in written
    assert SENTINEL_BODY not in written


async def test_a_failing_tool_publishes_no_parameters(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The captured leak was on the failure path, which is where an operator looks."""
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    # Refused at the boundary now, and the refusal itself must not quote it back
    # into a log line either.
    ai_providers.agent = scripted(
        tool_calls_response(((RECORD_LEAD_TOOL, {"name": SENTINEL_NAME + "\x00"}),)),
        text_response("Noted."),
    )

    with caplog.at_level(logging.DEBUG):
        await ai_turns.answer(workspace, conversation_id, ids[0])

    outcomes = await ai_turns.execution_outcomes(workspace.tenant_id)
    assert [row[1] for row in outcomes] == [ToolExecutionState.REJECTED.value]

    written = "\n".join(
        record.getMessage() + json.dumps(record.__dict__, default=str) for record in caplog.records
    )
    assert SENTINEL_NAME not in written
    assert "parameters:" not in written


async def test_a_database_error_from_this_application_quotes_no_parameters(
    prepared_database: str,
) -> None:
    """The engine setting, asserted on a real error rather than on a keyword.

    `hide_parameters=True` is what stops SQLAlchemy formatting a statement's
    bound parameters into the exception's own message - and an exception message
    is a string that travels: into a traceback, into a dead-letter record, into
    whatever an operator pastes into a ticket. The two tests above prove nothing
    leaks through the *paths the tool layer takes today*; this one proves the
    setting itself, on the class the application builds its engine with, because
    the next path is not written yet.

    Provoked with a genuine unique violation, so the sentinel really is a bound
    parameter of the statement that failed.
    """
    database = Database(settings_for(prepared_database))
    slug = f"hidden-{uuid.uuid4().hex[:8]}"
    try:
        async with database.session() as session:
            session.add(Tenant(name=SENTINEL_NAME, slug=slug))
        with pytest.raises(IntegrityError) as failure:
            async with database.session() as session:
                session.add(Tenant(name=SENTINEL_NAME, slug=slug))
    finally:
        async with database.session() as session:
            await session.execute(delete(Tenant).where(Tenant.slug == slug))
        await database.dispose()

    # Non-vacuity: the failure really is the one carrying the sentinel.
    message = str(failure.value)
    assert "uq_tenants_slug" in message or "tenants" in message
    assert SENTINEL_NAME not in message
    assert "[parameters:" not in message
    assert "hide_parameters=True" in message


def test_the_tool_metrics_are_in_the_catalogue_that_is_scraped() -> None:
    """A counter written to a hash nobody reads is a metric that does not exist."""
    assert REDIS_COUNTERS[TOOL_COUNTER][1] == ("tool", "outcome")
    assert REDIS_HISTOGRAMS[TOOL_DURATION][1] == ("tool",)


async def test_every_call_is_counted_by_tool_and_by_outcome(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """One response, three endings, three series - and no identifier in a label."""
    redis: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    await redis.delete(f"metrics:counter:{TOOL_COUNTER}")
    set_counter_sink(redis)
    try:
        workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
        conversation_id, ids = await ai_turns.write(workspace, ["hello"])
        ai_providers.agent = scripted(
            tool_calls_response(
                (
                    (RECORD_LEAD_TOOL, {"name": "Ahmed"}),
                    (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "later"}),
                    (RECORD_LEAD_TOOL, {"budget_amount": "not a number"}),
                )
            ),
            text_response("Noted."),
        )

        await ai_turns.answer(workspace, conversation_id, ids[0])

        counted: dict[str, str] = await redis.hgetall(  # type: ignore[misc]
            f"metrics:counter:{TOOL_COUNTER}"
        )
    finally:
        set_counter_sink(None)
        await redis.aclose()

    assert counted, "no tool call was counted at all"
    assert counted.get(f"outcome=succeeded,tool={RECORD_LEAD_TOOL}") == "1"
    assert counted.get(f"outcome=rejected,tool={RECORD_LEAD_TOOL}") == "1"
    assert counted.get(f"outcome=denied,tool={SCHEDULE_FOLLOW_UP_TOOL}") == "1"

    # Label hygiene: nothing in this metric identifies a workspace, a
    # conversation, a turn or a call.
    forbidden = (str(workspace.tenant_id), str(conversation_id), str(ids[0]), "call_")
    for field in counted:
        assert not any(value in field for value in forbidden)


async def test_a_tool_name_the_model_invented_is_not_a_label_of_its_own(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """A label domain a stranger's message can extend is a cardinality leak."""
    redis: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    await redis.delete(f"metrics:counter:{TOOL_COUNTER}")
    set_counter_sink(redis)
    invented = f"summon_the_manager_{uuid.uuid4().hex[:8]}"
    try:
        workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
        conversation_id, ids = await ai_turns.write(workspace, ["hello"])
        ai_providers.agent = scripted(
            tool_calls_response(((invented, {"anything": "at all"}),)),
            text_response("Noted."),
        )

        await ai_turns.answer(workspace, conversation_id, ids[0])

        counted: dict[str, str] = await redis.hgetall(  # type: ignore[misc]
            f"metrics:counter:{TOOL_COUNTER}"
        )
    finally:
        set_counter_sink(None)
        await redis.aclose()

    assert counted.get("outcome=denied,tool=unknown") == "1"
    assert not any(invented in field for field in counted)


async def test_the_denial_log_line_names_the_conversation_it_is_about(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It logged the conversation id under the key `agent_id` (TOOL-15).

    The one signal that a model reached for a capability it does not have, and
    it pointed an investigator at an agent that does not exist.
    """
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    ai_providers.agent = scripted(
        tool_calls_response(((RECORD_LEAD_TOOL, {"name": "Ahmed"}),)),
        text_response("Noted."),
    )

    with caplog.at_level(logging.WARNING):
        await ai_turns.answer(workspace, conversation_id, ids[0])

    denials = [r for r in caplog.records if r.getMessage() == "agent.tool_not_granted"]
    assert len(denials) == 1, "the denial never happened"
    record = denials[0]
    assert record.conversation_id == str(conversation_id)  # type: ignore[attr-defined]
    assert record.agent_id == str(workspace.agent_id)  # type: ignore[attr-defined]
