"""The follow-up slot belongs to the workspace's reminders while they run (FUS-03).

A real agent turn through the orchestrator: with sequences covering the
conversation's platform, `schedule_follow_up` is not described to the model
even though the agent is granted it; with sequences off - or never configured -
it is, exactly as before.
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert

from app.agents.registry import RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL
from app.db.models.follow_up_settings import FollowUpSettings
from tests.integration.ai_harness import FakeProviders, TurnRunner, text_response

pytestmark = pytest.mark.integration


async def _offered(
    ai_turns: TurnRunner, ai_providers: FakeProviders, *, enabled: bool | None
) -> set[str]:
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL])
    if enabled is not None:
        await ai_turns.execute(
            insert(FollowUpSettings).values(
                tenant_id=workspace.tenant_id,
                enabled=enabled,
                channel_types=["whatsapp"],
                steps=[{"delay_minutes": 5, "guidance": None}],
                version=1,
            )
        )
    conversation_id, ids = await ai_turns.write(workspace, ["How much is delivery?"])
    ai_providers.agent = lambda _request: _answer()
    await ai_turns.answer(workspace, conversation_id, ids[0])
    request = ai_providers.agent_requests[-1]
    return {tool["name"] for tool in request.get("tools", [])}


async def _answer() -> dict[str, object]:
    return text_response("Delivery is free.")


async def test_sequences_on_hide_the_tool(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    assert await _offered(ai_turns, ai_providers, enabled=True) == {RECORD_LEAD_TOOL}


@pytest.mark.parametrize("enabled", [False, None])
async def test_sequences_off_or_unconfigured_offer_it_as_before(
    ai_turns: TurnRunner, ai_providers: FakeProviders, enabled: bool | None
) -> None:
    assert await _offered(ai_turns, ai_providers, enabled=enabled) == {
        RECORD_LEAD_TOOL,
        SCHEDULE_FOLLOW_UP_TOOL,
    }
