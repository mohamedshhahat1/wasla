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
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ExternalServiceError, RateLimitedError, ValidationError
from app.core.logging import get_logger
from app.core.pagination import Cursor, Page, paginate
from app.db.models.conversation import ConversationStatus, MessageStatus
from app.db.models.follow_up import (
    MAX_ATTEMPTS,
    MAX_BODY_LENGTH,
    MAX_REASON_LENGTH,
    FollowUp,
    FollowUpStatus,
)
from app.db.models.lead import ActorKind
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.follow_up_repository import FollowUpRepository
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
        self._templates = WhatsAppTemplateRepository(session, tenant_id=tenant_id)

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
        conversation = await self._conversations.require_by_id(conversation_id)
        if conversation.status is ConversationStatus.CLOSED:
            raise ValidationError("This conversation is closed.")

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

        existing = await self._follow_ups.get_pending_for_conversation(conversation_id)
        if existing is not None:
            existing.scheduled_at = when
            existing.body = text
            existing.template_name = name
            existing.template_language = language
            existing.template_components = template_components
            existing.reason = _trimmed(reason)
            existing.created_by_id = created_by_id
            existing.created_by_kind = created_by_kind
            # Reset, because this is a fresh intention rather than a retry of
            # the old one.
            existing.attempts = 0
            existing.last_error = None
            logger.info(
                "follow_up.rescheduled",
                extra={"follow_up_id": str(existing.id), "conversation_id": str(conversation_id)},
            )
            return existing

        follow_up = self._follow_ups.create(
            conversation_id=conversation_id,
            scheduled_at=when,
            body=text,
            template_name=name,
            template_language=language,
            template_components=template_components,
            reason=_trimmed(reason),
            lead_id=lead_id,
            created_by_id=created_by_id,
            created_by_kind=created_by_kind,
        )
        logger.info(
            "follow_up.scheduled",
            extra={"conversation_id": str(conversation_id), "scheduled_at": when.isoformat()},
        )
        return follow_up

    def _resolve_time(
        self,
        *,
        delay: timedelta | None,
        scheduled_at: datetime | None,
    ) -> datetime:
        """Turn a delay or an absolute time into a bounded absolute time."""
        now = datetime.now(UTC)
        if scheduled_at is not None:
            when = scheduled_at if scheduled_at.tzinfo else scheduled_at.replace(tzinfo=UTC)
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
        """Cancel one follow-up by id.

        A follow-up that already finished is returned untouched rather than
        raising: cancelling something that has been sent is a race, not a
        mistake, and the caller's intent is already satisfied.
        """
        follow_up = await self._follow_ups.require_by_id(follow_up_id)
        if not follow_up.is_pending:
            return follow_up
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
        pending = await self._follow_ups.list_pending_for_conversation(conversation_id)
        for follow_up in pending:
            self._cancel(follow_up, reason=reason)
        if pending:
            logger.info(
                "follow_up.cancelled_on_reply",
                extra={"conversation_id": str(conversation_id), "cancelled": len(pending)},
            )
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

        messaging = self._messaging
        if messaging is None:
            if self._settings is None:
                raise RuntimeError("FollowUpService needs settings or a messaging service to send.")
            messaging = MessagingService(
                session=self._session,
                settings=self._settings,
                tenant_id=self._tenant_id,
            )

        conversation = await self._conversations.require_by_id(follow_up.conversation_id)
        if conversation.status is ConversationStatus.CLOSED:
            return self._skip(
                follow_up, "The conversation was closed before the follow-up was due."
            )

        window_open = messaging.window_open(conversation)

        if window_open and follow_up.body:
            send = messaging.send_text(conversation_id=conversation.id, body=follow_up.body)
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
        except (ExternalServiceError, RateLimitedError, ValidationError) as error:
            return self._fail(follow_up, str(error))

        if message.status is MessageStatus.FAILED:
            # The messaging service records a rejected send rather than raising,
            # so the failure arrives as a row state.
            return self._fail(follow_up, message.failure_reason or "The message was rejected.")

        follow_up.status = FollowUpStatus.SENT
        follow_up.sent_at = datetime.now(UTC)
        follow_up.message_id = message.id
        follow_up.last_error = None
        logger.info(
            "follow_up.sent",
            extra={
                "follow_up_id": str(follow_up.id),
                "conversation_id": str(conversation.id),
                "used_template": not (window_open and follow_up.body),
            },
        )
        return DispatchOutcome(follow_up, FollowUpStatus.SENT)

    def _skip(self, follow_up: FollowUp, detail: str) -> DispatchOutcome:
        """Record a follow-up that policy forbade sending.

        Terminal on purpose. The service window does not reopen by itself, so a
        retry would be a message that can never legally go out.
        """
        follow_up.status = FollowUpStatus.SKIPPED
        follow_up.last_error = detail[:500]
        logger.info(
            "follow_up.skipped",
            extra={"follow_up_id": str(follow_up.id), "detail": detail},
        )
        return DispatchOutcome(follow_up, FollowUpStatus.SKIPPED, detail)

    def _fail(self, follow_up: FollowUp, detail: str) -> DispatchOutcome:
        """Record an attempt that broke, leaving it retryable until it is not.

        Kept pending while attempts remain, because the causes here — a network
        blip, a rate limit — are usually temporary. Once exhausted it becomes
        `FAILED` and stops, so a permanently broken follow-up is not retried
        forever against a customer who might eventually receive all of them.
        """
        follow_up.attempts += 1
        follow_up.last_error = detail[:500]

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
    "MAX_ATTEMPTS",
    "MAX_DELAY",
    "MIN_DELAY",
    "DispatchOutcome",
    "FollowUpService",
]
