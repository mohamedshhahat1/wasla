"""A colleague's takeover against every automated path that can hand a conversation over.

The audit proved the customer-facing half already held: once a takeover
commits, no stale AI path sends anything. It also proved the ownership half did
not (CRM-02): a sentiment escalation or the agent's handoff tool that decided
before the takeover and wrote after it replaced the colleague's reason with the
machine's, counted a second handoff, wrote an agent audit row for a handover the
agent did not make, and filed the turn as `handed_off`.

Every test here drives the real `AgentWorker` against committed rows and real
Redis, with the colleague's takeover committed by the real `InboxService` in a
session of its own at the exact moment the finding needs. The assertions are
both halves: nothing reached the customer, and the colleague's takeover stands
- their reason, their ownership, one handoff, and a turn that says a person owns
the conversation.

The last two tests pin each of the two independent guards on the send (TG-3).
The pair was only ever proved together, so either could have been deleted in a
refactor with every test still green.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.agents.orchestrator import AgentOrchestrator
from app.agents.registry import HANDOFF_TOOL, ToolRegistry
from app.db.models.agent_turn import TurnOutcome
from app.db.models.analytics import AnalyticsEvent, AnalyticsEventType
from app.db.models.audit import AuditAction
from app.db.models.conversation import ConversationMode
from app.db.models.enums import TenantRole
from app.db.models.membership import Membership
from app.db.models.sentiment import SentimentLabel
from app.db.models.user import User
from app.integrations.openai.types import AgentReply, TokenUsage
from app.services.inbox_service import InboxService
from tests.fakes import as_responses
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    Workspace,
    scripted,
    text_response,
    tool_call_response,
)

pytestmark = pytest.mark.integration

COLLEAGUE_REASON = "VIP - call personally"


class Colleagues:
    """Members of the harness's workspaces, removed afterwards."""

    def __init__(self, runner: TurnRunner) -> None:
        self._runner = runner
        self.created: list[uuid.UUID] = []

    async def add(self, workspace: Workspace) -> User:
        async with self._runner.database.session() as session:
            user = User(email=f"rep-{uuid.uuid4().hex[:10]}@handoff.test", hashed_password="x")
            session.add(user)
            await session.flush()
            session.add(
                Membership(tenant_id=workspace.tenant_id, user_id=user.id, role=TenantRole.MEMBER)
            )
        self.created.append(user.id)
        return user

    async def take_over(self, workspace: Workspace, conversation_id: uuid.UUID, user: User) -> None:
        async with self._runner.database.session() as session:
            taken = await InboxService(session=session, tenant_id=workspace.tenant_id).take_over(
                conversation_id=conversation_id, actor=user, reason=COLLEAGUE_REASON
            )
            assert taken.changed


@pytest_asyncio.fixture
async def colleagues(ai_turns: TurnRunner) -> AsyncIterator[Colleagues]:
    helper = Colleagues(ai_turns)
    try:
        yield helper
    finally:
        # After the harness removes the workspaces, whose memberships cascade.
        await ai_turns.cleanup()
        async with ai_turns.database.session() as session:
            if helper.created:
                await session.execute(delete(User).where(User.id.in_(helper.created)))


async def _handoff_sources(runner: TurnRunner, tenant_id: uuid.UUID) -> list[str]:
    async with runner.database.session() as session:
        rows = await session.scalars(
            select(AnalyticsEvent.source).where(
                AnalyticsEvent.tenant_id == tenant_id,
                AnalyticsEvent.event_type == AnalyticsEventType.HANDOFF,
            )
        )
        return sorted(str(row) for row in rows)


async def _assert_colleague_owns(
    runner: TurnRunner,
    workspace: Workspace,
    conversation_id: uuid.UUID,
    colleague: User,
) -> None:
    conversation = await runner.conversation(conversation_id)
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.handoff_reason == COLLEAGUE_REASON
    assert conversation.assigned_to_id == colleague.id
    assert await _handoff_sources(runner, workspace.tenant_id) == ["user"]
    actions = await runner.audit_actions(workspace.tenant_id)
    assert actions.count(AuditAction.CONVERSATION_TAKEN_OVER.value) == 1
    assert AuditAction.AGENT_HANDOFF_REQUESTED.value not in actions


# -------------------------------------------------------------- CRM-02


async def test_a_takeover_during_the_sentiment_reading_is_not_overwritten(
    ai_turns: TurnRunner, ai_providers: FakeProviders, colleagues: Colleagues
) -> None:
    """P2a: the classifier was reading when the colleague took over."""
    workspace = await ai_turns.workspace(escalation_sentiment=SentimentLabel.ANGRY)
    colleague = await colleagues.add(workspace)
    conversation_id, ids = await ai_turns.write(workspace, ["this is unacceptable"])
    ai_providers.reading = {
        "sentiment": "angry",
        "score": -0.9,
        "intent": "refund",
        "confidence": 0.95,
    }

    async def take_over_while_reading() -> None:
        await colleagues.take_over(workspace, conversation_id, colleague)

    ai_providers.sentiment_gate = take_over_while_reading
    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.sentiment == 1
    assert (ai_providers.inference, len(ai_providers.sends)) == (0, 0)
    await _assert_colleague_owns(ai_turns, workspace, conversation_id, colleague)
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == [TurnOutcome.SUPPRESSED_HUMAN.value]


async def test_a_handoff_tool_that_loses_to_a_takeover_hands_nothing_over(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    colleagues: Colleagues,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2c: the tool read AI, and the takeover committed before its write."""
    workspace = await ai_turns.workspace(grants=[HANDOFF_TOOL])
    colleague = await colleagues.add(workspace)
    conversation_id, ids = await ai_turns.write(workspace, ["I want a manager"])
    ai_providers.agent = scripted(
        tool_call_response(HANDOFF_TOOL, {"reason": "Customer asked for a manager"}),
        text_response("A colleague will be with you."),
    )

    original = InboxService.hand_off

    async def after_the_read(self: InboxService, **kwargs: Any) -> Any:
        # Between the tool's own mode check and its write: the window the
        # finding was proved in.
        await colleagues.take_over(workspace, conversation_id, colleague)
        return await original(self, **kwargs)

    monkeypatch.setattr(InboxService, "hand_off", after_the_read)
    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.sends == []
    # The turn stopped after the tool round rather than paying for another.
    assert ai_providers.inference == 1
    await _assert_colleague_owns(ai_turns, workspace, conversation_id, colleague)
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == [TurnOutcome.SUPPRESSED_HUMAN.value]
    [(tool, state, _reason)] = await ai_turns.execution_outcomes(workspace.tenant_id)
    assert tool == HANDOFF_TOOL
    assert state != "succeeded"


async def test_an_empty_response_handoff_and_a_takeover_make_one_handoff(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    colleagues: Colleagues,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The system fallback is conditional too: exactly one of the two writers wins.

    By the time the fallback hands the conversation over, the turn's own
    transaction has already updated the conversation row for its send, so a
    colleague's takeover racing it waits and commits second - and must then be
    the no-op a takeover of a human conversation is, not a second handoff.
    """
    workspace = await ai_turns.workspace()
    colleague = await colleagues.add(workspace)
    conversation_id, ids = await ai_turns.write(workspace, ["hello?"])
    ai_providers.agent = scripted(text_response(""))
    racing: list[asyncio.Task[bool]] = []

    async def take_over() -> bool:
        async with ai_turns.database.session() as session:
            taken = await InboxService(session=session, tenant_id=workspace.tenant_id).take_over(
                conversation_id=conversation_id, actor=colleague, reason=COLLEAGUE_REASON
            )
            return taken.changed

    original = InboxService.hand_off

    async def while_a_colleague_takes_over(self: InboxService, **kwargs: Any) -> Any:
        racing.append(asyncio.create_task(take_over()))
        # The colleague is started first but cannot get the row the turn holds.
        await asyncio.sleep(0)
        return await original(self, **kwargs)

    monkeypatch.setattr(InboxService, "hand_off", while_a_colleague_takes_over)
    await ai_turns.answer(workspace, conversation_id, ids[0])
    colleague_changed = await racing[0]

    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.HUMAN
    assert colleague_changed is False
    assert await _handoff_sources(ai_turns, workspace.tenant_id) == ["system"]
    actions = await ai_turns.audit_actions(workspace.tenant_id)
    assert actions.count(AuditAction.CONVERSATION_TAKEN_OVER.value) == 1


# ------------------------------------------------------------------ TG-3


class TakingOverProvider:
    """Commits a colleague's takeover while composing, then answers anyway."""

    def __init__(self, colleagues: Colleagues, workspace: Workspace, user: User) -> None:
        self._colleagues = colleagues
        self._workspace = workspace
        self._user = user
        self.conversation_id: uuid.UUID | None = None

    async def respond(self, **_: object) -> AgentReply:
        assert self.conversation_id is not None
        await self._colleagues.take_over(self._workspace, self.conversation_id, self._user)
        return AgentReply(
            text="Here is the answer.",
            tool_calls=(),
            usage=TokenUsage(input_tokens=5, output_tokens=5, total_tokens=10),
            response_id="resp_taken_over",
        )


async def test_the_orchestrator_withholds_a_reply_composed_across_a_takeover(
    ai_turns: TurnRunner, colleagues: Colleagues
) -> None:
    """The first guard on its own (M21): the orchestrator's re-read after inference.

    Driven through the orchestrator alone, so the worker's final pre-send check
    - the second guard - is not in the path and cannot cover for this one.
    """
    workspace = await ai_turns.workspace()
    colleague = await colleagues.add(workspace)
    conversation_id, _ = await ai_turns.write(workspace, ["hello?"])
    provider = TakingOverProvider(colleagues, workspace, colleague)
    provider.conversation_id = conversation_id

    async with ai_turns.database.session() as session:
        outcome = await AgentOrchestrator(
            session=session,
            tenant_id=workspace.tenant_id,
            client=as_responses(provider),
            registry=ToolRegistry(),
        ).answer(conversation_id=conversation_id)

    assert outcome.reply is None
    assert outcome.outcome is TurnOutcome.SUPPRESSED_HUMAN


async def test_the_final_check_refuses_a_takeover_after_the_orchestrator_looked(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    colleagues: Colleagues,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second guard on its own (M22): the worker's pre-send re-read.

    The takeover commits after the orchestrator returned its reply - after its
    own re-read had already said "still the AI's" - and before the send. Only
    the final check stands between that reply and the customer.
    """
    workspace = await ai_turns.workspace()
    colleague = await colleagues.add(workspace)
    conversation_id, ids = await ai_turns.write(workspace, ["hello?"])
    original = AgentOrchestrator.answer

    async def then_taken_over(self: AgentOrchestrator, **kwargs: Any) -> Any:
        outcome = await original(self, **kwargs)
        assert outcome.reply, "the orchestrator must have produced a reply to withhold"
        await colleagues.take_over(workspace, conversation_id, colleague)
        return outcome

    monkeypatch.setattr(AgentOrchestrator, "answer", then_taken_over)
    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 1
    assert ai_providers.sends == []
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == [TurnOutcome.SUPPRESSED_HUMAN.value]
