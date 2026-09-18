"""The world changes while the model is thinking, and the executor looks again.

The audit's central finding about this subsystem: it was safe against the model
choosing the wrong thing and weak against the world changing underneath it.
Everything a *reply* depended on was re-read before sending; nothing a *tool*
depended on was re-read at all. So a workspace suspended for abuse or
non-payment, a workspace deleted and inside its retention window, an agent
switched off, a capability revoked and a colleague taking the conversation over
all failed to stop the tools of the round that followed - a lead was written, a
customer message was scheduled and audit rows were recorded, while the reply was
correctly suppressed, which is exactly what made it invisible (TOOL-03, TOOL-05,
TOOL-06, TOOL-07).

Every test here commits the change **from another connection, during the
inference**, and asserts it landed before reading what the tool did. That is the
shape of the race in production: an administrator clicks suspend, or a colleague
opens the conversation, while a model is composing.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import (
    HANDOFF_TOOL,
    RECORD_LEAD_TOOL,
    SCHEDULE_FOLLOW_UP_TOOL,
    ToolContext,
    build_default_registry,
)
from app.core.exceptions import ConflictError
from app.db.models.agent import Agent, AgentStatus, AgentTool
from app.db.models.agent_turn import TurnOutcome
from app.db.models.analytics import AnalyticsEventType
from app.db.models.audit import AuditLog
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
)
from app.db.models.enums import TenantStatus
from app.db.models.tenant import Tenant
from app.db.models.tool_execution import ToolExecutionReason, ToolExecutionState
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.conversation_repository import ConversationRepository
from app.services.lead_service import ExtractedLead, LeadService
from tests.integration.ai_harness import (
    FakeProviders,
    JsonObject,
    TurnRunner,
    Workspace,
    text_response,
    tool_call_response,
    tool_calls_response,
)

pytestmark = pytest.mark.integration

ANSWER = "Understood."
COLLEAGUE_REASON = "COLLEAGUE: VIP, call personally"

# One response asking for both writing tools, so a single run covers the two
# tools that can leave something behind.
WRITES: tuple[tuple[str, JsonObject], ...] = (
    (RECORD_LEAD_TOOL, {"name": "Ahmed", "interest": "finishing"}),
    (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "Still interested?"}),
)


def _during_inference(
    ai_turns: TurnRunner,
    change: Callable[[], Awaitable[None]],
    body: JsonObject,
) -> Callable[[JsonObject], Awaitable[JsonObject]]:
    """Commit `change` while the provider is 'composing', then ask for tools.

    The handler runs inside the provider call, which is exactly where the turn
    holds no transaction and no connection (ADR-080) - so a write from another
    connection lands and commits in the window this is about. Applied once: the
    later rounds answer normally.
    """
    applied = {"done": False}

    async def handle(_request: JsonObject) -> JsonObject:
        if applied["done"]:
            return text_response(ANSWER)
        await change()
        applied["done"] = True
        return body

    return handle


async def _suspend(ai_turns: TurnRunner, workspace: Workspace) -> None:
    await ai_turns.execute(
        update(Tenant).where(Tenant.id == workspace.tenant_id).values(status=TenantStatus.SUSPENDED)
    )


async def _soft_delete(ai_turns: TurnRunner, workspace: Workspace) -> None:
    await ai_turns.execute(
        update(Tenant).where(Tenant.id == workspace.tenant_id).values(deleted_at=datetime.now(UTC))
    )


async def _disable_agent(ai_turns: TurnRunner, workspace: Workspace) -> None:
    await ai_turns.execute(
        update(Agent).where(Agent.id == workspace.agent_id).values(status=AgentStatus.DISABLED)
    )


async def _revoke_grants(ai_turns: TurnRunner, workspace: Workspace) -> None:
    await ai_turns.execute(
        update(AgentTool).where(AgentTool.agent_id == workspace.agent_id).values(enabled=False)
    )


async def _take_over(ai_turns: TurnRunner, conversation_id: uuid.UUID) -> None:
    await ai_turns.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(mode=ConversationMode.HUMAN, handoff_reason=COLLEAGUE_REASON)
    )


LIFECYCLE_CHANGES: list[tuple[str, str, ToolExecutionReason, str]] = [
    (
        "workspace suspended",
        "suspend",
        ToolExecutionReason.WORKSPACE_SUSPENDED,
        "suppressed_workspace",
    ),
    (
        "workspace soft-deleted",
        "delete",
        ToolExecutionReason.WORKSPACE_DELETED,
        "suppressed_workspace",
    ),
    ("agent disabled", "disable", ToolExecutionReason.AGENT_DISABLED, "suppressed_agent"),
]


@pytest.mark.parametrize(
    ("case", "change", "reason", "outcome"),
    LIFECYCLE_CHANGES,
    ids=[row[0] for row in LIFECYCLE_CHANGES],
)
async def test_a_workspace_that_stopped_being_served_mid_inference_runs_no_tool(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    case: str,
    change: str,
    reason: ToolExecutionReason,
    outcome: str,
) -> None:
    """`docs/AI_AGENTS.md` promised "no inference, no tool and no message". Now true."""
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])

    applied: dict[str, Any] = {}

    async def apply() -> None:
        if change == "suspend":
            await _suspend(ai_turns, workspace)
        elif change == "delete":
            await _soft_delete(ai_turns, workspace)
        else:
            await _disable_agent(ai_turns, workspace)
        applied["state"] = await _lifecycle(ai_turns, workspace)

    ai_providers.agent = _during_inference(ai_turns, apply, tool_calls_response(WRITES))

    await ai_turns.answer(workspace, conversation_id, ids[0])

    # Non-vacuity: the change was committed and visible before the tools ran.
    assert applied["state"] == _expected_state(change), "the change never landed"

    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (RECORD_LEAD_TOOL, ToolExecutionState.REJECTED.value, reason.value),
        (SCHEDULE_FOLLOW_UP_TOOL, ToolExecutionState.REJECTED.value, reason.value),
    ]
    assert await ai_turns.leads(workspace.tenant_id) == []
    assert await ai_turns.follow_ups(workspace.tenant_id) == []
    assert await ai_turns.audit_actions(workspace.tenant_id) == []
    assert ai_providers.sends == []
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == [outcome]


async def _lifecycle(ai_turns: TurnRunner, workspace: Workspace) -> tuple[str, bool, str]:
    async with ai_turns.database.session() as session:
        row = (
            await session.execute(
                select(Tenant.status, Tenant.deleted_at, Agent.status)
                .select_from(Tenant)
                .join(Agent, Agent.tenant_id == Tenant.id)
                .where(Tenant.id == workspace.tenant_id, Agent.id == workspace.agent_id)
            )
        ).one()
    return (str(row[0]), row[1] is not None, str(row[2]))


def _expected_state(change: str) -> tuple[str, bool, str]:
    if change == "suspend":
        return ("suspended", False, "active")
    if change == "delete":
        return ("active", True, "active")
    return ("active", False, "disabled")


async def test_a_grant_revoked_during_the_inference_stops_the_call(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """A snapshot taken at turn start decides what is offered, never what may run.

    The grant set was read once, before the provider was called, and the
    execution-time check tested membership of that snapshot - so withdrawing a
    capability did not stop the turn already running (TOOL-05, TM03).
    """
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])

    enabled: dict[str, Any] = {}

    async def revoke() -> None:
        await _revoke_grants(ai_turns, workspace)
        async with ai_turns.database.session() as session:
            enabled["count"] = len(
                list(
                    await session.scalars(
                        select(AgentTool.id).where(
                            AgentTool.agent_id == workspace.agent_id,
                            AgentTool.enabled.is_(True),
                        )
                    )
                )
            )

    ai_providers.agent = _during_inference(
        ai_turns,
        revoke,
        tool_call_response(RECORD_LEAD_TOOL, {"name": "Ahmed"}),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert enabled["count"] == 0, "the grant was still enabled when the tool ran"
    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (
            RECORD_LEAD_TOOL,
            ToolExecutionState.REJECTED.value,
            ToolExecutionReason.TOOL_DISABLED.value,
        )
    ]
    assert await ai_turns.leads(workspace.tenant_id) == []
    assert await ai_turns.audit_actions(workspace.tenant_id) == []
    # The customer is still answered: a refused tool is not a lost turn.
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]


async def test_a_colleagues_takeover_outranks_every_stale_tool_call(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Human ownership wins (PD-TOOLS-01), for all three writing tools at once.

    `schedule_follow_up` had no human-mode guard at all, so an AI nudge landed
    on a conversation a colleague had just taken over (TOOL-06); the handoff
    tool overwrote the colleague's own note about why they took it and recorded
    an agent handoff that never happened (TOOL-07); only the lead tool refused,
    and it did so by an accident of the identity map (TOOL-21).
    """
    workspace = await ai_turns.workspace(
        grants=[RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL, HANDOFF_TOOL]
    )
    conversation_id, ids = await ai_turns.write(workspace, ["I am Ahmed"])

    seen: dict[str, Any] = {}

    async def take_over() -> None:
        await _take_over(ai_turns, conversation_id)
        conversation = await ai_turns.conversation(conversation_id)
        seen["mode"] = conversation.mode
        seen["reason"] = conversation.handoff_reason

    ai_providers.agent = _during_inference(
        ai_turns,
        take_over,
        tool_calls_response((*WRITES, (HANDOFF_TOOL, {"reason": "model reason"}))),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert seen["mode"] is ConversationMode.HUMAN, "the takeover never landed"
    assert seen["reason"] == COLLEAGUE_REASON

    human = ToolExecutionReason.CONVERSATION_HUMAN.value
    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (RECORD_LEAD_TOOL, ToolExecutionState.REJECTED.value, human),
        (SCHEDULE_FOLLOW_UP_TOOL, ToolExecutionState.REJECTED.value, human),
        (HANDOFF_TOOL, ToolExecutionState.REJECTED.value, human),
    ]

    conversation = await ai_turns.conversation(conversation_id)
    # The colleague's own words survive. This is the whole finding.
    assert conversation.handoff_reason == COLLEAGUE_REASON
    assert await ai_turns.leads(workspace.tenant_id) == []
    assert await ai_turns.follow_ups(workspace.tenant_id) == []
    assert await ai_turns.audit_actions(workspace.tenant_id) == []
    assert AnalyticsEventType.HANDOFF.value not in await ai_turns.analytics_types(
        workspace.tenant_id
    )
    assert ai_providers.sends == []
    assert await ai_turns.turn_outcomes(workspace.tenant_id) == [TurnOutcome.SUPPRESSED_HUMAN.value]


async def test_a_handoff_that_succeeds_stops_the_rest_of_its_own_response(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """`[handoff, schedule_follow_up]` used to hand over and then plant a nudge.

    Within one model response: the handoff set HUMAN and cancelled the nudges
    that existed, and the follow-up call that came after it created a fresh one
    on the conversation just handed over (TOOL-06, PD-TOOLS-02).
    """
    workspace = await ai_turns.workspace(grants=[HANDOFF_TOOL, SCHEDULE_FOLLOW_UP_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["get me a person"])
    ai_providers.agent = lambda _request: _answer(
        tool_calls_response(
            (
                (HANDOFF_TOOL, {"reason": "The customer asked for a person."}),
                (SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "Still there?"}),
            )
        )
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    conversation = await ai_turns.conversation(conversation_id)
    assert conversation.mode is ConversationMode.HUMAN, "the handoff did not happen"
    assert await ai_turns.follow_ups(workspace.tenant_id) == []
    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (HANDOFF_TOOL, ToolExecutionState.SUCCEEDED.value, None),
        (
            SCHEDULE_FOLLOW_UP_TOOL,
            ToolExecutionState.REJECTED.value,
            ToolExecutionReason.HANDOFF_COMPLETED.value,
        ),
    ]
    assert ai_providers.sends == []


async def _answer(body: JsonObject) -> JsonObject:
    return body


# ------------------------------------------- the service's own guard, directly


async def test_the_handoff_tool_refuses_a_conversation_a_colleague_already_owns(
    db_session: AsyncSession,
) -> None:
    """The second layer, reached the way a future caller would reach it (TOOL-07).

    The executor's per-call lifecycle read refuses this before the handler runs,
    which is why every test above goes through the worker. But the executor is
    one caller, and the rule - a colleague's own note about why they took a
    conversation is not a model's to overwrite - belongs to the tool as well.
    Driven through `ToolRegistry.run` with a server-built context, which is the
    shape any other caller would have.
    """
    tenant, conversation = await _human_conversation(db_session)
    context = ToolContext(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        session=db_session,
        embeddings=None,
    )

    # Non-vacuity: the colleague really owns it, and really wrote that.
    assert conversation.mode is ConversationMode.HUMAN
    assert conversation.handoff_reason == COLLEAGUE_REASON

    with pytest.raises(ConflictError):
        await build_default_registry().run(
            name=HANDOFF_TOOL,
            arguments={"reason": "model reason"},
            context=context,
        )
    await db_session.flush()

    assert conversation.handoff_reason == COLLEAGUE_REASON
    rows = await db_session.scalars(select(AuditLog.action).where(AuditLog.tenant_id == tenant.id))
    assert list(rows) == []


async def _human_conversation(session: AsyncSession) -> tuple[Tenant, Conversation]:
    """A conversation a colleague has taken over, with their own reason on it."""
    slug = f"owned-{uuid.uuid4().hex[:8]}"
    tenant = Tenant(name="Owned", slug=slug)
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id="555000555",
        display_phone_number="+201000000004",
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"2018{uuid.uuid4().int % 10_000_000:07d}")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
        mode=ConversationMode.HUMAN,
        handoff_reason=COLLEAGUE_REASON,
    )
    session.add(conversation)
    await session.flush()
    return tenant, conversation


async def test_the_lead_tool_re_reads_a_conversation_it_is_already_holding(
    ai_turns: TurnRunner,
) -> None:
    """Freshness on purpose, not by accident (TOOL-21).

    The takeover guard worked before this remediation because nothing kept a
    strong reference to the conversation across the inference, so the identity
    map had usually dropped it and the re-`select` genuinely re-read. That is a
    property of garbage collection. This test deliberately keeps the loaded
    instance alive - exactly what a refactor might do - commits a takeover from
    another connection, and asserts the service still sees the database.
    """
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, _ = await ai_turns.write(workspace, ["I am Ahmed"])

    async with ai_turns.database.session() as session:
        conversations = ConversationRepository(session, tenant_id=workspace.tenant_id)
        stale = await conversations.require_by_id(conversation_id)
        # Non-vacuity: the session is holding this row, and it says AI.
        loaded_as = stale.mode
        assert loaded_as is ConversationMode.AI
        assert stale in session

        await _take_over(ai_turns, conversation_id)

        service = LeadService(session=session, tenant_id=workspace.tenant_id)
        with pytest.raises(ConflictError):
            await service.capture_from_conversation(
                conversation_id=conversation_id,
                extracted=ExtractedLead(name="Ahmed"),
            )

        # The instance the session was holding is the one that was refreshed,
        # which is what `populate_existing` is for.
        assert stale.mode is ConversationMode.HUMAN

    assert await ai_turns.leads(workspace.tenant_id) == []


async def test_a_capability_granted_during_the_inference_is_not_usable_in_that_turn(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The turn-start snapshot decides what may run, and it is not only advisory.

    Two questions share one set of grants and they are not the same question.
    The snapshot says what the model was *offered*, and this request's `tools`
    array is that snapshot; the database says what is currently permitted. A
    grant added while the model was composing is permitted and was never
    offered, and a call naming it is a call the request did not describe - so
    the snapshot refuses it, and the database read cannot un-refuse it.
    """
    workspace = await ai_turns.workspace(grants=[RECORD_LEAD_TOOL])
    conversation_id, ids = await ai_turns.write(workspace, ["talk tomorrow"])

    granted: dict[str, Any] = {}

    async def grant_mid_turn() -> None:
        await ai_turns.execute(
            insert(AgentTool).values(
                id=uuid.uuid4(),
                tenant_id=workspace.tenant_id,
                agent_id=workspace.agent_id,
                name=SCHEDULE_FOLLOW_UP_TOOL,
                enabled=True,
            )
        )
        async with ai_turns.database.session() as session:
            granted["enabled"] = list(
                await session.scalars(
                    select(AgentTool.name).where(
                        AgentTool.agent_id == workspace.agent_id,
                        AgentTool.enabled.is_(True),
                    )
                )
            )

    ai_providers.agent = _during_inference(
        ai_turns,
        grant_mid_turn,
        tool_calls_response(
            ((SCHEDULE_FOLLOW_UP_TOOL, {"delay_minutes": 60, "message": "Still there?"}),)
        ),
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    # Non-vacuity: the grant really is enabled in the database now.
    assert sorted(granted["enabled"]) == sorted([RECORD_LEAD_TOOL, SCHEDULE_FOLLOW_UP_TOOL])

    assert await ai_turns.execution_outcomes(workspace.tenant_id) == [
        (
            SCHEDULE_FOLLOW_UP_TOOL,
            ToolExecutionState.REJECTED.value,
            ToolExecutionReason.NOT_GRANTED.value,
        )
    ]
    assert await ai_turns.follow_ups(workspace.tenant_id) == []
    assert [row.body for row in await ai_turns.outbound(workspace.tenant_id)] == [ANSWER]
