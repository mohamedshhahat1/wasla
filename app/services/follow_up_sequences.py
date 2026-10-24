"""Runs of a workspace's automatic follow-ups (FUS-02 .. FUS-06, ADR-151).

**When a run starts.** In the transaction that stores an outbound reply - the
AI's, or a person's on an AI-owned conversation - when the workspace's
reminders cover the conversation's platform, the conversation is the AI's and
open, the customer has written since the last run started, nobody has opted
out, the lead is not won or lost, and no colleague's own follow-up waits on
it. Its anchor is that reply. Step 1 is due `anchor + steps[0]`; step n is due
when step n-1 was *sent* + `steps[n-1]`. A new reply while a run is active
restarts it from the new anchor (`sequence_restarted`).

**One waiting step.** Each step is a `follow_ups` row of origin `sequence`,
scheduled only once the one before it went, so the one-pending-per-conversation
index still holds.

**The window** (FUS-05). A step may be scheduled and sent only while
`now <= last inbound + the channel's window - FOLLOW_UP_WINDOW_MARGIN`; a step
due later is never scheduled (`window_closing`) and one found past it at
dispatch is skipped (`window_closed`). Steps are always free-form: never a
template, never a message tag.

**The stop rules** (FUS-06) end a run in the transaction that causes them -
the inbound message, the takeover or assignment, the close, the opt-out, the
lead won or lost, the settings change - and are checked again at dispatch.
A step whose send intent has committed is never cancelled: it ends `sent`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.registry import ChannelRegistry, ChannelUnavailableError, default_registry
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.telemetry import record_follow_up_step
from app.db.models.audit import AuditAction
from app.db.models.conversation import (
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
)
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.follow_up_sequence import (
    FollowUpOrigin,
    FollowUpSequenceRun,
    SequenceEndReason,
    SequenceRunState,
)
from app.db.models.lead import ActorKind, Lead, LeadStatus
from app.db.models.user import User
from app.repositories.consent_repository import ContactConsentRepository
from app.services.audit_service import AuditTrail
from app.services.follow_up_settings_service import SequenceSettings, read_settings

logger = get_logger(__name__)

#: How long before the window's end a step stops being sent (FUS-05). The
#: platform's own clock for "24 hours" is not documented; half an hour of
#: margin absorbs the difference and a sweep's lateness.
DEFAULT_WINDOW_MARGIN: Final = timedelta(minutes=30)

#: The lead statuses that end a run: the sale is won or lost (FUS-06).
CLOSED_LEAD_STATUSES: Final = frozenset({LeadStatus.WON, LeadStatus.LOST})


@dataclass(frozen=True, slots=True)
class FollowUpState:
    """A conversation's reminders as a person sees them: none, one scheduled, or ended."""

    state: Literal["none", "scheduled", "ended"]
    step_index: int | None
    steps_total: int | None
    due_at: datetime | None
    ended_reason: str | None
    ended_at: datetime | None


class SequenceService:
    """The runs of one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        channels: ChannelRegistry | None = None,
        window_margin: timedelta | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._channels = channels or default_registry()
        self._margin = (
            window_margin
            if window_margin is not None
            else timedelta(seconds=get_settings().follow_up_window_margin_seconds)
        )

    # ------------------------------------------------------------------ reads

    async def settings(self) -> SequenceSettings:
        return await read_settings(self._session, self._tenant_id)

    async def active_run(self, conversation_id: uuid.UUID) -> FollowUpSequenceRun | None:
        run: FollowUpSequenceRun | None = await self._session.scalar(
            select(FollowUpSequenceRun)
            .where(
                FollowUpSequenceRun.tenant_id == self._tenant_id,
                FollowUpSequenceRun.conversation_id == conversation_id,
                FollowUpSequenceRun.state == SequenceRunState.ACTIVE.value,
            )
            .with_for_update()
        )
        return run

    async def latest_run(self, conversation_id: uuid.UUID) -> FollowUpSequenceRun | None:
        run: FollowUpSequenceRun | None = await self._session.scalar(
            select(FollowUpSequenceRun)
            .where(
                FollowUpSequenceRun.tenant_id == self._tenant_id,
                FollowUpSequenceRun.conversation_id == conversation_id,
            )
            .order_by(FollowUpSequenceRun.anchor_at.desc(), FollowUpSequenceRun.created_at.desc())
            .limit(1)
        )
        return run

    async def state(self, conversation_id: uuid.UUID) -> FollowUpState:
        """What a person sees about this conversation's reminders (FUS-09)."""
        steps_total = len((await self.settings()).steps)
        step = await self._session.scalar(
            select(FollowUp).where(
                FollowUp.tenant_id == self._tenant_id,
                FollowUp.conversation_id == conversation_id,
                FollowUp.status == FollowUpStatus.PENDING,
            )
        )
        if step is not None:
            return FollowUpState(
                state="scheduled",
                step_index=step.step_index,
                steps_total=steps_total if step.is_sequence_step else None,
                due_at=step.scheduled_at,
                ended_reason=None,
                ended_at=None,
            )
        run = await self.latest_run(conversation_id)
        if run is not None and not run.is_active:
            return FollowUpState(
                state="ended",
                step_index=run.steps_sent or None,
                steps_total=steps_total,
                due_at=None,
                ended_reason=run.end_reason,
                ended_at=run.ended_at,
            )
        return FollowUpState(
            state="none",
            step_index=None,
            steps_total=None,
            due_at=None,
            ended_reason=None,
            ended_at=None,
        )

    async def stop_by_person(self, conversation_id: uuid.UUID, *, actor: User) -> bool:
        """A colleague pressed "stop follow-ups" (FUS-09): the run ends, audited."""
        stopped = await self.stop_conversation(conversation_id, SequenceEndReason.STOPPED_BY_PERSON)
        # Its own request, nobody else's write: flushed so the state read next
        # sees the step cancelled.
        await self._session.flush()
        if stopped:
            AuditTrail(self._session, tenant_id=self._tenant_id).record(
                AuditAction.FOLLOW_UP_STOPPED_BY_PERSON,
                actor=actor,
                target_type="conversation",
                target_id=conversation_id,
            )
        return stopped

    async def covers(self, conversation: Conversation) -> bool:
        """Whether the workspace's reminders run on this conversation's platform now (FUS-03)."""
        return (await self.settings()).covers(conversation.channel)

    def window_end(self, conversation: Conversation) -> datetime | None:
        """When a step stops being sendable: the channel's window end minus the margin.

        Read from the channel's policy, never a literal (FUS-05). A channel
        without a window of its own has no automated free-form messages here.
        """
        policy: object | None
        try:
            policy = self._channels.policy_for(conversation.channel)
        except ChannelUnavailableError:
            policy = self._channels.known_policy(conversation.channel)
        window = getattr(policy, "window", None)
        if not isinstance(window, timedelta) or conversation.last_inbound_at is None:
            return None
        return conversation.last_inbound_at + window - self._margin

    # ----------------------------------------------------------------- starting

    async def on_outbound_reply(self, conversation: Conversation, *, at: datetime) -> None:
        """An AI or person reply was stored on `conversation`: start or restart its run.

        Called in the transaction that stages the reply. Does nothing unless every
        condition of FUS-02 holds.
        """
        settings = await self.settings()
        if not settings.covers(conversation.channel):
            return
        if conversation.mode is not ConversationMode.AI:
            return
        if conversation.status is ConversationStatus.CLOSED:
            return
        if conversation.last_inbound_at is None:
            return

        active = await self.active_run(conversation.id)
        if active is None:
            previous = await self.latest_run(conversation.id)
            if previous is not None and not await self._wrote_since(
                conversation.id, previous.anchor_at
            ):
                # Nobody has written since the last run began: a business
                # message to a silent customer never restarts the chain.
                return
        if await self._colleague_follow_up_waits(conversation.id):
            # A person's own reminder is theirs; no run starts under it (FUS-03).
            return
        if await self.stopped(conversation) is not None:
            return

        if active is not None:
            await self._end(
                active, SequenceEndReason.SEQUENCE_RESTARTED, cancel="sequence_restarted"
            )
        else:
            await self._replace_agent_follow_up(conversation.id)

        run = FollowUpSequenceRun(
            tenant_id=self._tenant_id,
            conversation_id=conversation.id,
            anchor_at=at,
            state=SequenceRunState.ACTIVE.value,
            steps_sent=0,
        )
        self._session.add(run)
        await self._session.flush()
        await self.schedule_step(run, conversation, step_index=1, after=at, settings=settings)

    async def schedule_step(
        self,
        run: FollowUpSequenceRun,
        conversation: Conversation,
        *,
        step_index: int,
        after: datetime,
        settings: SequenceSettings | None = None,
    ) -> FollowUp | None:
        """Schedule step `step_index` of `run`, `after` + its delay - or end the run.

        The step's delay is read now, from the settings as they are (FUS-06):
        an edit applies to every step scheduled after it.
        """
        current = settings or await self.settings()
        if not current.covers(conversation.channel):
            await self._end(run, SequenceEndReason.SETTINGS_CHANGED)
            return None
        if step_index > len(current.steps):
            await self._end(run, SequenceEndReason.COMPLETED)
            return None
        stopped = await self.stopped(conversation)
        if stopped is not None:
            await self._end(run, stopped)
            return None
        due = after + timedelta(minutes=current.steps[step_index - 1].delay_minutes)
        window_end = self.window_end(conversation)
        if window_end is None or due > window_end:
            # Never scheduled: nothing is recorded as skipped for it (FUS-05).
            await self._end(run, SequenceEndReason.WINDOW_CLOSING)
            await record_follow_up_step("not_scheduled", "window_closing")
            return None
        step = FollowUp(
            tenant_id=self._tenant_id,
            conversation_id=conversation.id,
            scheduled_at=due,
            requested_at=due,
            status=FollowUpStatus.PENDING,
            origin=FollowUpOrigin.SEQUENCE.value,
            sequence_run_id=run.id,
            step_index=step_index,
            anchor_at=run.anchor_at,
            created_by_kind=ActorKind.SYSTEM,
            attempts=0,
        )
        self._session.add(step)
        await self._session.flush()
        logger.info(
            "follow_up.sequence_step_scheduled",
            extra={
                "event": "follow_up.sequence_step_scheduled",
                "tenant_id": str(self._tenant_id),
                "conversation_id": str(conversation.id),
                "step_index": step_index,
            },
        )
        return step

    async def after_step_sent(self, step: FollowUp, *, sent_at: datetime) -> None:
        """A step went: count it and schedule the next from the moment it was sent."""
        run = await self._run_of(step)
        if run is None or not run.is_active:
            return
        run.steps_sent += 1
        conversation = await self._conversation(step.conversation_id)
        await self.schedule_step(
            run, conversation, step_index=int(step.step_index or 0) + 1, after=sent_at
        )

    # ----------------------------------------------------------------- stopping

    async def stop_conversation(
        self, conversation_id: uuid.UUID, reason: SequenceEndReason
    ) -> bool:
        """End this conversation's active run, cancelling its waiting step. Whether one ended."""
        with self._session.no_autoflush:
            run = await self.active_run(conversation_id)
            if run is None:
                return False
            await self._end(run, reason, cancel=reason.value)
        return True

    async def stop_contact(
        self, contact_id: uuid.UUID, reason: SequenceEndReason, *, channel: str | None = None
    ) -> int:
        """End the active runs of every conversation of this contact (on `channel`)."""
        statement = select(Conversation.id).where(
            Conversation.tenant_id == self._tenant_id,
            Conversation.contact_id == contact_id,
        )
        if channel is not None:
            statement = statement.where(Conversation.channel == channel)
        ended = 0
        with self._session.no_autoflush:
            found = list((await self._session.execute(statement)).scalars())
        for conversation_id in found:
            ended += await self.stop_conversation(conversation_id, reason)
        return ended

    async def end_run(self, step: FollowUp, reason: SequenceEndReason) -> None:
        """End the run a step belongs to, from dispatch, without touching the step itself."""
        run = await self._run_of(step)
        if run is not None and run.is_active:
            await self._end(run, reason)

    async def cancel_step(self, step: FollowUp, reason: SequenceEndReason) -> None:
        """Cancel a due step found no longer wanted at dispatch, and end its run."""
        _cancel(step, reason.value)
        await self.end_run(step, reason)

    async def apply_settings(self, settings: SequenceSettings) -> int:
        """What a settings change does to waiting steps, in its transaction (FUS-06).

        Off, or the platform removed: the platform's waiting steps cancelled. A
        reminder deleted: the waiting steps whose reminder no longer exists
        cancelled. A step whose reminder still exists keeps its due time.
        """
        rows = await self._session.execute(
            select(FollowUp, Conversation.channel)
            .join(
                Conversation,
                and_(
                    Conversation.tenant_id == FollowUp.tenant_id,
                    Conversation.id == FollowUp.conversation_id,
                ),
            )
            .where(
                FollowUp.tenant_id == self._tenant_id,
                FollowUp.status == FollowUpStatus.PENDING,
                FollowUp.origin == FollowUpOrigin.SEQUENCE.value,
                FollowUp.message_id.is_(None),
            )
            .with_for_update(of=FollowUp)
        )
        cancelled = 0
        for step, channel in rows.all():
            if settings.covers(channel) and int(step.step_index or 0) <= len(settings.steps):
                continue
            run = await self._run_of(step)
            _cancel(step, "settings_changed")
            if run is not None and run.is_active:
                _close(run, SequenceEndReason.SETTINGS_CHANGED)
            cancelled += 1
        return cancelled

    async def replaced_by_person(self, conversation_id: uuid.UUID) -> None:
        """A person scheduled a one-off: the run ends (FUS-03); its step becomes theirs."""
        run = await self.active_run(conversation_id)
        if run is not None:
            _close(run, SequenceEndReason.REPLACED_BY_PERSON)

    # ------------------------------------------------------------------ helpers

    async def stopped(self, conversation: Conversation) -> SequenceEndReason | None:
        """The stop rule that holds for this conversation now, if any (FUS-06)."""
        if conversation.mode is not ConversationMode.AI:
            return SequenceEndReason.HUMAN_TOOK_OVER
        if conversation.status is ConversationStatus.CLOSED:
            return SequenceEndReason.CONVERSATION_CLOSED
        if not await ContactConsentRepository(
            self._session, tenant_id=self._tenant_id
        ).accepts_marketing(conversation.contact_id, conversation.channel):
            return SequenceEndReason.OPTED_OUT
        closed_lead = await self._session.scalar(
            select(Lead.id)
            .where(
                Lead.tenant_id == self._tenant_id,
                Lead.contact_id == conversation.contact_id,
                Lead.status.in_(CLOSED_LEAD_STATUSES),
            )
            .limit(1)
        )
        if closed_lead is not None:
            return SequenceEndReason.LEAD_CLOSED
        return None

    async def _wrote_since(self, conversation_id: uuid.UUID, moment: datetime) -> bool:
        """Whether the customer wrote after `moment`, by when Wasla stored it.

        Not `last_inbound_at`: that is the platform's timestamp, whole seconds
        on WhatsApp, so a reply in the same second as the anchor would read as
        older than it.
        """
        found = await self._session.scalar(
            select(Message.id)
            .where(
                Message.tenant_id == self._tenant_id,
                Message.conversation_id == conversation_id,
                Message.direction == MessageDirection.INBOUND,
                Message.created_at > moment,
            )
            .limit(1)
        )
        return found is not None

    async def _colleague_follow_up_waits(self, conversation_id: uuid.UUID) -> bool:
        found = await self._session.scalar(
            select(FollowUp.id)
            .where(
                FollowUp.tenant_id == self._tenant_id,
                FollowUp.conversation_id == conversation_id,
                FollowUp.status == FollowUpStatus.PENDING,
                FollowUp.created_by_kind == ActorKind.USER,
            )
            .limit(1)
        )
        return found is not None

    async def _replace_agent_follow_up(self, conversation_id: uuid.UUID) -> None:
        """An agent's one-off scheduled before sequences were on gives way to the run."""
        rows = await self._session.execute(
            select(FollowUp)
            .where(
                FollowUp.tenant_id == self._tenant_id,
                FollowUp.conversation_id == conversation_id,
                FollowUp.status == FollowUpStatus.PENDING,
                FollowUp.created_by_kind == ActorKind.AGENT,
                FollowUp.message_id.is_(None),
            )
            .with_for_update()
        )
        for follow_up in rows.scalars():
            _cancel(follow_up, "replaced_by_sequence")
        await self._session.flush()

    async def _end(
        self, run: FollowUpSequenceRun, reason: SequenceEndReason, *, cancel: str | None = None
    ) -> None:
        """End `run`; with `cancel`, its waiting step is cancelled with that reason."""
        if cancel is not None:
            rows = await self._session.execute(
                select(FollowUp)
                .where(
                    FollowUp.tenant_id == self._tenant_id,
                    FollowUp.sequence_run_id == run.id,
                    FollowUp.status == FollowUpStatus.PENDING,
                    FollowUp.message_id.is_(None),
                )
                .with_for_update()
            )
            for step in rows.scalars():
                _cancel(step, cancel)
                await record_follow_up_step("cancelled", cancel)
        # Not flushed here: a stop rule runs inside somebody else's write - a
        # lead's status, a takeover - and flushing it would expire the row the
        # caller is about to serialise. Their commit writes this too.
        _close(run, reason)
        logger.info(
            "follow_up.sequence_ended",
            extra={
                "event": "follow_up.sequence_ended",
                "tenant_id": str(self._tenant_id),
                "conversation_id": str(run.conversation_id),
                "reason": reason.value,
            },
        )

    async def _run_of(self, step: FollowUp) -> FollowUpSequenceRun | None:
        if step.sequence_run_id is None:
            return None
        run: FollowUpSequenceRun | None = await self._session.scalar(
            select(FollowUpSequenceRun)
            .where(
                FollowUpSequenceRun.tenant_id == self._tenant_id,
                FollowUpSequenceRun.id == step.sequence_run_id,
            )
            .with_for_update()
        )
        return run

    async def _conversation(self, conversation_id: uuid.UUID) -> Conversation:
        conversation = await self._session.scalar(
            select(Conversation)
            .where(Conversation.tenant_id == self._tenant_id, Conversation.id == conversation_id)
            .execution_options(populate_existing=True)
        )
        assert conversation is not None  # noqa: S101 - a step's conversation cascades with it
        return conversation


def _cancel(follow_up: FollowUp, reason: str) -> None:
    follow_up.claim_token = None
    follow_up.claimed_until = None
    follow_up.status = FollowUpStatus.CANCELLED
    follow_up.cancelled_at = datetime.now(UTC)
    follow_up.cancelled_reason = reason


def _close(run: FollowUpSequenceRun, reason: SequenceEndReason) -> None:
    if not run.is_active:
        return
    run.state = SequenceRunState.ENDED.value
    run.end_reason = reason.value
    run.ended_at = datetime.now(UTC)


__all__ = [
    "CLOSED_LEAD_STATUSES",
    "DEFAULT_WINDOW_MARGIN",
    "FollowUpState",
    "SequenceService",
]
