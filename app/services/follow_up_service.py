"""Scheduling, cancelling and sending follow-ups.

A follow-up is a promise to say something later unless the customer speaks
first. Three rules make that safe to automate, and they are what this module is
for.

**One pending nudge per conversation.** Scheduling again while one waits
reschedules it. An agent that decides to follow up on every turn would otherwise
queue five messages at one person, and each of those is a real notification on a
real phone.

**A reply cancels it.** The nudge exists because the customer went quiet; the
moment they answer, its reason is gone. Cancellation runs on the inbound path,
before anything else has a chance to send it.

**Sending obeys WhatsApp's rules or does not happen.** Inside the 24-hour
service window, free text. Outside it, an approved template or nothing at all.
"Nothing at all" is a recorded outcome (`SKIPPED`) rather than a silent
discard, because the business needs to know the nudge it configured never went.

**A colleague taking the conversation over stops the agent's nudges, and a
customer who asked not to be marketed at stops every nudge.** Both are re-read
at dispatch rather than trusted from scheduling, because hours pass between the
two moments and both facts change in that gap. Neither was checked at all: a
follow-up fired underneath a person who had taken the conversation over -
defeating the product's own mechanism for stopping the AI - and fired at
somebody who had written STOP (MSG-05, MSG-06).

A colleague's *own* reminder is not the AI, and a human-owned conversation is
exactly where one belongs: it is accepted there, survives a takeover, and is
sent (PD-CRM-1). It used to be accepted with a 201 and then always skipped,
which made the documented way to use the feature a guaranteed no-op (CRM-08).

**A follow-up has one honest ending.** The sweep's claim is a token of its own,
so a reschedule or a cancel that lands while a row is claimed takes it back from
the sweep instead of racing it; once the send intent has committed, neither is
possible, and the caller is told so instead of being told "cancelled" about a
message on the customer's phone (CRM-09, CRM-10).

The opt-out rule needs stating, because the codebase applies it unevenly on
purpose. A campaign honours it and an AI reply does not, and both are right: a
customer refusing marketing has not refused an answer to their own question. A
follow-up sits between the two and is filed with the campaign, because it is
unsolicited and automated - nobody asked for it, and it arrives after the
conversation has gone quiet. See `docs/CAMPAIGNS.md`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.pagination import Cursor, Page, paginate
from app.db.models.conversation import (
    ConversationMode,
    ConversationStatus,
    Message,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.enums import TenantStatus
from app.db.models.follow_up import (
    MAX_ATTEMPTS,
    MAX_BODY_LENGTH,
    MAX_REASON_LENGTH,
    FollowUp,
    FollowUpStatus,
)
from app.db.models.lead import ActorKind
from app.db.models.tenant import Tenant
from app.integrations.whatsapp.client import ProviderAuthError
from app.repositories.conversation_repository import (
    ContactRepository,
    ConversationRepository,
)
from app.repositories.follow_up_repository import FollowUpRepository
from app.repositories.lead_repository import LeadRepository
from app.repositories.membership_repository import MembershipRepository
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.services.messaging_service import MessagingService
from app.services.template_service import refusal_reason_for

logger = get_logger(__name__)

# Bounds on how far ahead a follow-up may be scheduled. The lower bound stops a
# zero or negative delay turning into an immediate send that reads as a bug to
# the customer; the upper bound stops a model's stray number parking a message
# years away where nobody will ever see it waiting.
MIN_DELAY: Final = timedelta(minutes=1)
MAX_DELAY: Final = timedelta(days=30)
DEFAULT_DELAY: Final = timedelta(minutes=30)

#: The `error_code` of a cancel or reschedule that reached a follow-up whose
#: send has already been committed. The message may be on the customer's phone,
#: so neither can be honoured - and neither may be reported as done (CRM-10).
DISPATCH_IN_PROGRESS: Final = "dispatch_in_progress"

#: `cancelled_reason` for the reminders of a colleague removed from the
#: workspace (PD-CRM-8).
MEMBER_REVOKED_REASON: Final = "member_revoked"


@dataclass(frozen=True, slots=True)
class _Intention:
    """What a caller wants the conversation's one pending nudge to say.

    Extracted so that creating a row and rescheduling the row somebody else
    created apply the same fields. The race path does both, and two hand-written
    copies of "what a follow-up is" would drift.
    """

    scheduled_at: datetime
    body: str | None
    template_name: str | None
    template_language: str | None
    template_components: list[dict[str, Any]] | None
    reason: str | None
    created_by_id: uuid.UUID | None
    created_by_kind: ActorKind


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What happened when a due follow-up was dealt with."""

    follow_up: FollowUp
    status: FollowUpStatus
    detail: str | None = None


class FollowUpService:
    """Follow-up operations for one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        settings: Settings | None = None,
        messaging: MessagingService | None = None,
    ) -> None:
        """`settings` is needed only to send; `messaging` overrides how.

        Scheduling and cancelling touch no external service, so the callers that
        only do those - the webhook's inbound path, the agent tool - construct
        this without either. Injecting `messaging` lets a test drive the
        compliance branches without a WhatsApp account, which is the whole
        reason it is a parameter rather than something built inline.
        """
        self._session = session
        self._tenant_id = tenant_id
        self._settings = settings
        self._messaging = messaging
        self._follow_ups = FollowUpRepository(session, tenant_id=tenant_id)
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._contacts = ContactRepository(session, tenant_id=tenant_id)
        self._templates = WhatsAppTemplateRepository(session, tenant_id=tenant_id)
        self._leads = LeadRepository(session, tenant_id=tenant_id)
        self._memberships = MembershipRepository(session, tenant_id=tenant_id)

    # ------------------------------------------------------------------ reads

    async def get(self, follow_up_id: uuid.UUID) -> FollowUp:
        return await self._follow_ups.require_by_id(follow_up_id)

    async def list_follow_ups(
        self,
        *,
        statuses: tuple[FollowUpStatus, ...] = (),
        conversation_id: uuid.UUID | None = None,
        lead_id: uuid.UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[FollowUp]:
        after = Cursor.decode(cursor) if cursor else None
        rows = await self._follow_ups.list_follow_ups(
            statuses=statuses,
            conversation_id=conversation_id,
            lead_id=lead_id,
            limit=limit,
            after=after,
        )
        return paginate(
            rows,
            limit=limit,
            key=lambda row: Cursor(sort_value=row.scheduled_at, id=row.id),
        )

    # -------------------------------------------------------------- scheduling

    async def schedule(
        self,
        *,
        conversation_id: uuid.UUID,
        delay: timedelta | None = None,
        scheduled_at: datetime | None = None,
        body: str | None = None,
        template_name: str | None = None,
        template_language: str | None = None,
        template_components: list[dict[str, Any]] | None = None,
        reason: str | None = None,
        lead_id: uuid.UUID | None = None,
        created_by_id: uuid.UUID | None = None,
        created_by_kind: ActorKind = ActorKind.USER,
    ) -> FollowUp:
        """Schedule a nudge, or reschedule the one already waiting.

        Rescheduling rather than refusing is the useful behaviour: the second
        call carries newer information than the first, and a customer who said
        "next week" after saying "tomorrow" should be followed up next week.
        """
        conversation = await self._conversations.require_by_id(
            conversation_id,
            # Read as the database holds it now, not as the turn loaded it
            # before the inference (TOOL-21). The takeover this guards against
            # is one that happened while the model was composing.
            populate_existing=True,
        )
        if conversation.status is ConversationStatus.CLOSED:
            raise ValidationError("This conversation is closed.")

        if conversation.mode is ConversationMode.HUMAN and created_by_kind is ActorKind.AGENT:
            # **Handing a conversation to a person stops the AI** (TOOL-06,
            # PD-TOOLS-01). This had no guard at all: an agent could plant a
            # nudge on a conversation a colleague had just taken over - and
            # could do it in the same model response as the handoff that was
            # supposed to cancel the nudges, moments after `set_mode` had
            # cancelled the ones that existed. `dispatch` would skip it while
            # the conversation stayed human, but the row waited, and handing the
            # conversation back to the AI released it.
            #
            # Only an agent is refused. A colleague scheduling a follow-up on a
            # conversation they own is the ordinary way to use the feature, and
            # it is sent (PD-CRM-1).
            raise ConflictError("This conversation is handled by a colleague.")

        if lead_id is not None:
            await self._require_lead_of(lead_id, contact_id=conversation.contact_id)

        when = self._resolve_time(delay=delay, scheduled_at=scheduled_at)
        text = _validated_body(body)
        name, language = _validated_template(template_name, template_language)

        if text is None and name is None:
            raise ValidationError(
                "A follow-up needs a message to send, an approved template, or both."
            )

        if name is not None:
            refusal = await self._template_refusal(name, str(language))
            if refusal is not None:
                # Refused now rather than at the due moment. Scheduling is where
                # a person is present to fix it; the send happens hours later
                # against nobody.
                raise ValidationError(refusal)

        intention = _Intention(
            scheduled_at=when,
            body=text,
            template_name=name,
            template_language=language,
            template_components=template_components,
            reason=_trimmed(reason),
            created_by_id=created_by_id,
            created_by_kind=created_by_kind,
        )

        existing = await self._follow_ups.get_pending_for_conversation(
            conversation_id,
            for_update=True,
        )
        if existing is not None:
            return self._reschedule(existing, intention)

        created = await self._create_pending(
            conversation_id=conversation_id,
            intention=intention,
            lead_id=lead_id,
        )
        if created is not None:
            return created

        # Another turn of this conversation scheduled one between the read above
        # and the insert (TOOL-02). Its row is the pending follow-up now, and
        # this call carries the newer intention, so the contract is the same one
        # rescheduling has always had: the later decision wins.
        winner = await self._follow_ups.get_pending_for_conversation(
            conversation_id,
            for_update=True,
        )
        if winner is None:  # pragma: no cover - the index says this cannot happen
            raise ConflictError("A follow-up for this conversation could not be resolved.")
        return self._reschedule(winner, intention)

    async def _require_lead_of(self, lead_id: uuid.UUID, *, contact_id: uuid.UUID) -> None:
        """The lead a follow-up names must be this workspace's, and this customer's.

        Resolved through the workspace's own repository (CRM-01). The id came
        from a request body and was stored verbatim: another workspace's lead
        was accepted with a 201, and an id naming nothing surfaced as a 409 -
        a cross-tenant reference plus an oracle for which lead ids exist. Both
        are now the same not-found, so the answer says nothing about any
        workspace but the caller's own.

        A lead of a *different customer in this workspace* is refused too
        (CRM-14). It is not a leak, so it says what is wrong - but a nudge to
        one customer filed against another's opportunity is a record that
        misleads everybody who reads the pipeline.
        """
        lead = await self._leads.get_by_id(lead_id)
        if lead is None:
            raise NotFoundError()
        if lead.contact_id != contact_id:
            raise ValidationError("That lead belongs to a different customer.")

    def _reschedule(self, follow_up: FollowUp, intention: _Intention) -> FollowUp:
        """Point the one pending nudge at what the caller wants now.

        The row arrives locked. If its send has already been committed it
        cannot be moved - the customer may have it - and the caller is told so
        rather than being told it moved (CRM-09). Otherwise the sweep's claim is
        cleared with the change, so a worker holding it finds nothing to send
        and the new time and text are what go out, at the new time.
        """
        if follow_up.is_in_flight:
            raise ConflictError(
                "This follow-up is being sent and can no longer be changed.",
                error_code=DISPATCH_IN_PROGRESS,
            )
        _release_claim(follow_up)
        follow_up.scheduled_at = intention.scheduled_at
        follow_up.body = intention.body
        follow_up.template_name = intention.template_name
        follow_up.template_language = intention.template_language
        follow_up.template_components = intention.template_components
        follow_up.reason = intention.reason
        follow_up.created_by_id = intention.created_by_id
        follow_up.created_by_kind = intention.created_by_kind
        # Reset, because this is a fresh intention rather than a retry of
        # the old one.
        follow_up.attempts = 0
        follow_up.last_error = None
        logger.info(
            "follow_up.rescheduled",
            extra={
                "follow_up_id": str(follow_up.id),
                "conversation_id": str(follow_up.conversation_id),
            },
        )
        return follow_up

    async def _create_pending(
        self,
        *,
        conversation_id: uuid.UUID,
        intention: _Intention,
        lead_id: uuid.UUID | None,
    ) -> FollowUp | None:
        """Open the conversation's pending nudge, or None because somebody else did.

        **Inside a savepoint** (TOOL-02), for the same reason the lead tool's
        create is. Two turns of one conversation are ordinary - a customer
        writing twice in a few seconds produces two, deliberately not coalesced
        (AI-08) - and `uq_follow_ups_pending_conversation` lets exactly one
        insert win. The loser's `IntegrityError` used to escape the tool and
        abort the turn's whole transaction, so the second customer message was
        answered by silence. Rolled back to a savepoint, the session survives and
        the caller reschedules the winner's row.
        """
        try:
            async with self._session.begin_nested():
                follow_up = self._follow_ups.create(
                    conversation_id=conversation_id,
                    scheduled_at=intention.scheduled_at,
                    body=intention.body,
                    template_name=intention.template_name,
                    template_language=intention.template_language,
                    template_components=intention.template_components,
                    reason=intention.reason,
                    lead_id=lead_id,
                    created_by_id=intention.created_by_id,
                    created_by_kind=intention.created_by_kind,
                )
                # Flushed so the caller can read the generated id and the
                # timestamps. Without it a route serialising this row answers
                # 500: the primary key default and the server defaults are
                # applied at flush, and the request commits after the response
                # has already been built. It is also what makes the unique
                # index speak here rather than at the round's commit.
                await self._session.flush()
        except IntegrityError:
            logger.info(
                "follow_up.create_raced",
                extra={
                    "event": "follow_up.create_raced",
                    "conversation_id": str(conversation_id),
                },
            )
            return None

        logger.info(
            "follow_up.scheduled",
            extra={
                "conversation_id": str(conversation_id),
                "scheduled_at": intention.scheduled_at.isoformat(),
            },
        )
        return follow_up

    def _resolve_time(
        self,
        *,
        delay: timedelta | None,
        scheduled_at: datetime | None,
    ) -> datetime:
        """Turn a delay or an absolute time into a bounded absolute time.

        An absolute time must say which clock it is on (PD-CRM-9). A naive one
        used to be read as UTC, so a colleague in Cairo typing "09:00" had the
        nudge arrive three hours late in summer and two in winter, with
        nothing to say why (CRM-15). Refused rather than guessed: the only
        correct zone to assume is one this system does not know.
        """
        now = datetime.now(UTC)
        if scheduled_at is not None:
            if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
                raise ValidationError(
                    "A scheduled time must include its UTC offset, such as +02:00 or Z."
                )
            when = scheduled_at.astimezone(UTC)
        else:
            when = now + (delay if delay is not None else DEFAULT_DELAY)

        if when - now < MIN_DELAY:
            raise ValidationError(
                f"A follow-up must be at least {int(MIN_DELAY.total_seconds() // 60)} "
                "minute away."
            )
        if when - now > MAX_DELAY:
            raise ValidationError(f"A follow-up cannot be more than {MAX_DELAY.days} days away.")
        return when

    async def cancel(
        self,
        *,
        follow_up_id: uuid.UUID,
        reason: str | None = None,
    ) -> FollowUp:
        """Cancel one follow-up by id, if it can still be stopped.

        A conditional transition on the row as committed (CRM-10). The cancel
        used to write `CANCELLED` over whatever it had read, so a cancel racing
        the sweep answered "cancelled" while the customer received the message,
        and the row ended `CANCELLED` naming a sent message.

        - Pending and not yet sent: cancelled, and the sweep's claim with it,
          so a worker holding the claim finds nothing to send.
        - Its send intent already committed: a 409, because the customer may
          have it and "cancelled" would be false. The dispatch ends it `SENT`
          or `FAILED`, and that is what the caller sees next.
        - Already finished: returned untouched. The status in the response is
          the truth - `sent` says the nudge went - and there is nothing for the
          caller to retry.
        """
        follow_up = await self._follow_ups.lock_by_id(follow_up_id)
        if not follow_up.is_pending:
            return follow_up
        if follow_up.is_in_flight:
            raise ConflictError(
                "This follow-up is already being sent and can no longer be cancelled.",
                error_code=DISPATCH_IN_PROGRESS,
            )
        return self._cancel(follow_up, reason=reason)

    async def cancel_for_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        reason: str = "The customer replied.",
    ) -> int:
        """Cancel whatever is waiting on this conversation. Returns how many.

        Called from the inbound path. The nudge existed because the customer had
        gone quiet, so their reply removes its reason — sending it anyway would
        read as a system talking over someone who is already talking.
        """
        pending = [
            follow_up
            for follow_up in await self._follow_ups.list_pending_for_conversation(conversation_id)
            if not follow_up.is_in_flight
        ]
        for follow_up in pending:
            self._cancel(follow_up, reason=reason)
        if pending:
            logger.info(
                "follow_up.cancelled_on_reply",
                extra={"conversation_id": str(conversation_id), "cancelled": len(pending)},
            )
        return len(pending)

    async def cancel_agent_follow_ups_for_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        reason: str,
    ) -> int:
        """Cancel the agent's waiting nudges on one conversation. Returns how many.

        Called by a takeover. Only an agent's: the AI stops when a person takes
        over, and a colleague's own reminder is a person's, which the takeover
        must not discard (PD-CRM-1). A nudge whose send has already committed
        is left to finish - it cannot be recalled, and marking it cancelled
        would be false.
        """
        pending = [
            follow_up
            for follow_up in await self._follow_ups.list_pending_for_conversation(conversation_id)
            if follow_up.created_by_kind is ActorKind.AGENT and not follow_up.is_in_flight
        ]
        for follow_up in pending:
            self._cancel(follow_up, reason=reason)
        return len(pending)

    async def cancel_member_follow_ups(self, *, user_id: uuid.UUID) -> int:
        """Cancel the waiting reminders of a colleague who is leaving. Returns how many.

        Inside the removal's transaction (PD-CRM-8). A reminder is a person's
        intention to say something to a customer, and once that person has
        left the workspace nobody intends it any more - sending it would be a
        message from somebody who is gone. What already went out is history
        and is untouched, as is a send already committed, which cannot be
        recalled; dispatch re-checks the creator for anything that slips past.
        """
        pending = [
            follow_up
            for follow_up in await self._follow_ups.lock_pending_created_by(user_id)
            if not follow_up.is_in_flight
        ]
        for follow_up in pending:
            self._cancel(follow_up, reason=MEMBER_REVOKED_REASON)
        return len(pending)

    async def _template_refusal(self, name: str, language: str) -> str | None:
        """Whether the registry knows a reason this template must not be sent.

        Silent when the registry has never heard of the template. A workspace
        that has not synced yet would otherwise lose every template-bearing
        follow-up it has, and "unknown" cannot be told apart from "never
        synced". See `refusal_reason_for`.
        """
        return refusal_reason_for(await self._templates.find_anywhere(name=name, language=language))

    def _cancel(self, follow_up: FollowUp, *, reason: str | None) -> FollowUp:
        _release_claim(follow_up)
        follow_up.status = FollowUpStatus.CANCELLED
        follow_up.cancelled_at = datetime.now(UTC)
        follow_up.cancelled_reason = _trimmed(reason)
        return follow_up

    # ----------------------------------------------------------------- sending

    async def dispatch(self, follow_up: FollowUp) -> DispatchOutcome:
        """Send one due follow-up, or record why it was not sent.

        This is the compliance boundary. Inside the service window free text is
        allowed; outside it Meta accepts approved templates only, so a follow-up
        with no template is `SKIPPED` rather than attempted. Skipping is not a
        failure and is never retried: the window will not reopen on its own, and
        retrying would be a queue of messages that can never legally send.
        """
        if not follow_up.is_pending:
            # Something else finished it between the claim and now.
            return DispatchOutcome(follow_up, follow_up.status, "Already resolved.")

        # The claim this dispatch runs under. The outcome after the send is
        # written only while the row still carries it (CRM-09).
        claim = follow_up.claim_token

        if follow_up.message_id is not None:
            # **A pending follow-up naming a message is one whose send did not
            # resolve.** `_fail` clears the link when Meta refused - nothing was
            # delivered, so the next attempt makes a new message - and success
            # is not pending. So reaching here means a previous attempt
            # committed a send intent and its worker then stopped, and whether
            # Meta took that message is not knowable: there is no idempotency
            # key on the send endpoint and no lookup to ask with. Terminal
            # rather than retried, because the alternative is somebody
            # receiving the same nudge twice because a process died at the
            # wrong instant (ADR-093).
            return self._abandon(
                follow_up,
                "An earlier attempt could not be confirmed, so this was not sent again.",
            )

        messaging = self._messaging
        if messaging is None:
            if self._settings is None:
                raise RuntimeError("FollowUpService needs settings or a messaging service to send.")
            messaging = MessagingService(
                session=self._session,
                settings=self._settings,
                tenant_id=self._tenant_id,
            )

        if not await self._workspace_is_served():
            # **No automated message leaves a workspace that is not being
            # served** (TOOL-04, PD-TOOLS-06). Every other pre-send fact here is
            # re-read because hours pass between scheduling and sending; the
            # workspace's own lifecycle was the one that was not, so a
            # suspended or deleted workspace went on delivering nudges a model
            # had composed - the one place a tool's effect was both
            # customer-visible and detached from every check the AI path makes.
            #
            # Terminal rather than postponed, and that is deliberate
            # (PD-TOOLS-06): a nudge suppressed during a suspension must not
            # arrive weeks later as a surprise when the workspace is restored.
            # The customer has moved on and the message is about a conversation
            # they no longer remember.
            #
            # The second of two guards. Lifecycle transitions cancel the pending
            # agent nudges they can see; this one is authoritative, because it
            # runs immediately before the send and cannot be missed by a
            # transition that happened while the sweep already held the row.
            return self._skip(
                follow_up,
                "The workspace was not being served when the follow-up came due.",
            )

        # Share-locked until the send intent commits, so a takeover cannot slip
        # between the mode read below and the decision to send: it waits for
        # the intent, and a takeover that committed first is what this reads.
        conversation = await self._conversations.lock_by_id(
            follow_up.conversation_id,
            share=True,
        )
        if conversation.status is ConversationStatus.CLOSED:
            return self._skip(
                follow_up, "The conversation was closed before the follow-up was due."
            )

        if conversation.mode is ConversationMode.HUMAN and follow_up.created_by_kind is (
            ActorKind.AGENT
        ):
            # A colleague owns this conversation, and this nudge is the
            # agent's. Handing it over is the documented way to stop the AI,
            # and an AI nudge arriving underneath somebody who is
            # mid-conversation with the customer makes that promise false in
            # the most visible way available (MSG-05).
            #
            # The second of two guards. A takeover cancels the agent's pending
            # follow-ups at the moment of handover, which handles the ordinary
            # case; this one handles the race, because the mode can change
            # between the sweep claiming this row and the send leaving.
            #
            # A colleague's own reminder is not refused here: it is a person's
            # message on a conversation a person owns (PD-CRM-1).
            return self._skip(
                follow_up,
                "A colleague has taken this conversation over.",
            )

        if follow_up.created_by_kind is ActorKind.USER and not await self._creator_is_member(
            follow_up
        ):
            # The colleague who asked for this reminder has left. Their removal
            # cancels what it can see; this is the one that a retry or a claim
            # carried past it (PD-CRM-8).
            _release_claim(follow_up)
            follow_up.status = FollowUpStatus.CANCELLED
            follow_up.cancelled_at = datetime.now(UTC)
            follow_up.cancelled_reason = MEMBER_REVOKED_REASON
            return DispatchOutcome(follow_up, FollowUpStatus.CANCELLED, MEMBER_REVOKED_REASON)

        contact = await self._contacts.require_by_id(conversation.contact_id)
        if not contact.accepts_campaigns:
            # Re-read here rather than trusted from scheduling, exactly as the
            # campaign sweep does: somebody who says STOP after the nudge was
            # scheduled must not receive it (MSG-06).
            return self._skip(
                follow_up,
                "The customer has opted out of automated messages.",
            )

        window_open = messaging.window_open(conversation)

        def link(message: Message) -> None:
            """Name the send on this row, inside the transaction that commits it.

            So that a worker which dies between the commit and Meta's answer
            leaves a follow-up that says "a message was staged for me" rather
            than one that looks untouched and gets sent again (ADR-093).
            """
            follow_up.message_id = message.id

        if window_open and follow_up.body:
            send = messaging.send_text(
                conversation_id=conversation.id,
                body=follow_up.body,
                link=link,
                # A nudge carries no `sent_by_id`, which made it
                # indistinguishable from an AI reply in the transcript
                # (MSG-16).
                origin=MessageOrigin.FOLLOW_UP,
            )
        elif follow_up.has_template:
            # Checked again here, not only at scheduling. Meta pauses a template
            # that draws complaints without warning, and hours can pass between
            # the two moments; sending one it has since withdrawn is the thing
            # that costs a workspace its number.
            refusal = await self._template_refusal(
                str(follow_up.template_name),
                str(follow_up.template_language),
            )
            if refusal is not None:
                return self._skip(follow_up, refusal)
            # Valid in or out of the window. Preferred outside it because it is
            # the only thing Meta will accept there.
            send = messaging.send_template(
                conversation_id=conversation.id,
                name=str(follow_up.template_name),
                language=str(follow_up.template_language),
                components=follow_up.template_components,
                link=link,
                origin=MessageOrigin.FOLLOW_UP,
            )
        elif window_open:
            # In the window but nothing to say: a template-only follow-up whose
            # template has gone missing.
            return self._skip(follow_up, "The follow-up has no message to send.")
        else:
            return self._skip(
                follow_up,
                "The 24-hour service window has closed and no approved template is configured.",
            )

        try:
            message = await send
        except ProviderAuthError:
            # Let out rather than recorded against this nudge. The number's
            # credential is refused, so every other follow-up queued for this
            # workspace fails the same way; the worker stops sweeping them for
            # the rest of the pass instead of discovering it one at a time
            # (MSG-18).
            raise
        except (ExternalServiceError, RateLimitedError, ValidationError) as error:
            detail = str(error)
            return await self._settle(follow_up, claim, lambda row: self._fail(row, detail))

        return await self._settle(
            follow_up,
            claim,
            lambda row: self._record_sent(
                row, message, used_template=not (window_open and row.body)
            ),
        )

    async def _settle(
        self,
        follow_up: FollowUp,
        claim: uuid.UUID | None,
        outcome: Callable[[FollowUp], DispatchOutcome],
    ) -> DispatchOutcome:
        """Write what the send came to, if this row is still the one that was claimed.

        The send commits its intent before Meta is asked, which ends the lock
        the re-take held, so the outcome lands in a later transaction. Nothing
        but this dispatch may change a row whose send is committed - cancel and
        reschedule refuse one - but a lease that ran out while Meta was slow
        can have handed it to another worker, and the outcome is then that
        worker's to write. Proved under a fresh lock before anything is written.
        """
        current = await self._follow_ups.reacquire(follow_up.id, claim)
        if current is None:
            logger.warning(
                "follow_up.claim_lost",
                extra={"event": "follow_up.claim_lost", "follow_up_id": str(follow_up.id)},
            )
            return DispatchOutcome(follow_up, follow_up.status, "The claim was lost.")
        return outcome(current)

    async def _creator_is_member(self, follow_up: FollowUp) -> bool:
        """Whether the colleague who scheduled this is still in the workspace.

        A reminder with no recorded creator - one written before attribution
        existed, or whose account row is gone - has nobody to have left, and
        belongs to the workspace.
        """
        if follow_up.created_by_id is None:
            return True
        return await self._memberships.get_for_user(follow_up.created_by_id) is not None

    def _record_sent(
        self,
        follow_up: FollowUp,
        message: Message,
        *,
        used_template: bool,
    ) -> DispatchOutcome:
        if message.delivery_uncertain:
            # Meta did not answer, and there is no way to ask what it did with
            # the request. Terminal rather than retried: a nudge nobody is sure
            # about is not worth risking a second copy of (ADR-093).
            return self._abandon(
                follow_up,
                "WhatsApp did not confirm this message, so it was not sent again.",
            )

        if message.status is MessageStatus.FAILED:
            # The messaging service records a rejected send rather than raising,
            # so the failure arrives as a row state.
            return self._fail(follow_up, message.failure_reason or "The message was rejected.")

        _release_claim(follow_up)
        follow_up.status = FollowUpStatus.SENT
        follow_up.sent_at = datetime.now(UTC)
        follow_up.message_id = message.id
        follow_up.last_error = None
        logger.info(
            "follow_up.sent",
            extra={
                "follow_up_id": str(follow_up.id),
                "conversation_id": str(follow_up.conversation_id),
                "used_template": used_template,
            },
        )
        return DispatchOutcome(follow_up, FollowUpStatus.SENT)

    async def _workspace_is_served(self) -> bool:
        """Whether this workspace is still one Wasla sends for.

        Columns rather than the mapped row, for the reason `app.agents.lifecycle`
        gives: the tenant may already be in this session's identity map with the
        attributes it was loaded with, and what this needs is the row as it is
        now. Suspension and soft deletion both stop service; retention decides
        when a deleted workspace's data is erased, not whether it is served in
        the meantime.
        """
        row = (
            await self._session.execute(
                select(Tenant.status, Tenant.deleted_at)
                .where(Tenant.id == self._tenant_id)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            return False
        status, deleted_at = row
        return status is TenantStatus.ACTIVE and deleted_at is None

    async def cancel_agent_follow_ups(self, *, reason: str) -> int:
        """Cancel every pending nudge an agent scheduled. Returns how many.

        Called when a workspace stops being served (PD-TOOLS-06). The dispatch
        gate above is what guarantees nothing is sent; this is what stops the
        rows waiting, so a colleague looking at a suspended workspace sees no
        AI messages queued against their customers and a restore does not have
        to reason about a backlog.

        Only agent-created nudges. A colleague's own scheduled follow-up is
        their work, and a suspension that silently discarded it would lose
        something a person did rather than something a model decided.
        """
        pending = [
            follow_up
            for follow_up in await self._follow_ups.list_pending_by_actor(ActorKind.AGENT)
            if not follow_up.is_in_flight
        ]
        for follow_up in pending:
            self._cancel(follow_up, reason=reason)
        if pending:
            logger.info(
                "follow_up.cancelled_on_workspace_change",
                extra={
                    "event": "follow_up.cancelled_on_workspace_change",
                    "tenant_id": str(self._tenant_id),
                    "cancelled": len(pending),
                },
            )
        return len(pending)

    def _skip(self, follow_up: FollowUp, detail: str) -> DispatchOutcome:
        """Record a follow-up that policy forbade sending.

        Terminal on purpose. The service window does not reopen by itself, so a
        retry would be a message that can never legally go out.
        """
        _release_claim(follow_up)
        follow_up.status = FollowUpStatus.SKIPPED
        follow_up.last_error = detail[:500]
        logger.info(
            "follow_up.skipped",
            extra={"follow_up_id": str(follow_up.id), "detail": detail},
        )
        return DispatchOutcome(follow_up, FollowUpStatus.SKIPPED, detail)

    def _abandon(self, follow_up: FollowUp, detail: str) -> DispatchOutcome:
        """A send whose outcome nobody can determine. Terminal, and never retried.

        Kept apart from `_fail`, which is for attempts that provably delivered
        nothing and may be tried again, and from `_skip`, which is for sends
        policy forbade. This one records that a message may be on somebody's
        phone - the row names it - and that Wasla declined to send another
        (ADR-093).
        """
        _release_claim(follow_up)
        follow_up.attempts += 1
        follow_up.status = FollowUpStatus.FAILED
        follow_up.last_error = detail[:500]
        logger.warning(
            "follow_up.delivery_uncertain",
            extra={
                "event": "follow_up.delivery_uncertain",
                "follow_up_id": str(follow_up.id),
            },
        )
        return DispatchOutcome(follow_up, FollowUpStatus.FAILED, detail)

    def _fail(self, follow_up: FollowUp, detail: str) -> DispatchOutcome:
        """Record an attempt that broke, leaving it retryable until it is not.

        Kept pending while attempts remain, because the causes here — a network
        blip, a rate limit — are usually temporary. Once exhausted it becomes
        `FAILED` and stops, so a permanently broken follow-up is not retried
        forever against a customer who might eventually receive all of them.
        """
        # Released whatever happens next: a retry is claimed afresh when its
        # backoff elapses, and a final failure holds no claim at all.
        _release_claim(follow_up)
        follow_up.attempts += 1
        follow_up.last_error = detail[:500]
        # The message this attempt staged, if it staged one, is a finished
        # undelivered send. Unlinking it keeps the invariant the guard in
        # `dispatch` reads: a pending follow-up naming a message is one whose
        # send nobody could resolve.
        follow_up.message_id = None

        if follow_up.is_exhausted:
            follow_up.status = FollowUpStatus.FAILED
            logger.warning(
                "follow_up.failed",
                extra={"follow_up_id": str(follow_up.id), "attempts": follow_up.attempts},
            )
            return DispatchOutcome(follow_up, FollowUpStatus.FAILED, detail)

        # Pushed out rather than retried immediately: the next sweep would
        # otherwise pick it straight back up and burn the attempts in seconds.
        follow_up.scheduled_at = datetime.now(UTC) + _backoff(follow_up.attempts)
        logger.info(
            "follow_up.retry_scheduled",
            extra={"follow_up_id": str(follow_up.id), "attempts": follow_up.attempts},
        )
        return DispatchOutcome(follow_up, FollowUpStatus.PENDING, detail)


def _release_claim(follow_up: FollowUp) -> None:
    """Take the sweep's claim off a row, so no worker holding it can send."""
    follow_up.claim_token = None
    follow_up.claimed_until = None


def _backoff(attempts: int) -> timedelta:
    """How long to wait before trying again. Doubles, bounded by MAX_ATTEMPTS."""
    return timedelta(minutes=5 * (2 ** (attempts - 1)))


def _validated_body(body: str | None) -> str | None:
    if body is None:
        return None
    text = body.strip()
    if not text:
        return None
    if len(text) > MAX_BODY_LENGTH:
        raise ValidationError(f"A follow-up message cannot exceed {MAX_BODY_LENGTH} characters.")
    return text


def _validated_template(name: str | None, language: str | None) -> tuple[str | None, str | None]:
    """A template needs both halves or neither.

    Only the shape is checked here: a name without a language would fail at
    Meta, after the send has already been attempted. Whether Meta has approved
    the template is a question for the registry, and the caller asks it
    separately because the answer needs the database.
    """
    clean_name = name.strip() if name else None
    clean_language = language.strip() if language else None

    if bool(clean_name) != bool(clean_language):
        raise ValidationError("A template needs both a name and a language.")
    return clean_name, clean_language


def _trimmed(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text[:MAX_REASON_LENGTH] if text else None


__all__ = [
    "DEFAULT_DELAY",
    "DISPATCH_IN_PROGRESS",
    "MAX_ATTEMPTS",
    "MAX_DELAY",
    "MEMBER_REVOKED_REASON",
    "MIN_DELAY",
    "DispatchOutcome",
    "FollowUpService",
]
