"""Agent-facing conversation operations.

The read side of the inbox plus the state a human controls: who owns a
conversation, whether the AI answers it, and whether it is still open.

**Every ownership change is a transition judged against the row as committed
at the write** (CRM-02/03/04). Each one locks the conversation, re-reads it,
and only then decides whether it is still in the state it is moving *from*.
Two colleagues taking over at once, an agent's handoff landing after a
colleague's, a stale browser unassigning somebody who was reassigned a moment
ago - each of those used to be a last-writer-wins overwrite, and now the second
writer is judged against what the first committed. Only the winner of a real
transition writes the analytics event, the audit entry and the follow-up
cancellation; a loser and a no-op write nothing (CRM-05).

The rules those transitions implement are product decisions, recorded in
`docs/CRM.md`:

- a colleague taking a conversation over from the AI becomes its owner
  (PD-CRM-3); an automatic handoff never invents one;
- taking over a conversation somebody already owns changes nothing - not the
  owner, not the reason (PD-CRM-3, PD-CRM-5);
- the handoff reason is written by the transition that made the conversation
  human and by nothing else, and releasing it to the AI clears it (PD-CRM-5);
- an assignment names the owner it expects to replace, and a stale one is a
  409 (PD-CRM-4); any active member may make it (PD-CRM-10).

No lock here is held across anything but database work: every provider call in
the codebase goes through `released`, which commits first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.core.pagination import Cursor, Page, paginate
from app.db.models.analytics import AnalyticsSource
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
)
from app.db.models.sentiment import ConversationPriority
from app.db.models.user import User
from app.repositories.conversation_repository import ConversationRepository, MessageRepository
from app.repositories.membership_repository import MembershipRepository
from app.services.analytics_service import AnalyticsRecorder
from app.services.audit_service import AuditTrail
from app.services.follow_up_service import FollowUpService

logger = get_logger(__name__)

# `conversations.handoff_reason` is String(200).
MAX_HANDOFF_REASON_LENGTH: Final = 200

#: The `error_code` of an assignment whose expected owner is no longer the
#: owner. A client refreshes the conversation and asks again (PD-CRM-4).
STALE_ASSIGNMENT: Final = "stale_assignment"

# Who decided an automatic handoff, as the audit entry names them. Sentiment is
# the platform deciding on its own, so it is `SYSTEM` there and `sentiment` in
# `meta["source"]`, which is where the two are told apart.
_ACTOR_KIND: Final[dict[AnalyticsSource, AuditActorKind]] = {
    AnalyticsSource.USER: AuditActorKind.USER,
    AnalyticsSource.AGENT: AuditActorKind.AGENT,
    AnalyticsSource.SENTIMENT: AuditActorKind.SYSTEM,
    AnalyticsSource.SYSTEM: AuditActorKind.SYSTEM,
}


@dataclass(frozen=True, slots=True)
class ModeTransition:
    """What asking for a mode did.

    `changed` is False when the conversation was already in that mode when the
    request reached the row - a no-op, or a race the caller lost. Either way
    the caller did not make the transition, and nothing was recorded for it.
    """

    conversation: Conversation
    changed: bool


class InboxService:
    """Conversation operations for one workspace."""

    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._memberships = MembershipRepository(session, tenant_id=tenant_id)
        self._analytics = AnalyticsRecorder(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)
        # Constructed without settings, like the ingestion path's copy: nothing
        # here sends, and only `dispatch` needs somewhere to send from.
        self._follow_ups = FollowUpService(session=session, tenant_id=tenant_id)

    async def list_conversations(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        priority: ConversationPriority | None = None,
    ) -> Page[Conversation]:
        """Everything not closed, most recently active first.

        `priority` narrows the list to conversations at one level, which is how
        somebody works the flagged queue without the default view changing under
        everybody else.
        """
        after = Cursor.decode(cursor) if cursor else None
        rows = await self._conversations.list_open(limit=limit, after=after, priority=priority)
        return paginate(
            rows,
            limit=limit,
            key=lambda row: Cursor(sort_value=row.last_message_at, id=row.id),
        )

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation:
        return await self._conversations.require_by_id(conversation_id)

    async def list_messages(
        self,
        *,
        conversation_id: uuid.UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Message]:
        # Resolved first so another workspace's id answers not-found instead of
        # an empty list, which would leak that the conversation exists.
        await self._conversations.require_by_id(conversation_id)
        after = Cursor.decode(cursor) if cursor else None
        rows = await self._messages.list_for_conversation(
            conversation_id=conversation_id,
            limit=limit,
            after=after,
        )
        return paginate(
            rows,
            limit=limit,
            key=lambda row: Cursor(sort_value=row.created_at, id=row.id),
        )

    # ------------------------------------------------------------------ mode

    async def set_mode(
        self,
        *,
        conversation_id: uuid.UUID,
        mode: ConversationMode,
        actor: User,
        handoff_reason: str | None = None,
    ) -> Conversation:
        """A colleague taking a conversation over, or giving it back to the AI.

        The route's entry point. Automation never comes through here: it asks
        `hand_off`, which cannot assign anybody.
        """
        if mode is ConversationMode.HUMAN:
            transition = await self.take_over(
                conversation_id=conversation_id,
                actor=actor,
                reason=handoff_reason,
            )
        else:
            transition = await self.release_to_ai(conversation_id=conversation_id, actor=actor)
        return transition.conversation

    async def take_over(
        self,
        *,
        conversation_id: uuid.UUID,
        actor: User,
        reason: str | None = None,
    ) -> ModeTransition:
        """A colleague takes an AI-handled conversation, and becomes its owner.

        One transition (PD-CRM-3): the mode, the reason and the assignment move
        together, so there is no instant at which a person has stopped the AI
        and nobody owns the customer.

        On a conversation already human this changes nothing - not the reason,
        which belongs to whoever made it human, and not the owner. Moving a
        human-owned conversation between colleagues is `assign`, which says
        whom it expects to replace; a second "take over" silently stealing it
        is exactly what that precondition exists to prevent.
        """
        # First, and share-locked: the taker becomes the owner, so the same
        # rule applies as to any assignment - it cannot land on somebody whose
        # removal is committing at this moment (CRM-11).
        await self._memberships.hold_active_for_user(actor.id)
        return await self._to_human(
            conversation_id,
            reason=reason,
            source=AnalyticsSource.USER,
            actor=actor,
            assign_to=actor.id,
        )

    async def hand_off(
        self,
        *,
        conversation_id: uuid.UUID,
        reason: str,
        source: AnalyticsSource,
    ) -> ModeTransition:
        """Automation handing an AI conversation to the people - agent, sentiment, system.

        Succeeds only if the conversation is still the AI's when the write
        lands (CRM-02). The agent's handoff tool, a sentiment escalation, an
        empty model response and an exhausted allowance each decide from a read
        taken earlier - some of them a whole provider call earlier - and a
        colleague may have taken the conversation over in between. The
        colleague's takeover stands: its reason, its owner, its single
        analytics event. The caller learns it lost from `changed`.

        Never assigns anybody (PD-CRM-3). An automatic handoff has no taker,
        and inventing one would put a customer in the queue of a person who
        was never asked. An owner the conversation already has is kept.
        """
        return await self._to_human(
            conversation_id,
            reason=reason,
            source=source,
            actor=None,
            assign_to=None,
        )

    async def _to_human(
        self,
        conversation_id: uuid.UUID,
        *,
        reason: str | None,
        source: AnalyticsSource,
        actor: User | None,
        assign_to: uuid.UUID | None,
    ) -> ModeTransition:
        conversation = await self._conversations.lock_by_id(conversation_id)
        if conversation.mode is ConversationMode.HUMAN:
            # Already a person's. Whoever made it so wrote the reason and the
            # event; this request - a second takeover, a stale automated
            # decision - writes neither (CRM-03, PD-CRM-5).
            logger.info(
                "conversation.handoff_superseded",
                extra={
                    "event": "conversation.handoff_superseded",
                    "conversation_id": str(conversation_id),
                    "source": source.value,
                },
            )
            return ModeTransition(conversation, changed=False)

        previous_assignee = conversation.assigned_to_id
        conversation.mode = ConversationMode.HUMAN
        conversation.handoff_reason = _bounded_reason(reason)
        if assign_to is not None:
            conversation.assigned_to_id = assign_to

        # Taking a conversation over stops the AI, and an agent's nudge is an
        # AI-originated message, so the agent's pending nudges are cancelled
        # here rather than left for the worker to refuse (MSG-05). A
        # colleague's own reminder is theirs and survives the takeover: it is
        # the ordinary way a person uses follow-ups on a conversation they own
        # (PD-CRM-1).
        #
        # The first of two guards. `FollowUpService.dispatch` checks the mode
        # again before it sends an agent's nudge, because a handover landing
        # after the sweep has claimed the row cannot be cancelled by this one.
        cancelled = await self._follow_ups.cancel_agent_follow_ups_for_conversation(
            conversation_id=conversation_id,
            reason="A colleague took the conversation over.",
        )
        if cancelled:
            logger.info(
                "follow_up.cancelled_on_handoff",
                extra={
                    "event": "follow_up.cancelled_on_handoff",
                    "conversation_id": str(conversation_id),
                    "cancelled": cancelled,
                },
            )

        # One event and one audit entry per logical handoff, written by the
        # transition that happened and by nothing that lost to it.
        self._analytics.handoff(
            conversation_id=conversation_id,
            source=source,
            reason=conversation.handoff_reason,
            actor_id=actor.id if actor is not None else None,
        )
        self._record(
            AuditAction.CONVERSATION_TAKEN_OVER,
            conversation,
            actor=actor,
            source=source,
            previous_mode=ConversationMode.AI,
            previous_assignee=previous_assignee,
            extra={"reason_supplied": conversation.handoff_reason is not None},
        )
        logger.info(
            "conversation.mode_changed",
            extra={
                "conversation_id": str(conversation_id),
                "mode": ConversationMode.HUMAN.value,
                "source": source.value,
            },
        )
        return ModeTransition(conversation, changed=True)

    async def release_to_ai(
        self,
        *,
        conversation_id: uuid.UUID,
        actor: User,
    ) -> ModeTransition:
        """Give a human-owned conversation back to the AI.

        Clears the handoff reason: it explained a handoff that is over, and
        leaving it would describe the next one wrongly (PD-CRM-5). The owner is
        kept - who looks after a customer is a separate fact from who answers
        them.

        Nothing received while a person owned the conversation is answered
        now (PD-CRM-6). The AI answers the next thing the customer says; a
        reply to a backlog a colleague may already have dealt with would be the
        machine talking over them after the fact.
        """
        conversation = await self._conversations.lock_by_id(conversation_id)
        if conversation.mode is ConversationMode.AI:
            return ModeTransition(conversation, changed=False)

        conversation.mode = ConversationMode.AI
        conversation.handoff_reason = None
        self._analytics.handoff_resumed(conversation_id=conversation_id, actor_id=actor.id)
        self._record(
            AuditAction.CONVERSATION_RELEASED_TO_AI,
            conversation,
            actor=actor,
            source=AnalyticsSource.USER,
            previous_mode=ConversationMode.HUMAN,
            previous_assignee=conversation.assigned_to_id,
        )
        logger.info(
            "conversation.mode_changed",
            extra={"conversation_id": str(conversation_id), "mode": ConversationMode.AI.value},
        )
        return ModeTransition(conversation, changed=True)

    # ------------------------------------------------------------ assignment

    async def assign(
        self,
        *,
        conversation_id: uuid.UUID,
        assigned_to_id: uuid.UUID | None,
        expected_assigned_to_id: uuid.UUID | None,
        actor: User,
    ) -> Conversation:
        """Assign to a member of this workspace, or clear the assignment.

        `expected_assigned_to_id` is who the caller believes owns it now, and
        the write happens only if that is still true when it lands (PD-CRM-4).
        Without it two colleagues self-assigning were both told they owned the
        customer, and a stale "unassign me" erased a manager's newer
        reassignment (CRM-04). A mismatch is a 409 that changes nothing; the
        caller reads the conversation again and decides with the truth.

        Membership is verified, and share-locked, rather than assumed: the id
        arrives in a request body, and it must not be somebody outside the
        workspace or somebody whose removal is committing right now (CRM-11).
        """
        if assigned_to_id is not None:
            await self._memberships.hold_active_for_user(assigned_to_id)
        conversation = await self._conversations.lock_by_id(conversation_id)

        current = conversation.assigned_to_id
        if current != expected_assigned_to_id:
            logger.info(
                "conversation.assignment_stale",
                extra={
                    "event": "conversation.assignment_stale",
                    "conversation_id": str(conversation_id),
                },
            )
            raise ConflictError(
                "This conversation's owner changed since you looked. Refresh and try again.",
                error_code=STALE_ASSIGNMENT,
            )
        if assigned_to_id == current:
            return conversation

        conversation.assigned_to_id = assigned_to_id
        if assigned_to_id is None:
            action = AuditAction.CONVERSATION_UNASSIGNED
        elif current is None:
            action = AuditAction.CONVERSATION_ASSIGNED
        else:
            action = AuditAction.CONVERSATION_REASSIGNED
        self._record(
            action,
            conversation,
            actor=actor,
            source=AnalyticsSource.USER,
            previous_mode=conversation.mode,
            previous_assignee=current,
        )
        return conversation

    async def release_member(self, *, user_id: uuid.UUID, actor: User | None) -> int:
        """Unassign everything a departing member owns here. Returns how many.

        Called inside the removal's own transaction, after the membership row
        has been revoked and so is locked against concurrent assignments
        (PD-CRM-2). Ownership goes to nobody rather than to whoever removed
        them: a manager removing a colleague has not volunteered to take on
        their customers, and an unowned conversation is findable where a
        silently transferred one is not.
        """
        released = await self._conversations.lock_assigned_to(user_id)
        for conversation in released:
            conversation.assigned_to_id = None
            self._record(
                AuditAction.CONVERSATION_UNASSIGNED,
                conversation,
                actor=actor,
                source=AnalyticsSource.USER if actor is not None else AnalyticsSource.SYSTEM,
                previous_mode=conversation.mode,
                previous_assignee=user_id,
                extra={"cause": "member_revoked"},
            )
        return len(released)

    # ---------------------------------------------------------------- status

    async def close(self, conversation_id: uuid.UUID, *, actor: User) -> Conversation:
        return await self._set_status(
            conversation_id,
            status=ConversationStatus.CLOSED,
            action=AuditAction.CONVERSATION_CLOSED,
            actor=actor,
        )

    async def reopen(self, conversation_id: uuid.UUID, *, actor: User) -> Conversation:
        return await self._set_status(
            conversation_id,
            status=ConversationStatus.OPEN,
            action=AuditAction.CONVERSATION_REOPENED,
            actor=actor,
        )

    async def _set_status(
        self,
        conversation_id: uuid.UUID,
        *,
        status: ConversationStatus,
        action: AuditAction,
        actor: User,
    ) -> Conversation:
        conversation = await self._conversations.lock_by_id(conversation_id)
        if conversation.status is status:
            return conversation
        previous = conversation.status
        conversation.status = status
        self._record(
            action,
            conversation,
            actor=actor,
            source=AnalyticsSource.USER,
            previous_mode=conversation.mode,
            previous_assignee=conversation.assigned_to_id,
            extra={"previous_status": previous.value, "new_status": status.value},
        )
        return conversation

    # ----------------------------------------------------------------- audit

    def _record(
        self,
        action: AuditAction,
        conversation: Conversation,
        *,
        actor: User | None,
        source: AnalyticsSource,
        previous_mode: ConversationMode,
        previous_assignee: uuid.UUID | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """One ownership entry, with the before and after of who and what.

        Identifiers and states only. Never the handoff reason's text, a message
        or anything else the customer said: this trail answers "who and when",
        and the reason itself stays on the conversation row (CRM-05).
        """
        meta: dict[str, Any] = {
            "conversation_id": str(conversation.id),
            "source": source.value,
            "previous_mode": previous_mode.value,
            "new_mode": conversation.mode.value,
            "previous_assignee_id": str(previous_assignee) if previous_assignee else None,
            "new_assignee_id": (
                str(conversation.assigned_to_id) if conversation.assigned_to_id else None
            ),
        }
        if extra:
            meta.update(extra)
        self._audit.record(
            action,
            actor=actor,
            actor_kind=_ACTOR_KIND[source] if actor is None else AuditActorKind.USER,
            target_type="conversation",
            target_id=conversation.id,
            meta=meta,
        )


def _bounded_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = reason.strip()
    return text[:MAX_HANDOFF_REASON_LENGTH] if text else None


__all__ = ["MAX_HANDOFF_REASON_LENGTH", "STALE_ASSIGNMENT", "InboxService", "ModeTransition"]
