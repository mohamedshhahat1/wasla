"""Whether an agent may still act on a conversation, read from the database now.

Nothing on the AI path used to read a workspace's lifecycle (AI-06). Suspension
and deletion were enforced at the API authorization layer - which every human
request passes through and no worker does - so a suspended workspace, and a
deleted one for the whole of its retention window, kept running billable
inference and kept messaging its customers. And after a long inference only the
conversation's *mode* was read again (AI-07): an agent disabled, a conversation
closed or a workspace suspended while the model was composing still got its reply
sent.

One question, asked twice by the worker: before a turn is charged, so a workspace
that is not being served costs nothing and calls nobody; and immediately before a
reply leaves, because an inference is long enough for any of it to change.

**Columns, never objects.** The conversation and agent are already in the turn's
session, and a `select` returning a mapped entity hands back the instance in the
identity map with the attributes it was loaded with - which is exactly the stale
snapshot this exists to see past. Asking for column values gets the rows as they
are now. The same technique `AgentOrchestrator._taken_over` uses, for the same
reason, extended to everything a reply depends on.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent import Agent, AgentStatus
from app.db.models.agent_turn import TurnOutcome
from app.db.models.conversation import Conversation, ConversationMode, ConversationStatus
from app.db.models.enums import TenantStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus


async def refusal_now(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    agent_id: uuid.UUID | None,
) -> TurnOutcome | None:
    """Why an agent may not act on this conversation right now, or None if it may.

    Checked in order of consequence, so the answer names the widest reason: a
    suspended workspace is reported as that rather than as whatever else happens
    to be true of one of its conversations.

    Tenant-scoped on the conversation, so a conversation id from another
    workspace matches nothing and is refused rather than read.
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
        # Deleted underneath the turn. Closed, in the only sense that matters to
        # whether a message may be sent into it.
        return TurnOutcome.SUPPRESSED_CLOSED

    (
        workspace_status,
        deleted_at,
        mode,
        conversation_status,
        agent_status,
        account_status,
        released_at,
    ) = row

    # Retention decides when a deleted workspace's data is erased. It does not
    # mean the workspace is served in the meantime.
    if workspace_status is not TenantStatus.ACTIVE or deleted_at is not None:
        return TurnOutcome.SUPPRESSED_WORKSPACE
    if mode is ConversationMode.HUMAN:
        return TurnOutcome.SUPPRESSED_HUMAN
    # A colleague closed it deliberately. An old turn must not reopen it by
    # sending into it; the customer writing again is what reopens a conversation.
    if conversation_status is ConversationStatus.CLOSED:
        return TurnOutcome.SUPPRESSED_CLOSED
    if agent_status is not AgentStatus.ACTIVE:
        return TurnOutcome.SUPPRESSED_AGENT
    if account_status is not WhatsAppAccountStatus.ACTIVE or released_at is not None:
        return TurnOutcome.SUPPRESSED_CHANNEL
    return None
