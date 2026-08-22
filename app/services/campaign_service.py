"""Composing, scheduling and running campaigns.

A campaign is the one place in Wasla where the system writes to thousands of
people who did not just say something. Everything here is arranged around that:
the rules are strict, they are checked twice, and every refusal is recorded
rather than raised past the point where it can be seen.

Four rules define what a campaign is allowed to be.

**Only an approved template.** Checked when the campaign is composed and again
before every batch, because Meta pauses a template for quality without telling
anyone and hours pass between the two moments.

**Only people who wrote to this business.** The audience is built from
conversations on the sending number. There is no route that uploads a list of
phone numbers, and that absence is the anti-spam boundary rather than a missing
feature.

**Never someone who asked to stop.** Opt-out is part of the base population, not
a filter a caller can omit, and it is checked again at send time — a person who
opts out while a campaign is running must not receive the rest of it.

**Never faster than the number can bear.** The rate limit is stored on the row
and advanced as batches go out, so a worker that dies does not hand its
successor permission to send a burst.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    RateLimitedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.pagination import Cursor, Page, paginate
from app.db.models.campaign import (
    DEFAULT_MESSAGES_PER_MINUTE,
    MAX_CAMPAIGN_NAME_LENGTH,
    MAX_ERROR_LENGTH,
    MAX_MESSAGES_PER_MINUTE,
    MIN_MESSAGES_PER_MINUTE,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    OptOutSource,
    RecipientStatus,
)
from app.db.models.conversation import Contact, MessageStatus
from app.repositories.campaign_repository import (
    DEFAULT_RECIPIENT_BATCH,
    AudienceFilter,
    AudienceRepository,
    CampaignRecipientRepository,
    CampaignRepository,
    CampaignStatistics,
)
from app.repositories.conversation_repository import ContactRepository, ConversationRepository
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.services.messaging_service import MessagingService
from app.services.template_service import refusal_reason_for

logger = get_logger(__name__)

# How large an audience one campaign may hold. Not a licence to send that many —
# the rate limit governs that — but a bound on how much one materialisation
# writes in a single transaction, and a number a workspace has to ask to exceed.
MAX_AUDIENCE_SIZE: Final = 50_000

# How far ahead a campaign may be scheduled. The lower bound is zero: sending
# now is a legitimate thing to ask for, unlike a follow-up, which exists
# precisely to happen later.
MAX_SCHEDULE_AHEAD: Final = timedelta(days=90)

# Statuses a campaign may be scheduled from. A running one is excluded on
# purpose: rescheduling something already writing to people is not a schedule
# change, it is a pause followed by a decision.
#
# `FAILED` is included, and that is deliberate. A campaign fails because of a
# condition outside itself — a number disabled, a template withdrawn, a missing
# credential — and every one of those is something a workspace fixes and then
# wants to carry on from. Its remaining recipients are still pending and still
# have not been written to, so resuming sends to them and to nobody twice. What
# `FAILED` keeps that `PAUSED` does not is `last_error`: the reason it stopped.
SCHEDULABLE_FROM: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.DRAFT, CampaignStatus.PAUSED, CampaignStatus.FAILED},
)


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """What one sweep of one campaign did."""

    campaign_id: uuid.UUID
    status: CampaignStatus
    sent: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def attempted(self) -> int:
        return self.sent + self.failed + self.skipped


class CampaignService:
    """Campaign operations for one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        messaging: MessagingService | None = None,
    ) -> None:
        """`messaging` is needed only to send.

        Composing, scheduling and cancelling touch nothing outside the database,
        so a request constructs this without one. The worker supplies it, and a
        test supplies a stub to drive the compliance branches without a WhatsApp
        account.
        """
        self._session = session
        self._tenant_id = tenant_id
        self._messaging = messaging
        self._campaigns = CampaignRepository(session, tenant_id=tenant_id)
        self._recipients = CampaignRecipientRepository(session, tenant_id=tenant_id)
        self._audience = AudienceRepository(session, tenant_id=tenant_id)
        self._accounts = WhatsAppAccountRepository(session, tenant_id=tenant_id)
        self._templates = WhatsAppTemplateRepository(session, tenant_id=tenant_id)
        self._contacts = ContactRepository(session, tenant_id=tenant_id)
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)

    # ------------------------------------------------------------------ reads

    async def get(self, campaign_id: uuid.UUID) -> Campaign:
        return await self._campaigns.require_by_id(campaign_id)

    async def list_campaigns(
        self,
        *,
        statuses: tuple[CampaignStatus, ...] = (),
        account_id: uuid.UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[Campaign]:
        after = Cursor.decode(cursor) if cursor else None
        rows = await self._campaigns.list_campaigns(
            statuses=statuses,
            account_id=account_id,
            limit=limit,
            after=after,
        )
        return paginate(
            rows,
            limit=limit,
            key=lambda row: Cursor(sort_value=row.created_at, id=row.id),
        )

    async def statistics(self, campaign_id: uuid.UUID) -> CampaignStatistics:
        campaign = await self._campaigns.require_by_id(campaign_id)
        return await self._recipients.statistics(campaign.id)

    async def list_recipients(
        self,
        campaign_id: uuid.UUID,
        *,
        status: RecipientStatus | None = None,
        limit: int = 100,
    ) -> list[CampaignRecipient]:
        campaign = await self._campaigns.require_by_id(campaign_id)
        return await self._recipients.list_for_campaign(campaign.id, status=status, limit=limit)

    # -------------------------------------------------------------- composing

    async def create(
        self,
        *,
        account_id: uuid.UUID,
        template_id: uuid.UUID,
        name: str,
        description: str | None = None,
        variables: list[str] | None = None,
        messages_per_minute: int = DEFAULT_MESSAGES_PER_MINUTE,
        created_by_id: uuid.UUID | None = None,
    ) -> Campaign:
        """Compose a draft. Nothing is sent and no audience exists yet."""
        account = await self._accounts.require_by_id(account_id)
        if not account.is_active:
            raise ValidationError("This WhatsApp number is disabled.")

        template = await self._templates.require_by_id(template_id)
        if template.account_id != account_id:
            # Templates belong to a WhatsApp Business account, and Meta will not
            # render one from a number that does not have it.
            raise ValidationError("That template does not belong to this WhatsApp number.")
        refusal = refusal_reason_for(template)
        if refusal is not None:
            raise ValidationError(refusal)
        if not template.is_sendable:
            # Unreachable while `refusal_reason_for` refuses everything else,
            # and kept because a campaign is the one caller where "unknown to
            # the registry" must not mean "allowed".
            raise ValidationError("A campaign needs a template WhatsApp has approved.")

        supplied = list(variables or [])
        if len(supplied) != template.variable_count:
            raise ValidationError(
                f"This template expects {template.variable_count} variable(s); "
                f"{len(supplied)} were supplied."
            )

        return self._campaigns.create(
            account_id=account_id,
            template_id=template_id,
            name=_validated_name(name),
            description=description,
            variables=supplied or None,
            messages_per_minute=_validated_rate(messages_per_minute),
            created_by_id=created_by_id,
        )

    async def preview_audience(
        self,
        *,
        account_id: uuid.UUID,
        filters: AudienceFilter,
    ) -> int:
        """How many people this filter would reach, without writing anything."""
        await self._accounts.require_by_id(account_id)
        return await self._audience.count_eligible(account_id=account_id, filters=filters)

    async def set_audience(
        self,
        *,
        campaign_id: uuid.UUID,
        filters: AudienceFilter,
    ) -> Campaign:
        """Materialise the recipient list.

        Written out now rather than computed as the campaign runs. A lazy
        audience would change under the campaign's feet — a contact who writes
        in halfway through drops out of a filter defined by silence — and nobody
        could answer "who was this sent to" afterwards.

        Draft only. Rebuilding the list of a campaign that has already sent to
        some of it would either duplicate those people or silently drop them.
        """
        campaign = await self._campaigns.require_by_id(campaign_id)
        if campaign.status is not CampaignStatus.DRAFT:
            raise ValidationError("An audience can only be set while the campaign is a draft.")

        contacts = await self._audience.list_eligible(
            account_id=campaign.account_id,
            filters=filters,
            limit=MAX_AUDIENCE_SIZE + 1,
        )
        if len(contacts) > MAX_AUDIENCE_SIZE:
            raise ValidationError(
                f"This audience is larger than {MAX_AUDIENCE_SIZE:,} contacts. "
                "Narrow it before sending."
            )

        already = await self._recipients.existing_contact_ids(campaign.id)
        added = 0
        for contact in contacts:
            if contact.id in already:
                continue
            self._recipients.create(campaign_id=campaign.id, contact_id=contact.id)
            added += 1

        campaign.audience = filters.as_dict()
        campaign.audience_size = len(already) + added
        logger.info(
            "campaign.audience_set",
            extra={"campaign_id": str(campaign.id), "audience_size": campaign.audience_size},
        )
        return campaign

    # -------------------------------------------------------------- lifecycle

    async def schedule(
        self,
        *,
        campaign_id: uuid.UUID,
        scheduled_at: datetime | None = None,
    ) -> Campaign:
        """Hand the campaign to the worker, now or at a moment in the future.

        `None` means now, which is the common case: a person has reviewed the
        audience and wants it to go. The worker still does the sending, so even
        "now" never blocks a request behind ten thousand messages.
        """
        campaign = await self._campaigns.require_by_id(campaign_id)
        if campaign.status not in SCHEDULABLE_FROM:
            raise ValidationError(f"A {campaign.status.value} campaign cannot be scheduled.")

        pending = await self._recipients.pending_count(campaign.id)
        if pending == 0:
            raise ValidationError("This campaign has nobody left to send to.")

        now = datetime.now(UTC)
        when = now if scheduled_at is None else _aware(scheduled_at)
        if when - now > MAX_SCHEDULE_AHEAD:
            raise ValidationError(
                f"A campaign cannot be scheduled more than {MAX_SCHEDULE_AHEAD.days} days ahead."
            )

        campaign.status = CampaignStatus.SCHEDULED
        campaign.scheduled_at = max(when, now)
        campaign.next_send_at = None
        campaign.last_error = None
        logger.info(
            "campaign.scheduled",
            extra={
                "campaign_id": str(campaign.id),
                "scheduled_at": campaign.scheduled_at.isoformat(),
                "pending": pending,
            },
        )
        return campaign

    async def pause(self, campaign_id: uuid.UUID) -> Campaign:
        """Stop sending, keep the rest of the list.

        Reversible, which is the whole point: somebody watching a campaign go
        out and having second thoughts needs an action that is not destructive.
        """
        campaign = await self._campaigns.require_by_id(campaign_id)
        if campaign.is_finished:
            raise ValidationError(f"A {campaign.status.value} campaign cannot be paused.")
        if campaign.status is CampaignStatus.DRAFT:
            raise ValidationError("A draft is not sending.")

        campaign.status = CampaignStatus.PAUSED
        logger.info("campaign.paused", extra={"campaign_id": str(campaign.id)})
        return campaign

    async def cancel(self, campaign_id: uuid.UUID) -> Campaign:
        """Finish the campaign where it stands. What was sent stays sent."""
        campaign = await self._campaigns.require_by_id(campaign_id)
        if campaign.is_finished:
            return campaign

        campaign.status = CampaignStatus.CANCELLED
        campaign.cancelled_at = datetime.now(UTC)
        logger.info("campaign.cancelled", extra={"campaign_id": str(campaign.id)})
        return campaign

    # ------------------------------------------------------------------ sending

    async def dispatch_batch(
        self,
        campaign: Campaign,
        *,
        now: datetime | None = None,
        batch_limit: int = DEFAULT_RECIPIENT_BATCH,
    ) -> BatchOutcome:
        """Send the next batch of one campaign, then set when it may send again.

        The worker calls this and does nothing else. Every compliance question
        is re-asked here rather than trusted from composition time, because a
        campaign scheduled last night is a campaign whose number may have been
        disabled and whose template may have been paused since.
        """
        moment = now or datetime.now(UTC)

        if campaign.is_finished or campaign.status is CampaignStatus.PAUSED:
            return BatchOutcome(campaign.id, campaign.status)
        if campaign.status is CampaignStatus.DRAFT:
            # Nothing hands a draft to the worker; if one arrives, it is a bug
            # somewhere else and sending would be the wrong way to find out.
            return BatchOutcome(campaign.id, campaign.status)

        blocked = await self._blocking_reason(campaign)
        if blocked is not None:
            return self._fail(campaign, blocked)

        messaging = self._messaging
        if messaging is None:
            raise RuntimeError("CampaignService needs a messaging service to send.")

        if campaign.status is CampaignStatus.SCHEDULED:
            campaign.status = CampaignStatus.RUNNING
            campaign.started_at = campaign.started_at or moment

        allowance = max(1, min(batch_limit, campaign.messages_per_minute))
        claimed = await self._recipients.claim_pending(campaign.id, limit=allowance)
        if not claimed:
            return self._complete(campaign, moment)

        sent = failed = skipped = 0
        for recipient in claimed:
            try:
                outcome = await self._deliver(campaign, recipient, messaging=messaging, now=moment)
            except DependencyUnavailableError as error:
                # A missing platform credential, which is neither this
                # recipient's problem nor fixable by trying the next one. Left
                # as a per-recipient failure it would loop forever without ever
                # exhausting anyone's attempts, staging a message row per
                # recipient per sweep on a deployment that cannot send at all.
                return self._fail(campaign, str(error))
            if outcome is RecipientStatus.SENT:
                sent += 1
            elif outcome is RecipientStatus.SKIPPED:
                skipped += 1
            else:
                failed += 1

        # Rate limiting, stored rather than slept. A sleep would hold the lock
        # and would not survive a restart; a timestamp does both.
        campaign.next_send_at = moment + timedelta(
            minutes=len(claimed) / campaign.messages_per_minute
        )

        remaining = await self._recipients.pending_count(campaign.id)
        if remaining == 0:
            self._complete(campaign, moment)

        logger.info(
            "campaign.batch_sent",
            extra={
                "campaign_id": str(campaign.id),
                "sent": sent,
                "failed": failed,
                "skipped": skipped,
                "remaining": remaining,
            },
        )
        return BatchOutcome(
            campaign_id=campaign.id,
            status=campaign.status,
            sent=sent,
            failed=failed,
            skipped=skipped,
        )

    async def _blocking_reason(self, campaign: Campaign) -> str | None:
        """Why this campaign must not send at all, or None.

        Distinct from a recipient failing: these are conditions that will not
        improve by trying the next person, so the campaign stops rather than
        working through its list collecting the same error ten thousand times.
        """
        account = await self._accounts.get_by_id(campaign.account_id)
        if account is None or not account.is_active:
            return "The WhatsApp number this campaign sends from is disabled."

        template = await self._templates.get_by_id(campaign.template_id)
        if template is None:
            return "The template this campaign sends has been removed."
        refusal = refusal_reason_for(template)
        if refusal is not None or not template.is_sendable:
            return refusal or "WhatsApp has not approved this template."
        return None

    async def _deliver(
        self,
        campaign: Campaign,
        recipient: CampaignRecipient,
        *,
        messaging: MessagingService,
        now: datetime,
    ) -> RecipientStatus:
        """Send one person's copy and record what happened to it."""
        contact = await self._contacts.get_by_id(recipient.contact_id)
        if contact is None:
            return self._skip(recipient, "This contact no longer exists.")
        if not contact.accepts_campaigns:
            # Checked again here, not only when the audience was built. Somebody
            # who opts out while a campaign is running must not receive the rest
            # of it.
            return self._skip(recipient, "This contact has opted out of campaigns.")

        conversation, _ = await self._conversations.get_or_create(
            contact_id=contact.id,
            account_id=campaign.account_id,
        )
        await self._session.flush()
        recipient.conversation_id = conversation.id

        template = await self._templates.require_by_id(campaign.template_id)
        try:
            message = await messaging.send_template(
                conversation_id=conversation.id,
                name=template.name,
                language=template.language,
                components=_components(campaign.variables),
                sent_by_id=campaign.created_by_id,
            )
        except (ExternalServiceError, RateLimitedError, ValidationError) as error:
            return self._fail_recipient(recipient, str(error))

        if message.status is MessageStatus.FAILED:
            # The messaging service records a rejected send rather than raising,
            # so a refusal arrives as a row state.
            return self._fail_recipient(
                recipient, message.failure_reason or "Rejected by WhatsApp."
            )

        recipient.status = RecipientStatus.SENT
        recipient.sent_at = now
        recipient.message_id = message.id
        recipient.last_error = None
        return RecipientStatus.SENT

    def _skip(self, recipient: CampaignRecipient, detail: str) -> RecipientStatus:
        """A policy outcome, never retried: the reason will not change by itself."""
        recipient.status = RecipientStatus.SKIPPED
        recipient.last_error = detail[:MAX_ERROR_LENGTH]
        return RecipientStatus.SKIPPED

    def _fail_recipient(self, recipient: CampaignRecipient, detail: str) -> RecipientStatus:
        """An attempt that broke. Retried until the attempts run out."""
        recipient.attempts += 1
        recipient.last_error = detail[:MAX_ERROR_LENGTH]
        if recipient.is_exhausted:
            recipient.status = RecipientStatus.FAILED
            return RecipientStatus.FAILED
        # Left pending, so the next batch picks it up. No backoff of its own:
        # the campaign's own rate limit already spaces the attempts out.
        return RecipientStatus.PENDING

    def _complete(self, campaign: Campaign, now: datetime) -> BatchOutcome:
        campaign.status = CampaignStatus.COMPLETED
        campaign.completed_at = now
        campaign.next_send_at = None
        logger.info("campaign.completed", extra={"campaign_id": str(campaign.id)})
        return BatchOutcome(campaign.id, CampaignStatus.COMPLETED)

    def _fail(self, campaign: Campaign, detail: str) -> BatchOutcome:
        campaign.status = CampaignStatus.FAILED
        campaign.last_error = detail[:MAX_ERROR_LENGTH]
        campaign.next_send_at = None
        logger.warning(
            "campaign.blocked",
            extra={"campaign_id": str(campaign.id), "detail": detail},
        )
        return BatchOutcome(campaign.id, CampaignStatus.FAILED)

    # ------------------------------------------------------------------ opt-out

    async def set_opt_out(
        self,
        *,
        contact_id: uuid.UUID,
        source: OptOutSource,
        at: datetime | None = None,
    ) -> Contact:
        """Record that this person does not want campaign messages.

        Idempotent, and it never moves the timestamp forward. The first refusal
        is the one that matters: a second "stop" from someone already opted out
        must not make it look as though they only just decided.
        """
        contact = await self._contacts.require_by_id(contact_id)
        if contact.marketing_opt_out_at is not None:
            return contact

        contact.marketing_opt_out_at = at or datetime.now(UTC)
        contact.opt_out_source = source
        logger.info(
            "campaign.opt_out_recorded",
            extra={"contact_id": str(contact.id), "source": source.value},
        )
        return contact

    async def clear_opt_out(self, contact_id: uuid.UUID) -> Contact:
        """Let this person receive campaigns again.

        A person's own decision to stop is not one a workspace should undo
        lightly, and this exists mostly for the case a colleague recorded it in
        error. What it cannot do is be triggered by anything automatic: nothing
        on the inbound path calls it, so a customer writing back after opting
        out does not silently re-enrol.
        """
        contact = await self._contacts.require_by_id(contact_id)
        contact.marketing_opt_out_at = None
        contact.opt_out_source = None
        logger.info("campaign.opt_out_cleared", extra={"contact_id": str(contact.id)})
        return contact


def _components(variables: list[str] | None) -> list[dict[str, Any]] | None:
    """The template body's parameters, in Meta's shape.

    None when there are no variables: sending an empty component list is not the
    same as sending none, and Meta rejects the former on a template with no
    placeholders.
    """
    if not variables:
        return None
    return [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": value} for value in variables],
        }
    ]


def _validated_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise ValidationError("A campaign needs a name.")
    return trimmed[:MAX_CAMPAIGN_NAME_LENGTH]


def _validated_rate(messages_per_minute: int) -> int:
    if not MIN_MESSAGES_PER_MINUTE <= messages_per_minute <= MAX_MESSAGES_PER_MINUTE:
        raise ValidationError(
            f"The send rate must be between {MIN_MESSAGES_PER_MINUTE} and "
            f"{MAX_MESSAGES_PER_MINUTE} messages per minute."
        )
    return messages_per_minute


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


__all__ = [
    "MAX_AUDIENCE_SIZE",
    "MAX_SCHEDULE_AHEAD",
    "BatchOutcome",
    "CampaignService",
]
