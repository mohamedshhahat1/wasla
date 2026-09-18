"""Whether an agent may still act on a conversation, read from the database now.

Nothing on the AI path used to read a workspace's lifecycle (AI-06). Suspension
and deletion were enforced at the API authorization layer - which every human
request passes through and no worker does - so a suspended workspace, and a
deleted one for the whole of its retention window, kept running billable
inference and kept messaging its customers. And after a long inference only the
conversation's *mode* was read again (AI-07): an agent disabled, a conversation
closed or a workspace suspended while the model was composing still got its reply
sent.

One question, asked by the worker before a turn is charged and again immediately
before a reply leaves - and, since TOOL-03, by the tool executor immediately
before **every tool call**. That last one is the gap this module was written for
and did not cover: a workspace suspended, soft-deleted, or an agent disabled
while the model was composing did not stop the tools of the round that followed,
so a workspace the platform had stopped serving went on writing CRM records and
scheduling customer messages. The reply was correctly suppressed, which is what
made it invisible.

Two readings of one row, because two callers need different vocabularies for the
same facts: a turn ends in a `TurnOutcome` and a refused tool call records a
`ToolExecutionReason`, and the reason vocabulary is finer - it tells a suspended
workspace from a deleted one, which is the difference between a billing dispute
and a customer who left. `serving_state` does the query; the two mappers are what
the callers use.

**Columns, never objects.** The conversation and agent are already in the turn's
session, and a `select` returning a mapped entity hands back the instance in the
identity map with the attributes it was loaded with - which is exactly the stale
snapshot this exists to see past. Asking for column values gets the rows as they
are now. The same technique `AgentOrchestrator._taken_over` uses, for the same
reason, extended to everything a reply - or a tool - depends on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent import Agent, AgentStatus
from app.db.models.agent_turn import TurnOutcome
from app.db.models.conversation import Conversation, ConversationMode, ConversationStatus
from app.db.models.enums import TenantStatus
from app.db.models.tenant import Tenant
from app.db.models.tool_execution import ToolExecutionReason
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus


@dataclass(frozen=True, slots=True)
class ServingState:
    """Everything that decides whether an agent may act, as the rows read now.

    `missing` is its own fact rather than a null-shaped guess: a conversation
    that no longer exists, or belongs to another workspace, is refused rather
    than read, and the two callers phrase that refusal differently.
    """

    missing: bool
    workspace_active: bool = True
    workspace_deleted: bool = False
    human: bool = False
    conversation_closed: bool = False
    agent_active: bool = True
    channel_available: bool = True


async def serving_state(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    agent_id: uuid.UUID | None,
) -> ServingState:
    """One indexed read of everything an agent's authority depends on.

    Tenant-scoped on the conversation, so a conversation id from another
    workspace matches nothing and is reported missing rather than read.
    """
    row = (
        await session.execute(
            select(
                Tenant.status,
                Tenant.deleted_at,
                Conversation.mode,
                Conversation.status,
                Agent.status,
                WhatsAppAccount.status,
                WhatsAppAccount.released_at,
            )
            .select_from(Conversation)
            .join(Tenant, Tenant.id == Conversation.tenant_id)
            .join(
                WhatsAppAccount,
                and_(
                    WhatsAppAccount.id == Conversation.account_id,
                    WhatsAppAccount.tenant_id == Conversation.tenant_id,
                ),
            )
            .outerjoin(
                Agent,
                and_(Agent.id == agent_id, Agent.tenant_id == Conversation.tenant_id),
            )
            .where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
            )
        )
    ).one_or_none()

    if row is None:
        return ServingState(missing=True)

    (
        workspace_status,
        deleted_at,
        mode,
        conversation_status,
        agent_status,
        account_status,
        released_at,
    ) = row

    return ServingState(
        missing=False,
        workspace_active=workspace_status is TenantStatus.ACTIVE,
        # Retention decides when a deleted workspace's data is erased. It does
        # not mean the workspace is served in the meantime.
        workspace_deleted=deleted_at is not None,
        human=mode is ConversationMode.HUMAN,
        conversation_closed=conversation_status is ConversationStatus.CLOSED,
        agent_active=agent_status is AgentStatus.ACTIVE,
        channel_available=(account_status is WhatsAppAccountStatus.ACTIVE and released_at is None),
    )


def outcome_for(state: ServingState) -> TurnOutcome | None:
    """Why an agent may not act, as a turn ending, or None if it may.

    Checked in order of consequence, so the answer names the widest reason: a
    suspended workspace is reported as that rather than as whatever else happens
    to be true of one of its conversations.
    """
    if state.missing:
        # Deleted underneath the turn. Closed, in the only sense that matters to
        # whether a message may be sent into it.
        return TurnOutcome.SUPPRESSED_CLOSED
    if not state.workspace_active or state.workspace_deleted:
        return TurnOutcome.SUPPRESSED_WORKSPACE
    if state.human:
        return TurnOutcome.SUPPRESSED_HUMAN
    # A colleague closed it deliberately. An old turn must not reopen it by
    # sending into it; the customer writing again is what reopens a conversation.
    if state.conversation_closed:
        return TurnOutcome.SUPPRESSED_CLOSED
    if not state.agent_active:
        return TurnOutcome.SUPPRESSED_AGENT
    if not state.channel_available:
        return TurnOutcome.SUPPRESSED_CHANNEL
    return None


def tool_refusal_for(state: ServingState) -> ToolExecutionReason | None:
    """Why a tool may not run right now, or None if it may.

    The same order of consequence as `outcome_for`, in the finer vocabulary the
    execution record keeps. Suspension and deletion are told apart here because
    an operator counting refused tool calls wants to know which of the two: they
    are different incidents with different remedies.
    """
    if state.missing:
        return ToolExecutionReason.CONVERSATION_CLOSED
    if state.workspace_deleted:
        return ToolExecutionReason.WORKSPACE_DELETED
    if not state.workspace_active:
        return ToolExecutionReason.WORKSPACE_SUSPENDED
    if state.human:
        return ToolExecutionReason.CONVERSATION_HUMAN
    if state.conversation_closed:
        return ToolExecutionReason.CONVERSATION_CLOSED
    if not state.agent_active:
        return ToolExecutionReason.AGENT_DISABLED
    if not state.channel_available:
        return ToolExecutionReason.CHANNEL_UNAVAILABLE
    return None


async def refusal_now(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    agent_id: uuid.UUID | None,
) -> TurnOutcome | None:
    """Why an agent may not act on this conversation right now, or None if it may."""
    return outcome_for(
        await serving_state(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
        )
    )


__all__ = [
    "ServingState",
    "outcome_for",
    "refusal_now",
    "serving_state",
    "tool_refusal_for",
]
