"""What reaches the provider belongs to one workspace and one conversation.

The AI audit attacked this boundary hardest and it held; these tests keep it held.
Every assertion is made against the *serialized provider request* - the bytes that
left for OpenAI - rather than against a reply or a repository return value,
because a leak into the prompt is invisible in everything else.

Markers are planted in the places a leak would come from: the workspace's own
system prompt and the conversation history. Each request must carry its own
markers and none of the other side's, and must carry no internal identifier at
all.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.integration.ai_harness import FakeProviders, JsonObject, TurnRunner

pytestmark = pytest.mark.integration


def _wire(request: JsonObject) -> str:
    return json.dumps(request, ensure_ascii=False)


async def test_two_workspaces_interleaved_never_share_a_prompt(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """ALPHA, BETA, ALPHA, BETA through one worker against one database."""
    alpha = await ai_turns.workspace(system_prompt="ALPHA_PRIVATE_8472 You sell paint.")
    beta = await ai_turns.workspace(system_prompt="BETA_PRIVATE_5291 You sell tiles.")
    markers = {
        "alpha": ("ALPHA_PRIVATE_8472", "A_SECRET_MARKER"),
        "beta": ("BETA_PRIVATE_5291", "B_SECRET_MARKER"),
    }
    order: list[str] = []
    for round_number in range(2):
        for name, workspace, wa_id in (
            ("alpha", alpha, "201555000301"),
            ("beta", beta, "201555000302"),
        ):
            conversation_id, ids = await ai_turns.write(
                workspace,
                [f"{markers[name][1]} round {round_number}"],
                wa_id=wa_id,
            )
            await ai_turns.answer(workspace, conversation_id, ids[0])
            order.append(name)

    assert ai_providers.inference == 4, "every interleaved turn reached the provider"
    for index, name in enumerate(order):
        other = "beta" if name == "alpha" else "alpha"
        sent = _wire(ai_providers.agent_requests[index])
        assert all(marker in sent for marker in markers[name])
        assert not any(marker in sent for marker in markers[other])


async def test_two_conversations_in_one_workspace_do_not_merge(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    first, first_ids = await ai_turns.write(
        workspace, ["C1_MARKER my kitchen"], wa_id="201555000401"
    )
    second, second_ids = await ai_turns.write(
        workspace, ["C2_MARKER my bathroom"], wa_id="201555000402"
    )

    await ai_turns.answer(workspace, first, first_ids[0])
    await ai_turns.answer(workspace, second, second_ids[0])

    assert ai_providers.inference == 2
    to_first, to_second = (_wire(request) for request in ai_providers.agent_requests)
    assert "C1_MARKER" in to_first and "C2_MARKER" not in to_first
    assert "C2_MARKER" in to_second and "C1_MARKER" not in to_second


async def test_no_internal_identifier_or_contact_detail_reaches_the_provider(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The provider receives the prompt and the words, and nothing that identifies anyone."""
    workspace = await ai_turns.workspace()
    wa_id = "201555000499"
    conversation_id, ids = await ai_turns.write(workspace, ["how much is it?"], wa_id=wa_id)

    await ai_turns.answer(workspace, conversation_id, ids[0])

    requests: list[Any] = [*ai_providers.agent_requests, *ai_providers.sentiment_requests]
    assert len(requests) == 2, "both provider calls were made"
    for request in requests:
        sent = _wire(request)
        for identifier in (
            str(workspace.tenant_id),
            str(workspace.agent_id),
            str(workspace.account_id),
            str(conversation_id),
            str(ids[0]),
            wa_id,
            workspace.phone_number_id,
        ):
            assert identifier not in sent


async def test_the_customer_is_the_user_and_the_business_is_the_assistant(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["CUSTOMER_WORDS first"])
    await ai_turns.answer(workspace, conversation_id, ids[0])
    _, later = await ai_turns.write(workspace, ["CUSTOMER_WORDS second"])

    await ai_turns.answer(workspace, conversation_id, later[0])

    roles = [(item["role"], item["content"]) for item in ai_providers.agent_requests[-1]["input"]]
    assert roles == [
        ("user", "CUSTOMER_WORDS first"),
        ("assistant", "Yes, we are here."),
        ("user", "CUSTOMER_WORDS second"),
    ]
