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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.channels.outcomes import (
    ProviderAuthError,
    ProviderConnectionRefusedError,
    ProviderUnreachableError,
)
from app.channels.registry import PAUSED_RECHECK, ChannelPausedError
from app.channels.throughput import ConnectionThrottledError
from app.core.crypto import CredentialDecryptionError
from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    PlanLimitExceededError,
    RateLimitedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.pagination import Cursor, Page, paginate
from app.core.storage import MediaStorage, StorageError
from app.core.telemetry import (
    record_campaign_event,
    record_campaign_send,
    record_campaign_stop,
    record_campaign_wait,
)
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import LimitKey, Subscription, SubscriptionStatus
from app.db.models.campaign import (
    DEFAULT_MESSAGES_PER_MINUTE,
    MAX_CAMPAIGN_NAME_LENGTH,
    MAX_ERROR_LENGTH,
    MAX_MESSAGES_PER_MINUTE,
    MIN_MESSAGES_PER_MINUTE,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    CampaignStopReason,
    CampaignWaitReason,
    OptOutSource,
    OptOutVia,
    RecipientReason,
    RecipientStatus,
)
from app.db.models.campaign_media import CampaignHeaderMedia, HeaderMediaKind
from app.db.models.channel import Channel, ContactIdentity
from app.db.models.consent import ContactChannelConsent
from app.db.models.conversation import (
    Contact,
    Message,
    MessageDeliveryState,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.db.models.user import User
from app.db.models.whatsapp_template import TemplateStatus
from app.db.session import released
from app.integrations.whatsapp.client import (
    MARKETING_OPTED_OUT_CODE,
    PAIR_RATE_LIMIT_CODE,
    TEMPLATE_WITHDRAWN_CODES,
    MediaUploadRefusedError,
)
from app.repositories.campaign_media_repository import CampaignHeaderMediaRepository
from app.repositories.campaign_repository import (
    DEFAULT_RECIPIENT_BATCH,
    AudienceFilter,
    AudienceRepository,
    CampaignRecipientRepository,
    CampaignRepository,
    CampaignStatistics,
)
from app.repositories.channel_repository import (
    ChannelConnectionRepository,
    ContactIdentityRepository,
)
from app.repositories.consent_repository import ContactConsentRepository
from app.repositories.conversation_repository import (
    ContactRepository,
    ConversationRepository,
    MessageRepository,
)
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.services import messaging_limit_service as portfolio_limits
from app.services import workspace_serving as serving
from app.services.audit_service import AuditTrail
from app.services.business_initiated_ledger import (
    MessagingLimitUnknownError,
    PortfolioLimitReachedError,
)
from app.services.campaign_header_media_service import (
    CampaignHeaderMediaService,
    HeaderMediaUnavailableError,
)
from app.services.entitlement_service import (
    CHANNEL_NOT_IN_PLAN_DETAIL,
    EntitlementService,
    hold_limit_lock,
)
from app.services.messaging_service import (
    MessagingService,
    TemplateRefusedError,
    safe_upload_filename,
)
from app.services.opt_out import record_opt_out, record_resume
from app.services.quiet_hours import QuietHours
from app.services.template_parameters import (
    FILE_HEADER_FORMATS,
    TEMPLATE_PARAMETER_CODES,
    CampaignParameters,
    HeaderFileParameter,
    parameter_spec,
    send_components,
)
from app.services.template_parameters import refusal as parameter_refusal
from app.services.template_service import refusal_reason_for
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

# How large an audience one campaign may hold. Not a licence to send that many —
# the rate limit governs that — but a bound on how much one materialisation
# writes in a single transaction, and a number a workspace has to ask to exceed.
MAX_AUDIENCE_SIZE: Final = 50_000

# The stable error code a schedule is refused with when the plan in force does
# not include the campaign's channel (CMP-24): a 402, like every other plan
# refusal, because the answer is an upgrade, not a permission.
CHANNEL_NOT_IN_PLAN_CODE: Final = "channel_not_in_plan"

# The stable error code a campaign on a template that is not marketing is
# refused with at composition (CMP-09).
TEMPLATE_CATEGORY_CODE: Final = "template_category"

# The stable error code for a header file that is not stored: never written, or
# removed by retention (HM-02, HM-07).
HEADER_MEDIA_NOT_STORED_CODE: Final = "header_media_not_stored"

# The stable error code for a `scheduled_at` without a UTC offset (CMP-15).
NAIVE_SCHEDULE_CODE: Final = "scheduled_at_needs_offset"

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

# Statuses from which cancelling changes nothing, because the campaign is
# already over. `FAILED` is deliberately *not* one of them: it is resumable, so
# a workspace that has decided against a broken campaign needs a way to close it
# for good rather than leaving something a later hand could reschedule.
ALREADY_OVER: Final[frozenset[CampaignStatus]] = frozenset(
    {CampaignStatus.COMPLETED, CampaignStatus.CANCELLED},
)


@dataclass(frozen=True, slots=True)
class CampaignLease:
    """The claim a worker holds on one campaign while it sends a batch (CMP-01, ADR-134).

    `token` is what `claim_due` wrote on the row; `duration` is how long each
    renewal extends it. Renewed before every recipient, so it has to outlast
    one send - not a batch, and not a sweep of five batches.
    """

    token: uuid.UUID
    duration: timedelta


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
        entitlements: EntitlementService | None = None,
        default_plan_code: str | None = None,
        provider_unreachable_pause: timedelta | None = None,
        storage: MediaStorage | None = None,
        header_media_refresh: timedelta | None = None,
        limits: portfolio_limits.MessagingLimitService | None = None,
    ) -> None:
        """`messaging` is needed only to send.

        `entitlements` is needed only to schedule. A campaign is the one place a
        limit depends on *how much* is being asked for - ten thousand recipients
        is ten thousand messages - so it cannot be a route-level guard the way
        creating an agent can, and it is checked here instead. Without one, a
        campaign schedules unchecked: that is what the worker wants, having
        already been let through once.

        Composing, scheduling and cancelling touch nothing outside the database,
        so a request constructs this without one. The worker supplies it, and a
        test supplies a stub to drive the compliance branches without a WhatsApp
        account.
        """
        self._session = session
        self._tenant_id = tenant_id
        self._messaging = messaging
        self._entitlements = entitlements
        # The plan in force decides which channels a campaign may still send on
        # (ENT-16); a workspace not served is on the default plan.
        self._default_plan_code = default_plan_code
        # How long Meta may stay unreachable before a waiting campaign is
        # paused for a person to look at (CMP-07).
        self._provider_unreachable_pause = (
            provider_unreachable_pause
            if provider_unreachable_pause is not None
            else DEFAULT_PROVIDER_UNREACHABLE_PAUSE
        )
        # Where a campaign's header file is read from, and how old Meta's id
        # for it may grow before it is uploaded again (HM-05). The worker
        # supplies the store; a request never uploads.
        self._storage = storage
        self._header_refresh = (
            header_media_refresh
            if header_media_refresh is not None
            else DEFAULT_HEADER_MEDIA_REFRESH
        )
        # Reads Meta's messaging limit before a schedule when the stored value
        # is stale (LIM-01). A request supplies it; the worker never reads.
        self._limits = limits
        self._campaigns = CampaignRepository(session, tenant_id=tenant_id)
        self._recipients = CampaignRecipientRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._audience = AudienceRepository(session, tenant_id=tenant_id)
        self._accounts = WhatsAppAccountRepository(session, tenant_id=tenant_id)
        self._templates = WhatsAppTemplateRepository(session, tenant_id=tenant_id)
        self._contacts = ContactRepository(session, tenant_id=tenant_id)
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._usage = UsageRecorder(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)
        self._header_files = CampaignHeaderMediaRepository(session, tenant_id=tenant_id)

    # ------------------------------------------------------------------ reads

    async def get(self, campaign_id: uuid.UUID) -> Campaign:
        return await self._campaigns.require_by_id(campaign_id)

    async def header_media_for(
        self, campaigns: Sequence[Campaign]
    ) -> dict[uuid.UUID, CampaignHeaderMedia]:
        """Each campaign's header file, by campaign id, for the ones that have one (HM-08)."""
        files = await self._header_files.by_ids(
            campaign.header_media_id
            for campaign in campaigns
            if campaign.header_media_id is not None
        )
        return {
            campaign.id: files[campaign.header_media_id]
            for campaign in campaigns
            if campaign.header_media_id is not None and campaign.header_media_id in files
        }

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
        reason: RecipientReason | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[CampaignRecipient]:
        """Every recipient, a page at a time, filtered by status and reason code (CMP-13).

        Paged by id with a cursor, so a campaign of fifty thousand can be read
        to the end - the listing used to stop at the first hundred.
        """
        campaign = await self._campaigns.require_by_id(campaign_id)
        after = Cursor.decode(cursor).id if cursor else None
        rows = await self._recipients.list_for_campaign(
            campaign.id,
            status=status,
            reason=reason.value if reason is not None else None,
            limit=limit,
            after=after,
        )
        return paginate(rows, limit=limit, key=lambda row: Cursor(sort_value=None, id=row.id))

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
        header_variables: list[str] | None = None,
        button_parameters: dict[int, str] | None = None,
        header_media_id: uuid.UUID | None = None,
        actor: User | None = None,
    ) -> Campaign:
        """Compose a draft. Nothing is sent and no audience exists yet.

        **A marketing template only** (CMP-09, ADR-138), and **every parameter
        its components need** (CMP-05, ADR-137): the body's variables, a text
        header's, and each dynamic button's - or a refusal naming the
        component, before anyone is written to. A template whose components
        are not known, or whose header is a location, is refused too.

        **A media header takes the workspace's own stored file** (HM-02,
        HM-03): `header_media_id` names one of this workspace's header files,
        stored, of the kind the template's header names. Another workspace's
        id is not found. Fixed here: nothing changes it afterwards.
        """
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
        if not template.is_marketing:
            # A broadcast is marketing, which is what the model always said and
            # nothing enforced. A utility or authentication template sent to
            # thousands is miscategorised traffic Meta prices and polices as
            # such; an unknown category must not read as marketing.
            raise ValidationError(
                f"A campaign needs a marketing template; this one is "
                f"{template.category.value}.",
                error_code=TEMPLATE_CATEGORY_CODE,
            )

        header_file = None
        if header_media_id is not None:
            header_file = await self._header_files.require_by_id(header_media_id)
            if not header_file.is_stored:
                raise ValidationError(
                    "This header file is not stored. Upload it again.",
                    error_code=HEADER_MEDIA_NOT_STORED_CODE,
                )
        supplied = CampaignParameters(
            body=tuple(variables or ()),
            header=tuple(header_variables or ()),
            buttons=dict(button_parameters or {}),
            header_file_kind=header_file.kind if header_file is not None else None,
        )
        refused = parameter_refusal(parameter_spec(template.components), supplied)
        if refused is not None:
            raise refused

        campaign = self._campaigns.create(
            account_id=account_id,
            template_id=template_id,
            name=_validated_name(name),
            description=description,
            variables=list(supplied.body) or None,
            messages_per_minute=_validated_rate(messages_per_minute),
            created_by_id=created_by_id,
        )
        campaign.template_parameters = supplied.as_dict()
        campaign.header_media_id = header_media_id
        # Flushed so the caller can read the generated id and the timestamps.
        # Without it a route serialising this row answers 500: the primary key
        # default and the server defaults are applied at flush, and the request
        # commits after the response has already been built.
        await self._session.flush()
        # Audited (CMP-14): composing a broadcast is where its template and
        # number are chosen. The template's parameters stay out of the trail.
        self._audit.record(
            AuditAction.CAMPAIGN_CREATED,
            actor=actor,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
            meta={
                "account_id": str(account_id),
                "template_id": str(template_id),
                "messages_per_minute": campaign.messages_per_minute,
            },
        )
        logger.info("campaign.created", extra={"campaign_id": str(campaign.id)})
        return campaign

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
        actor: User | None = None,
    ) -> Campaign:
        """Materialise the recipient list.

        Written out now rather than computed as the campaign runs. A lazy
        audience would change under the campaign's feet — a contact who writes
        in halfway through drops out of a filter defined by silence — and nobody
        could answer "who was this sent to" afterwards.

        Draft only. Rebuilding the list of a campaign that has already sent to
        some of it would either duplicate those people or silently drop them.

        **Draft under the row lock, and under the allowance's lock** (CMP-04,
        ADR-136). The status was an unlocked read, so a wider audience could
        commit after another admin had scheduled - and reserved for - the
        narrower one, leaving a scheduled campaign owing more than it had
        reserved (audit P11b: 200 pending against 10). `schedule()` locks the
        same row, so the two are ordered: materialised first, the schedule
        counts every recipient; scheduled first, this is refused.
        """
        campaign = await self._campaigns.lock_by_id(campaign_id)
        if campaign.status is not CampaignStatus.DRAFT:
            raise ValidationError("An audience can only be set while the campaign is a draft.")
        # The campaign allowance's lock, taken after the row's as `schedule`
        # takes them, so nothing that reads promised recipients interleaves.
        await hold_limit_lock(
            self._session, tenant_id=self._tenant_id, key=LimitKey.PERIOD_CAMPAIGN_MESSAGES
        )

        members = await self._audience.list_eligible_members(
            account_id=campaign.account_id,
            filters=filters,
            limit=MAX_AUDIENCE_SIZE + 1,
        )
        if len(members) > MAX_AUDIENCE_SIZE:
            raise ValidationError(
                f"This audience is larger than {MAX_AUDIENCE_SIZE:,} contacts. "
                "Narrow it before sending."
            )

        already = await self._recipients.existing_contact_ids(campaign.id)
        added = 0
        for contact, participant_identity_id in members:
            if contact.id in already:
                continue
            # The exact identity this copy is addressed to (OMNI-004).
            self._recipients.create(
                campaign_id=campaign.id,
                contact_id=contact.id,
                participant_identity_id=participant_identity_id,
            )
            added += 1

        campaign.audience = filters.as_dict()
        campaign.audience_size = len(already) + added
        # Audited (CMP-14) with how many and which filters - the number of
        # hand-picked contacts, never who they are.
        summary = filters.as_dict()
        summary["contact_ids"] = len(filters.contact_ids)
        self._audit.record(
            AuditAction.CAMPAIGN_AUDIENCE_SET,
            actor=actor,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
            meta={"recipients": campaign.audience_size, "filters": summary},
        )
        logger.info(
            "campaign.audience_set",
            extra={"campaign_id": str(campaign.id), "audience_size": campaign.audience_size},
        )
        return campaign

    # -------------------------------------------------------------- lifecycle

    async def refresh_messaging_limit(self, campaign_id: uuid.UUID) -> None:
        """Read the sending number's limit again if it is older than the refresh age (LIM-01).

        Called by the schedule route before `schedule`, outside any lock: a
        Graph call is never made under a row or advisory lock (ADR-093). A
        read that fails leaves the stored value in force; one never read
        leaves the schedule to refuse.
        """
        if self._limits is None:
            return
        campaign = await self._campaigns.require_by_id(campaign_id)
        account = await self._accounts.get_by_id(campaign.account_id)
        if account is None:
            return
        await self._limits.refresh_if_stale(account)

    async def schedule(
        self,
        *,
        campaign_id: uuid.UUID,
        scheduled_at: datetime | None = None,
        actor: User | None = None,
    ) -> Campaign:
        """Hand the campaign to the worker, now or at a moment in the future.

        `None` means now, which is the common case: a person has reviewed the
        audience and wants it to go. The worker still does the sending, so even
        "now" never blocks a request behind ten thousand messages.
        """
        # Locked, and read as it is now (CMP-04): an audience being
        # materialised holds this row until it commits, and is then counted.
        campaign = await self._campaigns.lock_by_id(campaign_id)
        if campaign.status not in SCHEDULABLE_FROM:
            raise ValidationError(f"A {campaign.status.value} campaign cannot be scheduled.")

        pending = await self._recipients.pending_count(campaign.id)
        if pending == 0:
            raise ValidationError("This campaign has nobody left to send to.")
        if not (await portfolio_limits.limit_for_account(self._session, campaign.account_id)).known:
            # **Never against a limit nobody has read** (LIM-01): Meta's
            # messaging limit for this number's portfolio could not be read
            # yet. Refused, and read again at the next attempt.
            raise ValidationError(
                "WhatsApp's messaging limit for this number could not be read yet. "
                "Try again in a moment.",
                error_code=portfolio_limits.LIMIT_UNKNOWN_CODE,
            )
        if campaign.header_media_id is not None:
            # A header file retention has removed since composition (HM-07):
            # refused here, rather than scheduled to pause at its first batch.
            header_file = await self._header_files.get_by_id(campaign.header_media_id)
            if header_file is None or not header_file.is_stored:
                raise ValidationError(
                    "This campaign's header file is no longer stored. Compose the campaign "
                    "again with a new one.",
                    error_code=HEADER_MEDIA_NOT_STORED_CODE,
                )

        if self._entitlements is not None:
            # The request's own entitlement service, which carries the
            # deployment's default plan for a workspace on none.
            if not await self._entitlements.channel_in_plan(await self._channel_of(campaign)):
                # A campaign whose every copy would be skipped as
                # `channel_not_in_plan` is refused here, before it reserves
                # anything (CMP-24, ENT-16). The per-batch check stays: a plan
                # can change after scheduling.
                raise PlanLimitExceededError(
                    "The plan in force does not include this campaign's channel.",
                    error_code=CHANNEL_NOT_IN_PLAN_CODE,
                )
            # Checked for the whole audience at once, before a single message
            # goes out. Refusing halfway through a broadcast would leave a
            # workspace having written to some of its customers and not others,
            # which is worse than refusing it outright (ADR-030).
            # Under the workspace's lock for this allowance, counting what
            # other live campaigns have already been promised (BILL-08) - and
            # **both counts are read after the lock is taken** (CMP-03): they
            # are handed over as coroutine functions, not evaluated here.
            # Evaluated as arguments, the read ran before the lock and two
            # campaigns scheduled together both fitted into one remainder.
            await self._entitlements.reserve_period(
                LimitKey.PERIOD_CAMPAIGN_MESSAGES,
                additional=lambda: self._recipients.pending_count(campaign.id),
                reserved=lambda: self._recipients.pending_in_other_live_campaigns(campaign.id),
            )

        if scheduled_at is not None and scheduled_at.utcoffset() is None:
            # Refused rather than taken as UTC (CMP-15): a Cairo admin's 10:00
            # was sent at 13:00 local. There is no workspace time zone to
            # interpret it in (CMQ-04), so the offset has to come with it.
            raise ValidationError(
                "A scheduled time needs a UTC offset, for example 2026-10-20T10:00:00+03:00.",
                error_code=NAIVE_SCHEDULE_CODE,
            )
        now = datetime.now(UTC)
        when = now if scheduled_at is None else scheduled_at
        if when - now > MAX_SCHEDULE_AHEAD:
            raise ValidationError(
                f"A campaign cannot be scheduled more than {MAX_SCHEDULE_AHEAD.days} days ahead."
            )

        campaign.status = CampaignStatus.SCHEDULED
        campaign.scheduled_at = max(when, now)
        campaign.next_send_at = None
        # A fresh schedule is owned by nobody until a sweep claims it.
        campaign.lease_token = None
        campaign.last_error = None
        campaign.status_reason = None
        _clear_wait(campaign)
        # Writing to thousands of people who did not just say something is the
        # single most consequential thing a workspace can do here, so who
        # authorised it and how many it would reach are both recorded.
        self._audit.record(
            AuditAction.CAMPAIGN_SCHEDULED,
            actor=actor,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
            meta={"recipients": pending, "scheduled_at": campaign.scheduled_at.isoformat()},
        )
        logger.info(
            "campaign.scheduled",
            extra={
                "campaign_id": str(campaign.id),
                "scheduled_at": campaign.scheduled_at.isoformat(),
                "pending": pending,
            },
        )
        return campaign

    async def pause(self, campaign_id: uuid.UUID, *, actor: User | None = None) -> Campaign:
        """Stop sending, keep the rest of the list.

        Reversible, which is the whole point: somebody watching a campaign go
        out and having second thoughts needs an action that is not destructive.
        It stops at the next recipient, not after the batch in flight (CMP-08):
        the worker re-reads the campaign before every person.
        """
        campaign = await self._campaigns.require_by_id(campaign_id)
        if campaign.is_finished:
            raise ValidationError(f"A {campaign.status.value} campaign cannot be paused.")
        if campaign.status is CampaignStatus.DRAFT:
            raise ValidationError("A draft is not sending.")

        campaign.status = CampaignStatus.PAUSED
        campaign.status_reason = CampaignStopReason.ADMIN_PAUSED.value
        _clear_wait(campaign)
        # Audited like scheduling and cancelling (CMP-14): stopping a
        # broadcast part-way is a decision somebody is answerable for.
        self._audit.record(
            AuditAction.CAMPAIGN_PAUSED,
            actor=actor,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
            meta={"reason": CampaignStopReason.ADMIN_PAUSED.value},
        )
        logger.info("campaign.paused", extra={"campaign_id": str(campaign.id)})
        await record_campaign_stop(CampaignStopReason.ADMIN_PAUSED.value)
        return campaign

    async def pause_unserved_campaigns(
        self,
        *,
        reason: CampaignStopReason,
        actor: User | None = None,
        actor_kind: AuditActorKind | None = None,
    ) -> int:
        """Pause every scheduled or running campaign of a workspace leaving service.

        Called in the transaction that suspends or deletes the workspace, or
        suspends its subscription (CMP-02, CMQ-11). The worker's own gate,
        asked before every batch and every recipient, is the authoritative
        guard; this is what makes the stop visible as a state with a reason,
        so nobody looking at a suspended workspace sees campaigns waiting to
        go out against its customers.

        **Paused, not failed or cancelled, and never resumed by itself.** A
        restored workspace or a settled invoice leaves these campaigns paused;
        an admin reschedules them, and the reservation is taken again then, in
        the period they will run in. A broadcast arriving weeks later because a
        suspension was lifted is not something anybody decided.
        """
        campaigns = await self._campaigns.lock_live()
        for campaign in campaigns:
            self._pause_for(campaign, reason)
            self._audit.record(
                AuditAction.CAMPAIGN_PAUSED,
                actor=actor,
                actor_kind=actor_kind,
                target_type="campaign",
                target_id=campaign.id,
                target_label=campaign.name,
                meta={"reason": reason.value},
            )
            await record_campaign_stop(reason.value)
        if campaigns:
            logger.info(
                "campaign.paused_on_workspace_change",
                extra={
                    "event": "campaign.paused_on_workspace_change",
                    "tenant_id": str(self._tenant_id),
                    "reason": reason.value,
                    "paused": len(campaigns),
                },
            )
        return len(campaigns)

    async def cancel(self, campaign_id: uuid.UUID, *, actor: User | None = None) -> Campaign:
        """Finish the campaign where it stands. What was sent stays sent.

        A failed campaign can be cancelled, unlike a completed or an already
        cancelled one. Failure is recoverable — the campaign can be rescheduled
        once its cause is fixed — so a workspace that has decided against it
        needs this to be the way it says so.
        """
        campaign = await self._campaigns.require_by_id(campaign_id)
        if campaign.status in ALREADY_OVER:
            return campaign

        campaign.status = CampaignStatus.CANCELLED
        campaign.cancelled_at = datetime.now(UTC)
        _clear_wait(campaign)
        self._audit.record(
            AuditAction.CAMPAIGN_CANCELLED,
            actor=actor,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
        )
        logger.info("campaign.cancelled", extra={"campaign_id": str(campaign.id)})
        return campaign

    # ------------------------------------------------------------------ sending

    async def dispatch_batch(
        self,
        campaign: Campaign,
        *,
        now: datetime | None = None,
        batch_limit: int = DEFAULT_RECIPIENT_BATCH,
        lease: CampaignLease | None = None,
    ) -> BatchOutcome:
        """Send the next batch of one campaign, then set when it may send again.

        The worker calls this and does nothing else. Every compliance question
        is re-asked here rather than trusted from composition time, because a
        campaign scheduled last night is a campaign whose number may have been
        disabled and whose template may have been paused since.

        **Ownership is re-established for every recipient** (CMP-01, ADR-134).
        The batch's row locks end at the first send commit (ADR-093 TX1), so
        before each person this asks two things in two single statements: is
        the campaign still live and still this worker's (`hold`, which also
        renews the lease), and is this recipient still pending and nobody
        else's (`take`, which locks it and re-reads it). Either answering no
        means this worker sends nothing for that person. `lease` is the
        worker's claim; without one - a caller driving a single batch - the
        liveness check still runs and only the token is not compared.
        """
        moment = now or datetime.now(UTC)
        started = perf_counter()

        def clock() -> datetime:
            # The sweep's own clock, advanced by the time this batch has
            # taken: a test's `now` stays meaningful, and a long batch does not
            # date its lease renewals and its pacing from when it began.
            return moment + timedelta(seconds=perf_counter() - started)

        def lease_until() -> datetime | None:
            return clock() + lease.duration if lease is not None else None

        token = lease.token if lease is not None else None

        if campaign.is_finished or campaign.status is CampaignStatus.PAUSED:
            return BatchOutcome(campaign.id, campaign.status)
        if campaign.status is CampaignStatus.DRAFT:
            # Nothing hands a draft to the worker; if one arrives, it is a bug
            # somewhere else and sending would be the wrong way to find out.
            return BatchOutcome(campaign.id, campaign.status)

        unserved = await self._unserved_reason()
        if unserved is not None:
            # **Nothing automated leaves a workspace that is not served**
            # (CMP-02, CMQ-11): suspended by the platform, deleted by its
            # owner, or its subscription suspended for an unpaid invoice.
            # Asked before every batch and again before every recipient, the
            # way cancel is - the lifecycle transitions pause the campaigns
            # they can see, and this is the guard that cannot miss one.
            # Paused with the reason; no send, no attempt spent.
            return await self._pause_unserved(campaign, unserved)

        blocked = await self._blocking_reason(campaign)
        if blocked is not None:
            reason, detail = blocked
            return await self._fail(campaign, detail, reason=reason)

        messaging = self._messaging
        if messaging is None:
            raise RuntimeError("CampaignService needs a messaging service to send.")

        # **Quiet hours** (TZ-03): night in the workspace's own time. The
        # campaign sends nothing and comes back when the window ends - still
        # scheduled or running, nobody's attempt spent, no lease kept. A
        # scheduled campaign is not started by a batch that cannot send.
        quiet = await self._quiet_hours()
        if quiet.contains(moment):
            return await self._wait(
                campaign, CampaignWaitReason.QUIET_HOURS, until=quiet.ends_after(moment)
            )
        if not (await portfolio_limits.limit_for_account(self._session, campaign.account_id)).known:
            # **Fail closed** (LIM-01): nobody has read Meta's messaging limit
            # for this number's portfolio - a number connected before 0110,
            # say, before the deploy step read it. Nothing is sent against an
            # unknown limit; the campaign waits and looks again.
            return await self._wait(
                campaign,
                CampaignWaitReason.LIMIT_UNKNOWN,
                until=moment + LIMIT_UNKNOWN_RECHECK,
            )

        if campaign.status is CampaignStatus.SCHEDULED:
            campaign.status = CampaignStatus.RUNNING
            campaign.started_at = campaign.started_at or moment

        allowance = max(1, min(batch_limit, campaign.messages_per_minute))
        claimed = await self._recipients.claim_pending(campaign.id, limit=allowance, now=moment)
        if not claimed:
            deferred_until = await self._recipients.earliest_deferral(campaign.id)
            if deferred_until is not None:
                # Only people deferred by Meta's pair limit remain (CMP-18):
                # not finished, just waiting for the earliest of them.
                campaign.next_send_at = max(deferred_until, moment)
                campaign.lease_token = None
                _clear_wait(campaign)
                return BatchOutcome(campaign.id, campaign.status)
            return self._complete(campaign, moment)

        # **The header file, once for the campaign** (HM-04): uploaded - or
        # reused while Meta's id is fresh - before the first recipient is
        # taken, by the worker holding the lease, never per person. Every way
        # it can fail stops or waits the campaign as a whole (HM-06).
        header = _HeaderState()
        # Read for its format and not held: a template row kept alive for the
        # batch would be the one every later send's registry check is handed,
        # and a pause another sender wrote since would not be seen (CMP-19).
        spec = parameter_spec(
            (await self._templates.require_by_id(campaign.template_id)).components
        )
        if spec is not None and spec.header_format in FILE_HEADER_FORMATS:
            prepared = await self._header_file(
                campaign, token=token, lease_until=lease_until, clock=clock, messaging=messaging
            )
            if isinstance(prepared, BatchOutcome):
                return prepared
            header.file = prepared

        sent = failed = skipped = deferred = 0
        reached_provider = False
        throttled_until: datetime | None = None
        # Asked once per batch: the plan in force does not change per person.
        in_plan = await self._channel_in_plan(campaign)
        for recipient in claimed:
            if not await self._campaigns.hold(
                campaign.id, lease_token=token, lease_until=lease_until()
            ):
                # Paused, cancelled, or no longer this worker's, since the
                # previous recipient. A cancel or a pause landing mid-batch
                # stops at the next person rather than after the whole claimed
                # batch (MSG-22, CMP-08); what was already sent stays sent,
                # which is the only honest answer once a message has reached a
                # customer. A worker whose lease was taken over sends nothing
                # more (CMP-01).
                #
                # One conditional statement on the row as it is now, never the
                # snapshot `campaign` was loaded as: `pause()` and `cancel()`
                # commit in other transactions. The lock it takes lasts until
                # the next send intent commits - milliseconds, never across a
                # provider call (ADR-093).
                return await self._stopped(campaign, sent=sent, failed=failed, skipped=skipped)
            unserved = await self._unserved_reason()
            if unserved is not None:
                # Suspended, deleted or unpaid mid-batch: stops at the next
                # person, exactly like a cancel (CMP-02).
                return await self._pause_unserved(
                    campaign, unserved, sent=sent, failed=failed, skipped=skipped
                )
            if quiet.contains(clock()):
                # The window opened while this batch was sending: it stops at
                # the next person, like a pause (TZ-03, CMP-08), and waits.
                return await self._wait(
                    campaign,
                    CampaignWaitReason.QUIET_HOURS,
                    until=quiet.ends_after(clock()),
                    sent=sent,
                    failed=failed,
                    skipped=skipped,
                )
            if await self._recipients.take(recipient) is None:
                # Another worker owns this person right now, or finished them
                # since the batch was claimed (CMP-01). Not this worker's to
                # send, and no attempt is spent.
                logger.info(
                    "campaign.recipient_owned_elsewhere",
                    extra={
                        "event": "campaign.recipient_owned_elsewhere",
                        "campaign_id": str(campaign.id),
                    },
                )
                await record_campaign_event("recipient_owned_elsewhere")
                continue
            if not in_plan:
                # ENT-16: a channel the plan in force does not include sends
                # nothing automated. A policy outcome, never retried.
                self._skip(
                    recipient, CHANNEL_NOT_IN_PLAN_DETAIL, RecipientReason.CHANNEL_NOT_IN_PLAN
                )
                skipped += 1
                continue
            try:
                delivered = await self._deliver_with_header(
                    campaign,
                    recipient,
                    messaging=messaging,
                    now=moment,
                    header=header,
                    token=token,
                    lease_until=lease_until,
                    clock=clock,
                )
            except _BatchEndedError as ended:
                # The header file was refused twice, or could not be uploaded
                # again: the campaign stopped as a whole (HM-06).
                return ended.outcome
            except ProviderUnreachableError:
                # Meta could not be reached at all (CMP-07). Nothing was
                # delivered and nothing about this person caused it, so they
                # are unlinked from the undelivered message, keep every
                # attempt, and the campaign waits. Every other recipient would
                # meet the same outage; spending their attempts on it used to
                # cost a campaign its audience. Paused once the outage has
                # outlasted the bound, so it is not retried silently for ever.
                recipient.message_id = None
                since = campaign.provider_unreachable_since or moment
                campaign.provider_unreachable_since = since
                if clock() - since >= self._provider_unreachable_pause:
                    if not await self._campaigns.hold(
                        campaign.id, lease_token=token, lease_until=lease_until()
                    ):
                        return await self._stopped(
                            campaign, sent=sent, failed=failed, skipped=skipped
                        )
                    return await self._pause_unserved(
                        campaign,
                        CampaignStopReason.PROVIDER_UNREACHABLE,
                        sent=sent,
                        failed=failed,
                        skipped=skipped,
                    )
                throttled_until = clock() + PROVIDER_UNREACHABLE_BACKOFF
                break
            except ConnectionThrottledError as error:
                # The number's shared allowance is spent (ADR-123), or the
                # provider throttled it (OMNI-035). Either way nothing reached
                # the customer - the allowance refuses before staging, and a
                # throttled send is recorded undelivered - so the recipient is
                # unlinked from any such message and stays pending with no
                # attempt spent. The rest of the batch would meet the same
                # answer, so the campaign waits for the window instead.
                recipient.message_id = None
                throttled_until = error.retry_at
                break
            except ChannelPausedError:
                # The channel is paused (OMNI-031). Refused before anything was
                # staged, so the recipient is untouched; the campaign waits.
                throttled_until = moment + PAUSED_RECHECK
                break
            except PortfolioLimitReachedError as reached:
                # **The portfolio's campaign share of Meta's limit is used for
                # this moving 24 hours** (LIM-04). Refused under the
                # portfolio's lock before anything was staged: this person is
                # untouched - no attempt, still pending - and so is everybody
                # after them. The campaign waits until the first person
                # counted leaves the window, and carries on by itself.
                return await self._wait(
                    campaign,
                    CampaignWaitReason.DAILY_LIMIT,
                    until=reached.resumes_at,
                    sent=sent,
                    failed=failed,
                    skipped=skipped,
                )
            except MessagingLimitUnknownError:
                # Nobody has read the limit (LIM-01): nothing is sent.
                return await self._wait(
                    campaign,
                    CampaignWaitReason.LIMIT_UNKNOWN,
                    until=clock() + LIMIT_UNKNOWN_RECHECK,
                    sent=sent,
                    failed=failed,
                    skipped=skipped,
                )
            except _CampaignWideRefusalError as refusal:
                # The template is refused for everybody (CMP-05, CMP-19): the
                # campaign fails once with the reason and Meta's code, and the
                # recipients after this one are never asked.
                if not await self._campaigns.hold(
                    campaign.id, lease_token=token, lease_until=lease_until()
                ):
                    return await self._stopped(campaign, sent=sent, failed=failed, skipped=skipped)
                return await self._fail(campaign, refusal.detail, reason=refusal.reason)
            except (DependencyUnavailableError, ProviderConnectionRefusedError) as error:
                # A credential that is missing, or one Meta refuses. Neither is
                # this recipient's problem and neither is fixable by trying the
                # next one. Left as a per-recipient failure the first would
                # loop forever without ever exhausting anyone's attempts,
                # staging a message row per recipient per sweep on a deployment
                # that cannot send at all; the second would burn the whole
                # audience's attempts one person at a time (MSG-18).
                #
                # The recipient this happened to is left pending, its attempts
                # untouched. Its message is recorded UNDELIVERED - Meta read
                # the request and refused it - so the link is cleared: on
                # resume this person is sent, not abandoned as unconfirmed
                # (CMP-11). The campaign has stopped, so nothing picks it up
                # until somebody restarts it, once the credential is fixed.
                recipient.message_id = None
                if not await self._campaigns.hold(
                    campaign.id, lease_token=token, lease_until=lease_until()
                ):
                    return await self._stopped(campaign, sent=sent, failed=failed, skipped=skipped)
                return await self._fail(
                    campaign,
                    str(error),
                    reason=(
                        CampaignStopReason.CONNECTION_REFUSED
                        if isinstance(error, ProviderConnectionRefusedError)
                        and not isinstance(error, ProviderAuthError)
                        else CampaignStopReason.CREDENTIAL_REFUSED
                    ),
                )
            if delivered is None:
                # Re-taken after the header file was uploaded again, and owned
                # by somebody else by then: not this worker's to send.
                continue
            outcome = delivered
            if isinstance(outcome, _Deferred):
                deferred += 1
                reached_provider = True
            elif outcome is RecipientStatus.SENT:
                sent += 1
                reached_provider = True
            elif outcome is RecipientStatus.SKIPPED:
                skipped += 1
            else:
                failed += 1
                reached_provider = True
            await record_campaign_send(
                "deferred" if isinstance(outcome, _Deferred) else _send_outcome(recipient, outcome)
            )

        if not await self._campaigns.hold(campaign.id, lease_token=token, lease_until=None):
            # Paused, cancelled or taken over while the last recipient was
            # in flight: the campaign's state is someone else's to write now.
            # Without this, a `_complete` here overwrote a cancel that landed
            # after the last send.
            return await self._stopped(campaign, sent=sent, failed=failed, skipped=skipped)

        # Rate limiting, stored rather than slept. A sleep would hold the lock
        # and would not survive a restart; a timestamp does both.
        # From when the batch ended, not when the sweep began (CMP-20): a
        # batch that took a minute of slow sends would otherwise have spent
        # its pacing interval before it finished, and the next one followed
        # sooner than the rate allows.
        campaign.next_send_at = clock() + timedelta(
            minutes=len(claimed) / campaign.messages_per_minute
        )
        if throttled_until is not None and throttled_until > campaign.next_send_at:
            # Its own rate, or the connection's window - whichever is later.
            campaign.next_send_at = throttled_until
        # The batch is over, and with it this worker's claim.
        campaign.lease_token = None
        # It sent, or tried: whatever it was waiting for is over.
        _clear_wait(campaign)
        # A batch that ran resets the run of failed ones (CMP-20).
        campaign.consecutive_batch_failures = 0
        if reached_provider:
            # Meta answered: whatever outage there was is over (CMP-07).
            campaign.provider_unreachable_since = None

        # Flushed first: the session does not autoflush, so the last
        # recipient's outcome was not yet in the count and a finished campaign
        # completed only on the next sweep.
        await self._session.flush()
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

    async def _deliver_with_header(
        self,
        campaign: Campaign,
        recipient: CampaignRecipient,
        *,
        messaging: MessagingService,
        now: datetime,
        header: _HeaderState,
        token: uuid.UUID | None,
        lease_until: Callable[[], datetime | None],
        clock: Callable[[], datetime],
    ) -> RecipientStatus | _Deferred | None:
        """`_deliver`, sent again once after a fresh upload if Meta refuses the header file.

        Meta's 131053 ("Unable to upload the media used in the message") is the
        only code it documents for a file a send could not use; it names no
        code for an expired id (Error codes, read 2026-10-15). The first such
        answer re-uploads and re-sends **that recipient only** - Meta refused
        the request, so it provably delivered nothing. A second, for anyone in
        the batch, fails the campaign `header_media_refused`: two requests in
        all for that cause. Returns None when the person was taken by another
        worker between the two sends.
        """
        try:
            return await self._deliver(
                campaign,
                recipient,
                messaging=messaging,
                now=now,
                header_file=header.file,
                at=clock(),
            )
        except _HeaderFileRejectedError:
            pass
        if header.reuploaded:
            raise _BatchEndedError(await self._header_refused(campaign, token=token))
        header.reuploaded = True
        await record_campaign_event("header_media_reupload_on_send")
        prepared = await self._header_file(
            campaign,
            token=token,
            lease_until=lease_until,
            clock=clock,
            messaging=messaging,
            again=True,
        )
        if isinstance(prepared, BatchOutcome):
            raise _BatchEndedError(prepared)
        header.file = prepared
        if not await self._campaigns.hold(
            campaign.id, lease_token=token, lease_until=lease_until()
        ):
            raise _BatchEndedError(await self._stopped(campaign, sent=0, failed=0, skipped=0))
        if await self._recipients.take(recipient) is None:
            return None
        try:
            return await self._deliver(
                campaign,
                recipient,
                messaging=messaging,
                now=now,
                header_file=header.file,
                at=clock(),
            )
        except _HeaderFileRejectedError:
            raise _BatchEndedError(await self._header_refused(campaign, token=token)) from None

    async def _header_file(
        self,
        campaign: Campaign,
        *,
        token: uuid.UUID | None,
        lease_until: Callable[[], datetime | None],
        clock: Callable[[], datetime],
        messaging: MessagingService,
        again: bool = False,
    ) -> HeaderFileParameter | BatchOutcome:
        """Meta's id for the campaign's header file, uploading it if it must (HM-04 .. HM-06).

        Reused while it is fresh: uploaded through the campaign's own number,
        less than `campaign_header_media_refresh_seconds` ago. Otherwise -
        never uploaded, too old, through another number, or `again` because a
        send refused it - uploaded once, by this worker, only while it holds
        the campaign:

            hold        the campaign is live and carries this worker's lease
            --          read the stored file and check its SHA-256; no
                        transaction, no row lock across the store
            --          upload it to Meta through the number's own credential;
                        no transaction across the call (ADR-080, ADR-093)
            write       Meta's id, conditioned on the lease token, in its own
                        short transaction (`record_header_upload`)

        A worker whose lease was taken over in between writes nothing and sends
        nothing: its upload is an unused file at Meta, which expires. A
        `BatchOutcome` means the batch is over - stopped, waiting or failed.
        """
        header_media_id = campaign.header_media_id
        asset = (
            await self._header_files.get_by_id(header_media_id)
            if header_media_id is not None
            else None
        )
        if (
            asset is not None
            and not again
            and campaign.header_provider_media_id is not None
            and campaign.header_provider_account_id == campaign.account_id
            and campaign.header_provider_uploaded_at is not None
            and clock() - campaign.header_provider_uploaded_at < self._header_refresh
        ):
            return _header_parameter(asset, campaign.header_provider_media_id)

        if not await self._campaigns.hold(
            campaign.id, lease_token=token, lease_until=lease_until()
        ):
            return await self._stopped(campaign, sent=0, failed=0, skipped=0)
        if asset is None:
            # Composition requires a file and the key keeps it; a row that
            # still lacks one cannot be sent with its header.
            return await self._header_stop(
                campaign, token=token, pause=CampaignStopReason.HEADER_MEDIA_UNAVAILABLE
            )
        if self._storage is None:  # pragma: no cover - the worker always supplies one
            raise RuntimeError("Sending a campaign's header file needs the media store.")
        loader = CampaignHeaderMediaService(
            session=self._session, tenant_id=self._tenant_id, storage=self._storage
        )
        try:
            # Commits what the batch staged (its RUNNING status) and ends the
            # row lock `hold` took: nothing is held across the store.
            async with released(self._session):
                header_bytes = await loader.load_for_upload(asset)
        except HeaderMediaUnavailableError:
            # Gone, or not what was stored: never sent as something else.
            return await self._header_stop(
                campaign, token=token, pause=CampaignStopReason.HEADER_MEDIA_UNAVAILABLE
            )
        except StorageError:
            # The store cannot say: like reconciliation, that decides nothing.
            return await self._header_wait(
                campaign, token=token, until=clock() + PROVIDER_UNREACHABLE_BACKOFF
            )

        refreshing = campaign.header_provider_media_id is not None
        try:
            provider_media_id = await messaging.upload_header_file(
                connection_id=campaign.account_id,
                content=header_bytes.content,
                mime_type=header_bytes.mime_type,
                filename=_upload_name(asset),
            )
        except ProviderUnreachableError:
            # CMP-07's rule: nothing reached Meta, nothing is spent; the
            # campaign waits, and pauses once the outage outlasts the bound.
            since = campaign.provider_unreachable_since or clock()
            campaign.provider_unreachable_since = since
            if clock() - since >= self._provider_unreachable_pause:
                return await self._header_stop(
                    campaign, token=token, pause=CampaignStopReason.PROVIDER_UNREACHABLE
                )
            return await self._header_wait(
                campaign, token=token, until=clock() + PROVIDER_UNREACHABLE_BACKOFF
            )
        except MediaUploadRefusedError as error:
            # About the file, for everybody: the campaign fails once, with
            # Meta's code. The template is not paused - it is the asset.
            return await self._header_stop(
                campaign,
                token=token,
                fail=CampaignStopReason.HEADER_MEDIA_REFUSED,
                detail=(
                    f"WhatsApp refused the header file (code {error.provider_code})."
                    if error.provider_code is not None
                    else "WhatsApp refused the header file."
                ),
            )
        except (ProviderAuthError, ProviderConnectionRefusedError) as error:
            return await self._header_stop(
                campaign,
                token=token,
                fail=(
                    CampaignStopReason.CREDENTIAL_REFUSED
                    if isinstance(error, ProviderAuthError)
                    else CampaignStopReason.CONNECTION_REFUSED
                ),
                detail=str(error),
            )
        except (DependencyUnavailableError, CredentialDecryptionError) as error:
            # No credential to send as, or one this process cannot read - never
            # the platform's instead (ADR-034).
            return await self._header_stop(
                campaign,
                token=token,
                fail=CampaignStopReason.CREDENTIAL_REFUSED,
                detail=str(error),
            )
        except ValidationError as error:
            # The provider's own limits, or a channel without uploads.
            return await self._header_stop(
                campaign,
                token=token,
                fail=CampaignStopReason.HEADER_MEDIA_REFUSED,
                detail=error.message,
            )
        except (ExternalServiceError, RateLimitedError):
            # Throttled, or an answer that does not settle it - a 5xx, a read
            # timeout, a 2xx without an id. Meta may hold a copy; there is no
            # id to use. Nothing is recorded and the next batch uploads again:
            # a second upload is harmless, a second send is not.
            return await self._header_wait(
                campaign, token=token, until=clock() + PROVIDER_UNREACHABLE_BACKOFF
            )

        uploaded_at = clock()
        recorded = await self._campaigns.record_header_upload(
            campaign.id,
            lease_token=token,
            provider_media_id=provider_media_id,
            uploaded_at=uploaded_at,
            account_id=campaign.account_id,
        )
        # Its own short transaction: the id is durable before anybody is sent it.
        await self._session.commit()
        if not recorded:
            logger.info(
                "campaign.header_media_upload_unused",
                extra={
                    "event": "campaign.header_media_upload_unused",
                    "campaign_id": str(campaign.id),
                },
            )
            return await self._stopped(campaign, sent=0, failed=0, skipped=0)
        # Mirrored on the snapshot without making it dirty: the statement above
        # wrote it, under the lease, and nothing else may.
        set_committed_value(campaign, "header_provider_media_id", provider_media_id)
        set_committed_value(campaign, "header_provider_uploaded_at", uploaded_at)
        set_committed_value(campaign, "header_provider_account_id", campaign.account_id)
        # Meta answered: whatever outage there was is over (CMP-07).
        campaign.provider_unreachable_since = None
        if not again:
            await record_campaign_event(
                "header_media_refreshed" if refreshing else "header_media_uploaded"
            )
        logger.info(
            "campaign.header_media_uploaded",
            extra={
                "event": "campaign.header_media_uploaded",
                "campaign_id": str(campaign.id),
                "kind": asset.kind,
                "refresh": refreshing,
                "again": again,
            },
        )
        return _header_parameter(asset, provider_media_id)

    async def _header_stop(
        self,
        campaign: Campaign,
        *,
        token: uuid.UUID | None,
        pause: CampaignStopReason | None = None,
        fail: CampaignStopReason | None = None,
        detail: str = "",
    ) -> BatchOutcome:
        """Pause or fail the campaign for its header file - only while it is still this worker's."""
        if not await self._campaigns.hold(campaign.id, lease_token=token, lease_until=None):
            return await self._stopped(campaign, sent=0, failed=0, skipped=0)
        if pause is not None:
            return await self._pause_unserved(campaign, pause)
        assert fail is not None  # noqa: S101 - one of the two is always given
        return await self._fail(campaign, detail, reason=fail)

    async def _header_refused(self, campaign: Campaign, *, token: uuid.UUID | None) -> BatchOutcome:
        """Fail the campaign for a file Meta refused twice; its id is not kept.

        Forgotten so that a campaign rescheduled after somebody looked at it
        uploads the file afresh rather than sending the refused id again.
        """
        outcome = await self._header_stop(
            campaign,
            token=token,
            fail=CampaignStopReason.HEADER_MEDIA_REFUSED,
            detail=_HEADER_REFUSED_TWICE,
        )
        if outcome.status is CampaignStatus.FAILED:
            campaign.header_provider_media_id = None
            campaign.header_provider_uploaded_at = None
            campaign.header_provider_account_id = None
        return outcome

    async def _header_wait(
        self, campaign: Campaign, *, token: uuid.UUID | None, until: datetime
    ) -> BatchOutcome:
        """End the batch without a send and come back at `until`; nobody's attempt is spent."""
        if not await self._campaigns.hold(campaign.id, lease_token=token, lease_until=None):
            return await self._stopped(campaign, sent=0, failed=0, skipped=0)
        campaign.next_send_at = until
        campaign.lease_token = None
        return BatchOutcome(campaign.id, campaign.status)

    async def _channel_of(self, campaign: Campaign) -> Channel:
        """The channel this campaign sends on: its connection's."""
        connection = await ChannelConnectionRepository(
            self._session, tenant_id=self._tenant_id
        ).require_by_id(campaign.account_id)
        return connection.channel

    async def _channel_in_plan(self, campaign: Campaign) -> bool:
        """Whether the plan in force includes the channel this campaign sends on (ENT-16)."""
        return await EntitlementService(
            self._session, tenant_id=self._tenant_id, default_plan_code=self._default_plan_code
        ).channel_in_plan(await self._channel_of(campaign))

    async def _blocking_reason(self, campaign: Campaign) -> tuple[CampaignStopReason, str] | None:
        """Why this campaign must not send at all, and the sentence for it, or None.

        Distinct from a recipient failing: these are conditions that will not
        improve by trying the next person, so the campaign stops rather than
        working through its list collecting the same error ten thousand times.
        """
        account = await self._accounts.get_by_id(campaign.account_id)
        if account is None or not account.is_active:
            return (
                CampaignStopReason.CONNECTION_DISABLED,
                "The WhatsApp number this campaign sends from is disabled.",
            )

        template = await self._templates.get_by_id(campaign.template_id)
        if template is None:
            return (
                CampaignStopReason.TEMPLATE_REMOVED,
                "The template this campaign sends has been removed.",
            )
        refusal = refusal_reason_for(template)
        if refusal is not None or not template.is_sendable:
            return (
                # Paused or disabled by Meta is a withdrawal; pending or
                # rejected was never an approval.
                (
                    CampaignStopReason.TEMPLATE_WITHDRAWN
                    if template.status in (TemplateStatus.PAUSED, TemplateStatus.DISABLED)
                    else CampaignStopReason.TEMPLATE_NOT_APPROVED
                ),
                refusal or "WhatsApp has not approved this template.",
            )
        if not template.is_marketing:
            # Re-checked per batch (CMP-09): a sync can recategorise a template
            # after the campaign was composed.
            return (
                CampaignStopReason.TEMPLATE_CATEGORY,
                f"This campaign's template is now {template.category.value}; "
                "a campaign sends marketing templates only.",
            )
        refused = parameter_refusal(
            parameter_spec(template.components),
            CampaignParameters.from_campaign(
                campaign.variables,
                campaign.template_parameters,
                header_file_kind=await self._header_file_kind(campaign),
            ),
        )
        if refused is not None:
            # A sync changed what the template needs since composition: Meta
            # would refuse every recipient for it, so the campaign stops once.
            return (CampaignStopReason.TEMPLATE_PARAMETERS_REFUSED, refused.message)
        return None

    async def _header_file_kind(self, campaign: Campaign) -> str | None:
        """The kind of the campaign's header file, or None if it has none."""
        if campaign.header_media_id is None:
            return None
        header_file = await self._header_files.get_by_id(campaign.header_media_id)
        return header_file.kind if header_file is not None else None

    async def _unserved_reason(self) -> CampaignStopReason | None:
        """Why this workspace is not one Wasla sends for now, or None (CMP-02, CMQ-11).

        The workspace's lifecycle through the one predicate every automated
        sender shares (`workspace_serving`, which the follow-up gate asks too),
        then the subscription: one suspended for an unpaid invoice is service
        withheld pending payment (ADR-061), so its campaigns stop. `past_due`
        is the grace, and keeps sending, as BILLING.md defines it. Two indexed
        scalar reads, against a Graph API call.
        """
        state = await serving.workspace_serving(self._session, self._tenant_id)
        if state is serving.WorkspaceServing.SUSPENDED:
            return CampaignStopReason.WORKSPACE_SUSPENDED
        if state is not serving.WorkspaceServing.SERVED:
            return CampaignStopReason.WORKSPACE_DELETED
        subscription = await self._session.scalar(
            select(Subscription.status).where(Subscription.tenant_id == self._tenant_id)
        )
        if subscription is SubscriptionStatus.SUSPENDED:
            return CampaignStopReason.SUBSCRIPTION_SUSPENDED
        return None

    async def _pause_unserved(
        self,
        campaign: Campaign,
        reason: CampaignStopReason,
        *,
        sent: int = 0,
        failed: int = 0,
        skipped: int = 0,
    ) -> BatchOutcome:
        """Pause a campaign the worker found unserved, and say so in the audit log."""
        self._pause_for(campaign, reason)
        self._audit.record(
            AuditAction.CAMPAIGN_PAUSED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
            meta={"reason": reason.value},
        )
        await record_campaign_stop(reason.value)
        logger.warning(
            "campaign.paused_unserved",
            extra={
                "event": "campaign.paused_unserved",
                "campaign_id": str(campaign.id),
                "reason": reason.value,
            },
        )
        return BatchOutcome(
            campaign.id, CampaignStatus.PAUSED, sent=sent, failed=failed, skipped=skipped
        )

    @staticmethod
    def _pause_for(campaign: Campaign, reason: CampaignStopReason) -> None:
        campaign.status = CampaignStatus.PAUSED
        campaign.status_reason = reason.value
        campaign.next_send_at = None
        campaign.lease_token = None
        _clear_wait(campaign)

    async def _quiet_hours(self) -> QuietHours:
        """This workspace's quiet hours as committed now (TZ-03).

        Read by columns rather than as a `Tenant` object, which the session
        may hold from earlier in its life: a window an admin moved is honoured
        from the next batch on.
        """
        row = (
            await self._session.execute(
                select(
                    Tenant.campaign_quiet_hours_enabled,
                    Tenant.campaign_quiet_hours_start,
                    Tenant.campaign_quiet_hours_end,
                    Tenant.timezone,
                ).where(Tenant.id == self._tenant_id)
            )
        ).one()
        return QuietHours(enabled=row[0], start=row[1], end=row[2], timezone=row[3])

    async def _wait(
        self,
        campaign: Campaign,
        reason: CampaignWaitReason,
        *,
        until: datetime,
        sent: int = 0,
        failed: int = 0,
        skipped: int = 0,
    ) -> BatchOutcome:
        """End the batch without sending, and come back at `until` (TZ-03, LIM-04, FREQ-01).

        **A wait is not a pause.** The status stays `scheduled` or `running`,
        no recipient's state or attempts change, the lease is given up - a
        waiting campaign is nobody's - and `next_send_at` is the moment it is
        due again, which is how the rate limit has always been stored (ADR-026):
        a timestamp, never a sleep. Recorded once per transition into a wait -
        an audit entry by the system and `wasla_campaign_waits_total` - never
        once per batch.
        """
        entering = campaign.waiting_reason != reason.value
        campaign.waiting_reason = reason.value
        campaign.resumes_at = until
        campaign.next_send_at = until
        campaign.lease_token = None
        campaign.consecutive_batch_failures = 0
        if entering:
            self._audit.record(
                AuditAction.CAMPAIGN_WAITING,
                actor=None,
                actor_kind=AuditActorKind.SYSTEM,
                target_type="campaign",
                target_id=campaign.id,
                target_label=campaign.name,
                meta={"reason": reason.value, "resumes_at": until.isoformat()},
            )
            await record_campaign_wait(reason.value)
            logger.info(
                "campaign.waiting",
                extra={
                    "event": "campaign.waiting",
                    "campaign_id": str(campaign.id),
                    "reason": reason.value,
                    "resumes_at": until.isoformat(),
                },
            )
        return BatchOutcome(campaign.id, campaign.status, sent=sent, failed=failed, skipped=skipped)

    async def _deliver(
        self,
        campaign: Campaign,
        recipient: CampaignRecipient,
        *,
        messaging: MessagingService,
        now: datetime,
        header_file: HeaderFileParameter | None = None,
        at: datetime | None = None,
    ) -> RecipientStatus | _Deferred:
        """Send one person's copy and record what happened to it.

        `at` is the worker's clock at this send - what the business-initiated
        ledger dates it with (LIM-03).
        """
        contact = await self._contacts.get_by_id(recipient.contact_id)
        if contact is None:
            return self._skip(
                recipient, "This contact no longer exists.", RecipientReason.CONTACT_GONE
            )
        if not await ContactConsentRepository(
            self._session, tenant_id=self._tenant_id
        ).accepts_marketing(contact.id, await self._channel_of(campaign)):
            # Checked again here, not only when the audience was built. Somebody
            # who opts out while a campaign is running must not receive the rest
            # of it - on the channel this campaign sends on (ENT-19).
            return self._skip(
                recipient, "This contact has opted out of campaigns.", RecipientReason.OPTED_OUT
            )

        if recipient.message_id is not None:
            # **A pending recipient naming a message is one whose send did not
            # resolve** - unless the message itself says it provably did not
            # leave (CMP-11, ADR-140). `_fail_recipient` clears the link when
            # Meta refused one person; a campaign stopped by a refused
            # credential or connection leaves its in-flight recipient linked
            # to a message recorded UNDELIVERED - Meta read the request and
            # declined it, nothing reached the customer - and resuming must
            # send that person, not abandon them (audit P10: 4 of 5).
            #
            # Read from the message's own delivery state, never inferred from
            # the campaign's status. Anything else - REQUESTED above all, a
            # worker that stopped between the send intent and Meta's answer -
            # is unknowable, and is abandoned, never resent (ADR-093).
            if await self._messages.delivery_state(recipient.message_id) is (
                MessageDeliveryState.UNDELIVERED
            ):
                recipient.message_id = None
            else:
                return self._abandon_recipient(
                    recipient,
                    "An earlier attempt could not be confirmed, so it was not sent again.",
                )

        conversation = None
        if recipient.conversation_id is not None:
            # A retry: the conversation the first attempt resolved.
            conversation = await self._conversations.get_by_id(recipient.conversation_id)
        if conversation is None:
            conversation, _ = await self._conversations.get_or_create(
                contact_id=contact.id,
                account_id=campaign.account_id,
            )
            await self._session.flush()
        # **A campaign sends through its own connection, to the identity it was
        # built for, and nowhere else** (OMNI-004). Whatever the recipient row
        # names, a conversation on another number or with another customer is
        # refused rather than sent through.
        if conversation.account_id != campaign.account_id or conversation.contact_id != contact.id:
            return self._skip(
                recipient,
                "This copy names a conversation outside this campaign's number.",
                RecipientReason.OTHER_CONNECTION,
            )
        if recipient.participant_identity_id is None:
            # A recipient materialised before identities existed: record the
            # identity it is going to now, so the row still answers who.
            recipient.participant_identity_id = conversation.participant_identity_id
        elif recipient.participant_identity_id != conversation.participant_identity_id:
            return self._skip(
                recipient,
                "This contact's conversation no longer addresses the identity this "
                "campaign was built for.",
                RecipientReason.IDENTITY_CHANGED,
            )
        recipient.conversation_id = conversation.id

        def link(message: Message) -> None:
            """Name the send on this row, inside the transaction that commits it.

            So a worker that dies between the commit and Meta's answer leaves a
            recipient that says "a message was staged for me" rather than one
            that looks untouched and is sent again.
            """
            recipient.message_id = message.id

        template = await self._templates.require_by_id(campaign.template_id)
        spec = parameter_spec(template.components)
        if spec is None:  # pragma: no cover - `_blocking_reason` refused it first
            raise _CampaignWideRefusalError(
                CampaignStopReason.TEMPLATE_PARAMETERS_REFUSED,
                "This template's components are not known yet.",
            )
        try:
            message = await messaging.send_template(
                conversation_id=conversation.id,
                name=template.name,
                language=template.language,
                # Every component the template's spec needs - header, body,
                # buttons - in Meta's shape (CMP-05). The body alone was sent
                # before, and Meta refused every recipient for the rest.
                components=send_components(
                    spec,
                    CampaignParameters.from_campaign(
                        campaign.variables,
                        campaign.template_parameters,
                        # The one id the campaign's upload returned, the same
                        # for everybody (HM-04).
                        header_file=header_file,
                    ),
                ),
                sent_by_id=campaign.created_by_id,
                link=link,
                # Recorded as a campaign rather than inferred from
                # `sent_by_id`, which names the person who *set the campaign
                # up* and made every broadcast read as their own reply
                # (MSG-16).
                origin=MessageOrigin.CAMPAIGN,
                # Reserved in the business-initiated ledger against the
                # portfolio's campaign share, at the worker's clock (LIM-04).
                at=at or now,
                campaign_id=campaign.id,
            )
        except ProviderConnectionRefusedError:
            # Let out rather than filed against this recipient. The credential
            # - or the connection itself (OMNI-035) - is refused for the whole
            # number, so working through the audience would spend one attempt
            # budget per person discovering the same dead token and end with no
            # single thing to tell anybody (MSG-18). `dispatch_batch` fails the
            # campaign once instead.
            raise
        except (
            ConnectionThrottledError,
            ChannelPausedError,
            ProviderUnreachableError,
            PortfolioLimitReachedError,
            MessagingLimitUnknownError,
        ):
            # Not this recipient's failure: `dispatch_batch` waits for the
            # window, for the outage to pass (CMP-07), or for the portfolio's
            # moving 24 hours to free a place (LIM-04).
            raise
        except TemplateRefusedError as error:
            # The registry refuses the template - withdrawn by Meta through
            # another send since this batch began. Nothing was staged and this
            # recipient spends nothing; every later one would meet the same
            # refusal, so the campaign stops here (CMP-19).
            raise _CampaignWideRefusalError(
                CampaignStopReason.TEMPLATE_WITHDRAWN, error.message
            ) from error
        except (ExternalServiceError, RateLimitedError, ValidationError) as error:
            return self._fail_recipient(recipient, str(error))

        if message.delivery_uncertain:
            # Meta did not answer and there is no way to ask what it did with
            # the request. Terminal: a broadcast that retries an unconfirmed
            # send is a broadcast that sends some people two copies.
            return self._abandon_recipient(
                recipient,
                "WhatsApp did not confirm this message, so it was not sent again.",
            )

        if message.status is MessageStatus.FAILED:
            # The messaging service records a rejected send rather than raising,
            # so a refusal arrives as a row state - with Meta's code on it.
            code = message.failure_code
            if code == HEADER_MEDIA_REJECTED_CODE and header_file is not None:
                # **About the header file, not this person** (HM-06): Meta no
                # longer knows the id, or cannot use the file. Meta read the
                # request and refused it - nothing was delivered - so the
                # person keeps every attempt and is sent again once, after a
                # fresh upload; a second such answer fails the campaign.
                recipient.message_id = None
                raise _HeaderFileRejectedError()
            if code in TEMPLATE_PARAMETER_CODES or code in TEMPLATE_WITHDRAWN_CODES:
                # **A fact about the template, not this person** (CMP-05,
                # CMP-19). Meta refuses the parameters (132000, 132012,
                # 131008) or the template itself (132001, 132015, 132016)
                # for everybody, so the one asked spends its attempt and the
                # campaign stops: one request, not one per recipient. A
                # parameter refusal does not pause the template, which still
                # works with the right parameters (M25).
                self._fail_recipient(recipient, message.failure_reason or "Rejected by WhatsApp.")
                raise _CampaignWideRefusalError(
                    (
                        CampaignStopReason.TEMPLATE_PARAMETERS_REFUSED
                        if code in TEMPLATE_PARAMETER_CODES
                        else CampaignStopReason.TEMPLATE_WITHDRAWN
                    ),
                    (
                        f"WhatsApp refused this template's parameters (code {code})."
                        if code in TEMPLATE_PARAMETER_CODES
                        else f"WhatsApp refused this template (code {code})."
                    ),
                )
            if code == MARKETING_OPTED_OUT_CODE:
                # The person stopped marketing messages in WhatsApp; their
                # consent on this channel already says so (OMNI-046). Never
                # retried.
                recipient.attempts += 1
                recipient.message_id = None
                return self._skip(
                    recipient,
                    message.failure_reason or "The customer stopped marketing messages.",
                    RecipientReason.RECIPIENT_OPTED_OUT_AT_PROVIDER,
                )
            if code is not None and code in TERMINAL_PER_PERSON_CODES:
                # **Meta says asking again soon only fails again** (CMP-06):
                # 131049, its per-user marketing limit - "wait at least 24
                # hours" - and 131026, undeliverable to this person. The
                # person fails at once; the campaign goes on.
                recipient.attempts += 1
                recipient.message_id = None
                recipient.status = RecipientStatus.FAILED
                recipient.reason_code = TERMINAL_PER_PERSON_CODES[code]
                recipient.last_error = (message.failure_reason or "Rejected by WhatsApp.")[
                    :MAX_ERROR_LENGTH
                ]
                return RecipientStatus.FAILED
            if code == PAIR_RATE_LIMIT_CODE:
                # Meta's limit on this number and this person (131056, CMP-18):
                # other people are not affected. This one waits, keeping every
                # attempt; the batch goes on to the next.
                recipient.message_id = None
                recipient.not_before = now + PAIR_RATE_LIMIT_DEFERRAL
                return _DEFERRED
            return self._fail_recipient(
                recipient, message.failure_reason or "Rejected by WhatsApp."
            )

        recipient.status = RecipientStatus.SENT
        recipient.sent_at = now
        recipient.message_id = message.id
        recipient.last_error = None
        recipient.reason_code = None
        # A campaign message is metered twice on purpose, and the two figures
        # answer different questions. The messaging service already counted it
        # as a message sent, which is what a messaging allowance is spent from;
        # this counts it as campaign traffic, which is what a workspace looks at
        # when a broadcast costs more than it expected. What makes the pair
        # safe from double counting is the per-recipient claim (`take`), not
        # the row's state alone: with two workers the row once read `sent`
        # once while two copies - and two meters - had gone (CMP-01). The
        # claim lets one worker send this person; the row moves to SENT in
        # this same transaction and is never claimed again.
        self._usage.record(
            UsageEventType.CAMPAIGN_MESSAGE,
            occurred_at=now,
            meta={"campaign_id": str(campaign.id), "message_id": str(message.id)},
        )
        return RecipientStatus.SENT

    def _skip(
        self, recipient: CampaignRecipient, detail: str, reason: RecipientReason
    ) -> RecipientStatus:
        """A policy outcome, never retried: the reason will not change by itself."""
        recipient.status = RecipientStatus.SKIPPED
        recipient.last_error = detail[:MAX_ERROR_LENGTH]
        recipient.reason_code = reason.value
        return RecipientStatus.SKIPPED

    def _abandon_recipient(self, recipient: CampaignRecipient, detail: str) -> RecipientStatus:
        """A send whose outcome nobody can determine. Terminal, never retried.

        Kept apart from `_fail_recipient`, which is for attempts that provably
        delivered nothing and are worth another go, and from `_skip`, which is a
        policy refusal. This one records that a message may already be on
        somebody's phone - `recipient.message_id` names it - and that no second
        one was sent (ADR-093).
        """
        recipient.attempts += 1
        recipient.status = RecipientStatus.FAILED
        recipient.last_error = detail[:MAX_ERROR_LENGTH]
        recipient.reason_code = RecipientReason.DELIVERY_UNCONFIRMED.value
        return RecipientStatus.FAILED

    def _fail_recipient(self, recipient: CampaignRecipient, detail: str) -> RecipientStatus:
        """An attempt that broke. Retried until the attempts run out."""
        recipient.attempts += 1
        recipient.last_error = detail[:MAX_ERROR_LENGTH]
        # The message this attempt staged, if it staged one, is a finished
        # undelivered send. Unlinking it keeps the invariant `_deliver` reads: a
        # pending recipient naming a message is one whose send nobody resolved.
        recipient.message_id = None
        if recipient.is_exhausted:
            recipient.status = RecipientStatus.FAILED
            recipient.reason_code = RecipientReason.ATTEMPTS_EXHAUSTED.value
            return RecipientStatus.FAILED
        # Left pending, so the next batch picks it up. No backoff of its own:
        # the campaign's own rate limit already spaces the attempts out.
        return RecipientStatus.PENDING

    async def _stopped(
        self, campaign: Campaign, *, sent: int, failed: int, skipped: int
    ) -> BatchOutcome:
        """A batch that ended because the campaign is no longer this worker's to send.

        Nothing is written to the campaign: whoever paused, cancelled or took
        it over owns its state now. The recipients this batch already settled
        are flushed with the session as usual.
        """
        status = await self._campaigns.current_status(campaign.id)
        logger.info(
            "campaign.batch_stopped",
            extra={
                "event": "campaign.batch_stopped",
                "campaign_id": str(campaign.id),
                "status": status.value if status is not None else None,
                "sent": sent,
            },
        )
        if status in (CampaignStatus.SCHEDULED, CampaignStatus.RUNNING):
            # Still live, so the claim itself was lost (CMP-01).
            await record_campaign_event("lease_lost")
        return BatchOutcome(
            campaign_id=campaign.id,
            status=status or campaign.status,
            sent=sent,
            failed=failed,
            skipped=skipped,
        )

    def _complete(self, campaign: Campaign, now: datetime) -> BatchOutcome:
        campaign.status = CampaignStatus.COMPLETED
        campaign.completed_at = now
        campaign.next_send_at = None
        campaign.lease_token = None
        _clear_wait(campaign)
        logger.info("campaign.completed", extra={"campaign_id": str(campaign.id)})
        return BatchOutcome(campaign.id, CampaignStatus.COMPLETED)

    async def _fail(
        self, campaign: Campaign, detail: str, *, reason: CampaignStopReason
    ) -> BatchOutcome:
        campaign.status = CampaignStatus.FAILED
        campaign.last_error = detail[:MAX_ERROR_LENGTH]
        campaign.status_reason = reason.value
        campaign.next_send_at = None
        campaign.lease_token = None
        _clear_wait(campaign)
        logger.warning(
            "campaign.blocked",
            extra={"campaign_id": str(campaign.id), "detail": detail, "reason": reason.value},
        )
        await record_campaign_stop(reason.value)
        return BatchOutcome(campaign.id, CampaignStatus.FAILED)

    # ------------------------------------------------------------------ opt-out

    async def set_opt_out(
        self,
        *,
        contact_id: uuid.UUID,
        channel: Channel,
        source: OptOutSource,
        at: datetime | None = None,
    ) -> Contact:
        """Record that this person wants no marketing on `channel` (ENT-19).

        Idempotent, and it never moves the timestamp forward. The first refusal
        is the one that matters: a second "stop" from someone already opted out
        must not make it look as though they only just decided.
        """
        contact = await self._contacts.require_by_id(contact_id)
        # The same writer every other route uses (OMNI-030): recorded by a
        # colleague, whoever they say decided.
        await record_opt_out(
            self._session,
            tenant_id=self._tenant_id,
            contact_id=contact.id,
            channel=channel,
            source=source,
            via=OptOutVia.TEAM,
            at=at or datetime.now(UTC),
        )
        return contact

    async def opt_out_view(
        self, contact_id: uuid.UUID
    ) -> tuple[list[ContactIdentity], list[ContactChannelConsent]]:
        """A contact's identities and its consent on each channel (ENT-19).

        An opt-out covers the identities of its own channel - a WhatsApp phone
        and a WhatsApp business-scoped id alike - and none of another's.
        """
        identities = await ContactIdentityRepository(
            self._session, tenant_id=self._tenant_id
        ).list_for_contact(contact_id)
        consents = await ContactConsentRepository(
            self._session, tenant_id=self._tenant_id
        ).for_contact(contact_id)
        return identities, consents

    async def clear_opt_out(self, contact_id: uuid.UUID, *, channel: Channel) -> Contact:
        """Let this person receive marketing on `channel` again (ENT-19).

        A person's own decision to stop is not one a workspace should undo
        lightly, and this exists mostly for the case a colleague recorded it in
        error. What it cannot do is be triggered by anything automatic: nothing
        on the inbound path calls it, so a customer writing back after opting
        out does not silently re-enrol. The re-admission is recorded, so a
        replay of older opt-out evidence cannot undo it (OMNI-030).
        """
        contact = await self._contacts.require_by_id(contact_id)
        await record_resume(
            self._session,
            tenant_id=self._tenant_id,
            contact_id=contact.id,
            channel=channel,
            source=OptOutSource.TEAM,
            via=OptOutVia.TEAM,
            at=datetime.now(UTC),
        )
        return contact


class _Deferred:
    """A recipient deferred by a per-person limit: neither sent nor failed (CMP-18)."""


_DEFERRED: Final = _Deferred()

# Meta's codes that are final for one person and say nothing about anybody
# else (CMP-06): 131049, the per-user marketing limit ("wait at least 24 hours
# before resending ... will only result in another error"), and 131026, a
# message undeliverable to this person. Error-codes page, read 2026-10-13
# (EXT-CMP-03). Re-asking within the campaign is never worth an attempt.
TERMINAL_PER_PERSON_CODES: Final[dict[int, str]] = {
    131049: RecipientReason.MARKETING_LIMIT_PER_USER.value,
    131026: RecipientReason.UNDELIVERABLE.value,
}

# How long a person deferred by Meta's pair rate limit (131056) waits before
# the campaign asks again. Meta gives no figure beyond "wait and retry"; the
# pair limit is about one message every few seconds, so a minute is generous
# and costs the rest of the audience nothing (CMP-18).
PAIR_RATE_LIMIT_DEFERRAL: Final = timedelta(minutes=1)

# How long a campaign waits after Meta could not be reached at all (CMP-07),
# and how long an outage may last before it is paused for a person to look at.
PROVIDER_UNREACHABLE_BACKOFF: Final = timedelta(minutes=1)
# Meta's "Unable to upload the media used in the message" (Error codes, read
# 2026-10-15): the only code it documents for a send that could not use its
# media. It names none for an expired id; this is the one acted on (HM-06).
HEADER_MEDIA_REJECTED_CODE: Final = 131053
# Meta: "Media IDs returned by the API expire after 30 days" - re-uploaded
# five days before (HM-05; `campaign_header_media_refresh_seconds`).
DEFAULT_HEADER_MEDIA_REFRESH: Final = timedelta(days=25)
DEFAULT_PROVIDER_UNREACHABLE_PAUSE: Final = timedelta(hours=6)
# How often a campaign whose number's messaging limit was never read looks
# again (LIM-01). The deploy step reads every connection's limit; this is what
# a campaign does until it has.
LIMIT_UNKNOWN_RECHECK: Final = timedelta(minutes=5)


def _clear_wait(campaign: Campaign) -> None:
    """A campaign that sends, or leaves the live statuses, is not waiting for anything."""
    campaign.waiting_reason = None
    campaign.resumes_at = None


def _send_outcome(recipient: CampaignRecipient, outcome: RecipientStatus) -> str:
    """The `wasla_campaign_sends_total` outcome of one recipient's send."""
    if recipient.reason_code == RecipientReason.DELIVERY_UNCONFIRMED.value:
        return "abandoned"
    return {
        RecipientStatus.SENT: "sent",
        RecipientStatus.SKIPPED: "skipped",
    }.get(outcome, "failed")


class _CampaignWideRefusalError(Exception):
    """A refusal that is about the campaign's template, raised out of one recipient's send.

    `dispatch_batch` fails the campaign once with its reason, rather than
    letting every remaining recipient discover it (CMP-05, CMP-19).
    """

    def __init__(self, reason: CampaignStopReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class _HeaderFileRejectedError(Exception):
    """Meta refused a send for the header file it carried (131053, HM-06)."""


class _BatchEndedError(Exception):
    """The batch is over because of the header file; `outcome` says how."""

    def __init__(self, outcome: BatchOutcome) -> None:
        super().__init__(outcome.status.value)
        self.outcome = outcome


@dataclass(slots=True)
class _HeaderState:
    """One batch's header file: what is being sent, and whether it was re-uploaded."""

    file: HeaderFileParameter | None = None
    reuploaded: bool = False


_HEADER_REFUSED_TWICE: Final = (
    "WhatsApp refused the header file twice (code 131053), once after uploading it again."
)


def _upload_name(asset: CampaignHeaderMedia) -> str:
    """The Meta-safe name the file is uploaded and sent with, as an attachment's (MEDIA-06)."""
    return safe_upload_filename(asset.filename, mime_type=asset.mime_type)


def _header_parameter(asset: CampaignHeaderMedia, provider_media_id: str) -> HeaderFileParameter:
    return HeaderFileParameter(
        kind=asset.kind,
        provider_media_id=provider_media_id,
        # Meta shows a document's name: the one its upload used.
        filename=_upload_name(asset) if asset.kind == HeaderMediaKind.DOCUMENT.value else None,
    )


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


__all__ = [
    "MAX_AUDIENCE_SIZE",
    "MAX_SCHEDULE_AHEAD",
    "BatchOutcome",
    "CampaignService",
]
