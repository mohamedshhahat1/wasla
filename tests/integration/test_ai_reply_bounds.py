"""A reply longer than WhatsApp allows still reaches the customer, once (AI-05).

Measured before this: a 6,000-character reply produced zero sends, zero outbound
rows, a turn stranded `ENGAGED`, a dead-lettered job - and the agent round's
tokens rolled back with the transaction, so the workspace was not even billed for
the inference it lost. And an agent row with no output ceiling sent the provider
no `max_output_tokens` at all.

The real worker, real PostgreSQL and Redis; the provider bodies are genuinely that
long, and the assertion is on the outbound row and on the request that left.
"""

from __future__ import annotations

import pytest

from app.agents.reply import ARABIC_CONTINUATION, ENGLISH_CONTINUATION
from app.services.messaging_service import WHATSAPP_TEXT_MAX_CHARS
from tests.integration.ai_harness import FakeProviders, TurnRunner, scripted, text_response

pytestmark = pytest.mark.integration

LONG_ENGLISH = "\n\n".join(
    f"Section {index}. " + "This explains our finishing packages in careful detail. " * 12
    for index in range(10)
)
LONG_ARABIC = "نقدم باقات تشطيب كاملة للشقق والفلل بأسعار مناسبة وخامات ممتازة. " * 120


async def test_a_reply_longer_than_whatsapp_allows_is_sent_once_and_bounded(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    assert len(LONG_ENGLISH) > 6_000  # non-vacuity: the model genuinely overran
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["tell me everything"])
    ai_providers.agent = scripted(text_response(LONG_ENGLISH))

    await ai_turns.answer(workspace, conversation_id, ids[0])

    outbound = await ai_turns.outbound(workspace.tenant_id)
    assert len(outbound) == 1
    assert len(ai_providers.sends) == 1
    body = outbound[0].body or ""
    assert len(body) <= WHATSAPP_TEXT_MAX_CHARS
    assert body.startswith("Section 0.")
    assert body.endswith(ENGLISH_CONTINUATION)
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]
    # The agent round's tokens are recorded - 3 + 11 in, 2 + 5 out - where the
    # failure used to leave only the classifier's.
    usage = await ai_turns.usage(workspace.tenant_id)
    assert usage["ai_input_token"] == 14
    assert usage["ai_output_token"] == 7


async def test_a_long_arabic_reply_is_offered_continuation_in_arabic(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    assert len(LONG_ARABIC) > WHATSAPP_TEXT_MAX_CHARS
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["عايز كل التفاصيل"])
    ai_providers.agent = scripted(text_response(LONG_ARABIC))

    await ai_turns.answer(workspace, conversation_id, ids[0])

    (sent,) = await ai_turns.outbound(workspace.tenant_id)
    assert (sent.body or "").endswith(ARABIC_CONTINUATION)
    assert len(sent.body or "") <= WHATSAPP_TEXT_MAX_CHARS


async def test_the_deployment_ceiling_holds_an_agent_configured_above_it(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    ai_turns.configure(openai_max_output_tokens=256)
    workspace = await ai_turns.workspace(max_output_tokens=4_096)
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 1
    assert ai_providers.agent_requests[0]["max_output_tokens"] == 256


async def test_an_agents_own_lower_ceiling_is_honoured(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace(max_output_tokens=300)
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.agent_requests[0]["max_output_tokens"] == 300


async def test_an_agent_created_without_a_ceiling_gets_one_in_the_database(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The harness names no ceiling; the column default supplies one."""
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.agent_requests[0]["max_output_tokens"] == 2_048
