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

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Final

from sqlalchemy import func, select
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
    ConflictError,
    DependencyUnavailableError,
    ExternalServiceError,
    NotFoundError,
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
from app.db import advisory_locks
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
    CampaignTestSend,
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
from app.db.models.enums import TenantRole
from app.db.models.lead import Lead, LeadStatus
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.db.models.user import User
from app.db.models.whatsapp_template import PLACEHOLDER, TemplateStatus
from app.db.session import released
from app.integrations.whatsapp.client import (
    MARKETING_OPTED_OUT_CODE,
    PAIR_RATE_LIMIT_CODE,
    TEMPLATE_WITHDRAWN_CODES,
    MediaUploadRefusedError,
)
from app.repositories.audit_repository import AuditLogRepository
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
from app.services import contact_cap
from app.services import messaging_limit_service as portfolio_limits
from app.services import workspace_serving as serving
from app.services.audit_service import AuditTrail
from app.services.business_initiated_ledger import (
    MessagingLimitUnknownError,
    PortfolioLimitReachedError,
)
from app.services.campaign_activity import ActivityEntry, activity_entry
from app.services.campaign_header_media_service import (
    CampaignHeaderMediaService,
    HeaderMediaUnavailableError,
)
from app.services.campaign_segment_service import CampaignSegmentService, narrowed
from app.services.contact_cap import ContactFrequencyCapError
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
from app.services.personalisation import (
    Mapping,
    ParameterSource,
    PersonFacts,
    Rendered,
    fill,
    normalise,
    resolve,
)
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

# The stable error codes of a composition's per-person values (PER-01, PER-03).
PARAMETERS_CONFLICT_CODE: Final = "parameters_conflict"
PARAMETER_VALUE_REQUIRED_CODE: Final = "parameter_value_required"
PARAMETER_FALLBACK_REQUIRED_CODE: Final = "parameter_fallback_required"
PARAMETER_FALLBACK_TOO_LONG_CODE: Final = "parameter_fallback_too_long"
PARAMETER_KEY_UNKNOWN_CODE: Final = "parameter_key_unknown"
PARAMETER_SOURCE_NOT_ALLOWED_CODE: Final = "parameter_source_not_allowed"

# The test send's stable error codes (PG-09).
CAMPAIGN_NOT_TESTABLE_CODE: Final = "campaign_not_testable"
TEST_CONTACT_NO_CONVERSATION_CODE: Final = "contact_has_no_conversation"
TEST_CONTACT_OPTED_OUT_CODE: Final = "contact_opted_out"
TEST_SEND_LIMIT_CODE: Final = "test_send_limit"
PORTFOLIO_LIMIT_REACHED_CODE: Final = "portfolio_limit_reached"
# The moving window a workspace's test allowance is counted over (PG-09).
TEST_SEND_WINDOW: Final = timedelta(hours=24)

# The draft lifecycle's stable error codes (PG-03, PG-04, PG-08).
CAMPAIGN_NOT_EDITABLE_CODE: Final = "campaign_not_editable"
CAMPAIGN_NOT_DELETABLE_CODE: Final = "campaign_not_deletable"
CAMPAIGN_VERSION_CONFLICT_CODE: Final = "campaign_version_conflict"
CAMPAIGN_VERSION_REQUIRED_CODE: Final = "campaign_version_required"
IDEMPOTENCY_KEY_REUSED_CODE: Final = "idempotency_key_reused"
# How long a create key names its draft (PG-08).
CREATE_KEY_LIFETIME: Final = timedelta(hours=24)
# What a draft edit may change (PG-03), and which of those are its composition.
COMPOSITION_FIELDS: Final = frozenset(
    {
        "account_id",
        "template_id",
        "variables",
        "header_variables",
        "button_parameters",
        "body_mappings",
        "header_mappings",
        "header_media_id",
    }
)
DRAFT_FIELDS: Final = COMPOSITION_FIELDS | {"name", "description", "messages_per_minute"}

# The stable error code for a `scheduled_at` without a UTC offset (CMP-15).
NAIVE_SCHEDULE_CODE: Final = "scheduled_at_needs_offset"
# The stable error codes for a deadline without an offset, or not after the
# start (LIM-06).
SEND_UNTIL_NAIVE_CODE: Final = "send_until_needs_offset"
SEND_UNTIL_BEFORE_START_CODE: Final = "send_until_before_start"

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
        contact_cap_interval: timedelta | None = None,
        parameter_max_chars: int | None = None,
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
        # One marketing message per contact per this long (FREQ-01).
        self._cap_interval = contact_cap_interval or DEFAULT_CONTACT_CAP_INTERVAL
        # The longest one person's value may be before the fallback is sent (PER-03).
        self._parameter_max_chars = parameter_max_chars or DEFAULT_PARAMETER_MAX_CHARS
        self._campaigns = CampaignRepository(session, tenant_id=tenant_id)
        self._recipients = CampaignRecipientRepository(session, tenant_id=tenant_id)
        self._segments = CampaignSegmentService(session, tenant_id=tenant_id)
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
        name_contains: str | None = None,
    ) -> Page[Campaign]:
        after = Cursor.decode(cursor) if cursor else None
        rows = await self._campaigns.list_campaigns(
            statuses=statuses,
            account_id=account_id,
            limit=limit,
            after=after,
            name_contains=name_contains,
        )
        return paginate(
            rows,
            limit=limit,
            key=lambda row: Cursor(sort_value=row.created_at, id=row.id),
        )

    async def activity(
        self, campaign_id: uuid.UUID, *, limit: int = 50, cursor: str | None = None
    ) -> Page[ActivityEntry]:
        """A campaign's activity, newest first (PG-05); another workspace's is not found."""
        campaign = await self._campaigns.require_by_id(campaign_id)
        after = Cursor.decode(cursor) if cursor else None
        rows = await AuditLogRepository(self._session, tenant_id=self._tenant_id).campaign_activity(
            campaign.id,
            limit=limit,
            before=(after.sort_value, after.id) if after and after.sort_value else None,
        )
        # Paged on the trail's rows, then shown: an entry the page does not
        # show never ends a page early.
        page = paginate(rows, limit=limit, key=lambda row: Cursor(row[0].occurred_at, row[0].id))
        shown = [activity_entry(entry, name, campaign.id) for entry, name in page.items]
        return Page(
            items=[entry for entry in shown if entry is not None], next_cursor=page.next_cursor
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
        body_mappings: list[Mapping] | None = None,
        header_mappings: list[Mapping] | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
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

        **Each person's own values** (PER-01): `body_mappings` and
        `header_mappings` name, per placeholder, a constant or a field of the
        contact or their active lead, with a fallback - in place of
        `variables` and `header_variables`. Resolved per person when the
        audience is written.

        **A retried create makes one draft** (PG-08): with `idempotency_key`,
        the same key in this workspace within a day returns the draft it made;
        the key reused for a different request (`request_hash`) is refused 409
        `idempotency_key_reused`.
        """
        if idempotency_key is not None:
            earlier = await self._created_with(idempotency_key, request_hash)
            if earlier is not None:
                return earlier
        composition = await self._composition(
            account_id=account_id,
            template_id=template_id,
            variables=variables,
            header_variables=header_variables,
            button_parameters=button_parameters,
            header_media_id=header_media_id,
            body_mappings=body_mappings,
            header_mappings=header_mappings,
        )

        campaign = self._campaigns.create(
            account_id=account_id,
            template_id=template_id,
            name=_validated_name(name),
            description=description,
            variables=list(composition.parameters.body) or None,
            messages_per_minute=_validated_rate(messages_per_minute),
            created_by_id=created_by_id,
        )
        campaign.template_parameters = composition.stored()
        campaign.header_media_id = header_media_id
        if idempotency_key is not None:
            # The draft and its key in one savepoint: a concurrent retry that
            # holds the key first makes this draft vanish and that one answer.
            savepoint = await self._session.begin_nested()
            await self._session.flush()
            claimed = await self._campaigns.claim_create_key(
                key=idempotency_key,
                campaign_id=campaign.id,
                request_hash=request_hash or "",
                stale_before=datetime.now(UTC) - CREATE_KEY_LIFETIME,
            )
            if not claimed:
                await savepoint.rollback()
                earlier = await self._created_with(idempotency_key, request_hash)
                if earlier is None:  # pragma: no cover - the holder rolled back
                    raise ConflictError(
                        "Another request with this key is still being handled. Try again.",
                        error_code=IDEMPOTENCY_KEY_REUSED_CODE,
                    )
                return earlier
            await savepoint.commit()
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

    async def _composition(
        self,
        *,
        account_id: uuid.UUID,
        template_id: uuid.UUID,
        variables: list[str] | None,
        header_variables: list[str] | None,
        button_parameters: dict[int, str] | None,
        header_media_id: uuid.UUID | None,
        body_mappings: list[Mapping] | None,
        header_mappings: list[Mapping] | None,
    ) -> _Composition:
        """Everything a campaign is composed of, validated - the one validator (PG-03).

        Create and the draft edit both go through here, so a draft can never
        be edited into something create would have refused.
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
        body_sources = await self._validated_mappings(body_mappings, variables, "body")
        header_sources = await self._validated_mappings(header_mappings, header_variables, "header")
        supplied = CampaignParameters(
            body=(
                tuple(mapping.default() for mapping in body_sources)
                if body_sources is not None
                else tuple(variables or ())
            ),
            header=(
                tuple(mapping.default() for mapping in header_sources)
                if header_sources is not None
                else tuple(header_variables or ())
            ),
            buttons=dict(button_parameters or {}),
            header_file_kind=header_file.kind if header_file is not None else None,
        )
        refused = parameter_refusal(parameter_spec(template.components), supplied)
        if refused is not None:
            raise refused
        return _Composition(
            parameters=supplied,
            body_sources=body_sources,
            header_sources=header_sources,
        )

    async def _validated_mappings(
        self, mappings: list[Mapping] | None, constants: list[str] | None, part: str
    ) -> tuple[Mapping, ...] | None:
        """A part's mappings, checked (PER-01, PER-03); None when it has constants only."""
        if mappings is None:
            return None
        if constants:
            raise ValidationError(
                f"Give the {part}'s values either as constants or as mappings, not both.",
                error_code=PARAMETERS_CONFLICT_CODE,
            )
        checked: list[Mapping] = []
        for mapping in mappings:
            if mapping.source is ParameterSource.CONSTANT:
                if mapping.value is None:
                    raise ValidationError(
                        "A constant needs its value.", error_code=PARAMETER_VALUE_REQUIRED_CODE
                    )
                checked.append(Mapping(source=mapping.source, value=mapping.value))
                continue
            fallback = normalise(mapping.fallback)
            if not fallback:
                raise ValidationError(
                    "A value taken from the contact or the lead needs a fallback for people "
                    "without it.",
                    error_code=PARAMETER_FALLBACK_REQUIRED_CODE,
                )
            if len(fallback) > self._parameter_max_chars:
                raise ValidationError(
                    f"A fallback is at most {self._parameter_max_chars} characters.",
                    error_code=PARAMETER_FALLBACK_TOO_LONG_CODE,
                )
            key = None
            if mapping.source is ParameterSource.LEAD_CUSTOM_FIELD:
                key = (mapping.key or "").strip()
                if not key or not await self._custom_field_exists(key):
                    raise ValidationError(
                        "No lead in this workspace has that custom field.",
                        error_code=PARAMETER_KEY_UNKNOWN_CODE,
                    )
            checked.append(Mapping(source=mapping.source, key=key, fallback=fallback))
        return tuple(checked)

    async def _custom_field_exists(self, key: str) -> bool:
        found = await self._session.scalar(
            select(Lead.id)
            .where(Lead.tenant_id == self._tenant_id, Lead.custom_fields.has_key(key))
            .limit(1)
        )
        return found is not None

    async def preview_audience(
        self,
        *,
        account_id: uuid.UUID,
        filters: AudienceFilter,
        segment_id: uuid.UUID | None = None,
    ) -> int:
        """How many people this filter - over a saved segment's rules - would reach.

        Nothing is written.
        """
        await self._accounts.require_by_id(account_id)
        if segment_id is not None:
            filters = narrowed(
                await self._segments.for_audience(segment_id, account_id=account_id), filters
            )
        return await self._audience.count_eligible(account_id=account_id, filters=filters)

    async def set_audience(
        self,
        *,
        campaign_id: uuid.UUID,
        filters: AudienceFilter,
        actor: User | None = None,
        segment_id: uuid.UUID | None = None,
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
        if segment_id is not None:
            # The segment's rules, narrowed by the filters given with it, are
            # copied onto the campaign below (PG-06): a later edit of the
            # segment changes nothing here.
            filters = narrowed(
                await self._segments.for_audience(segment_id, account_id=campaign.account_id),
                filters,
            )
        campaign.audience_segment_id = segment_id
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
        # Each person's own values, resolved now and written down with them
        # (PER-02): what this campaign will send them, whatever changes later.
        renderer = await self._renderer(campaign)
        facts = (
            await self._facts([contact for contact, _ in members if contact.id not in already])
            if renderer is not None
            else {}
        )
        added = 0
        for contact, participant_identity_id in members:
            if contact.id in already:
                continue
            # The exact identity this copy is addressed to (OMNI-004).
            recipient = self._recipients.create(
                campaign_id=campaign.id,
                contact_id=contact.id,
                participant_identity_id=participant_identity_id,
            )
            if renderer is not None:
                recipient.rendered_parameters = renderer(facts[contact.id]).as_dict()
            added += 1

        campaign.audience = filters.as_dict()
        campaign.audience_size = len(already) + added
        # Writing the list is an edit of the draft (PG-03).
        campaign.version += 1
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
            meta={
                "recipients": campaign.audience_size,
                "filters": summary,
                "segment_id": str(segment_id) if segment_id is not None else None,
            },
        )
        logger.info(
            "campaign.audience_set",
            extra={"campaign_id": str(campaign.id), "audience_size": campaign.audience_size},
        )
        return campaign

    async def _renderer(self, campaign: Campaign) -> Callable[[PersonFacts], Rendered] | None:
        """How to resolve one person's values for this campaign; None if nothing is personal."""
        stored = campaign.template_parameters or {}
        body = tuple(Mapping.from_dict(entry) for entry in stored.get("body_sources") or ())
        header = tuple(Mapping.from_dict(entry) for entry in stored.get("header_sources") or ())
        if not any(mapping.personal for mapping in body + header):
            return None
        if not body:
            body = tuple(
                Mapping(ParameterSource.CONSTANT, value=v) for v in campaign.variables or ()
            )
        if not header:
            header = tuple(
                Mapping(ParameterSource.CONSTANT, value=str(v)) for v in stored.get("header") or ()
            )
        template = await self._templates.require_by_id(campaign.template_id)
        spec = parameter_spec(template.components)
        body_names = spec.body if spec is not None else ()
        max_chars = self._parameter_max_chars

        def render(facts: PersonFacts) -> Rendered:
            return resolve(
                body=body,
                header=header,
                facts=facts,
                max_chars=max_chars,
                body_text=template.body_text,
                body_names=body_names,
            )

        return render

    async def _facts(self, contacts: Sequence[Contact]) -> dict[uuid.UUID, PersonFacts]:
        """What each contact and their active lead say, in one read (PER-01)."""
        ids = [contact.id for contact in contacts]
        leads = {
            lead.contact_id: lead
            for lead in (
                await self._session.execute(
                    select(Lead).where(
                        Lead.tenant_id == self._tenant_id,
                        Lead.contact_id.in_(ids),
                        Lead.status.not_in([LeadStatus.WON, LeadStatus.LOST]),
                    )
                )
            ).scalars()
        }
        facts: dict[uuid.UUID, PersonFacts] = {}
        for contact in contacts:
            lead = leads.get(contact.id)
            facts[contact.id] = PersonFacts(
                contact_id=contact.id,
                display_name=contact.display_name,
                lead_name=lead.name if lead is not None else None,
                lead_interest=lead.interest if lead is not None else None,
                lead_status=lead.status.value if lead is not None else None,
                lead_budget_amount=lead.budget_amount if lead is not None else None,
                lead_budget_currency=lead.budget_currency if lead is not None else None,
                lead_custom_fields=lead.custom_fields if lead is not None else None,
            )
        return facts

    async def preview_messages(
        self, campaign_id: uuid.UUID, contact_ids: Sequence[uuid.UUID]
    ) -> list[MessagePreview]:
        """What each of these people would receive (PER-04): theirs, and nobody else's.

        A contact of the campaign's written audience is shown the values
        written for them; before the audience is written, any contact of the
        eligible population is resolved now. Anybody else - another
        workspace's contact, somebody outside the audience - is not found.
        """
        campaign = await self._campaigns.require_by_id(campaign_id)
        template = await self._templates.require_by_id(campaign.template_id)
        spec = parameter_spec(template.components)
        written = await self._recipients.rendered_for(campaign.id, contact_ids)
        has_audience = campaign.audience_size > 0
        renderer = await self._renderer(campaign)
        eligible = (
            {
                contact.id: contact
                for contact, _ in await self._audience.list_eligible_members(
                    account_id=campaign.account_id,
                    filters=AudienceFilter(contact_ids=tuple(contact_ids)),
                    limit=len(contact_ids) + 1,
                )
            }
            if not has_audience
            else {}
        )
        facts = await self._facts(list(eligible.values())) if renderer is not None else {}
        defaults = CampaignParameters.from_campaign(
            campaign.variables, campaign.template_parameters
        )
        previews: list[MessagePreview] = []
        for contact_id in contact_ids:
            if has_audience and contact_id not in written:
                raise NotFoundError("That contact is not in this campaign's audience.")
            if not has_audience and contact_id not in eligible:
                raise NotFoundError("That contact is not in this campaign's audience.")
            stored = written.get(contact_id)
            rendered = (
                Rendered.from_dict(stored)
                if stored
                else (renderer(facts[contact_id]) if renderer is not None else None)
            )
            parameters = _personal(defaults, rendered)
            header_text = _header_text(template.components)
            previews.append(
                MessagePreview(
                    contact_id=contact_id,
                    header=(
                        fill(header_text, spec.header, parameters.header)
                        if spec is not None and header_text
                        else None
                    ),
                    body=fill(template.body_text, spec.body if spec else (), parameters.body),
                    buttons=_button_texts(template.components, parameters.buttons),
                    body_fallbacks=rendered.body_fallbacks if rendered else (),
                    header_fallbacks=rendered.header_fallbacks if rendered else (),
                )
            )
        return previews

    # ---------------------------------------------------------------- drafts

    async def _created_with(self, key: str, request_hash: str | None) -> Campaign | None:
        """The draft this create key already made in the last day, if any (PG-08)."""
        found = await self._campaigns.created_with_key(
            key, since=datetime.now(UTC) - CREATE_KEY_LIFETIME
        )
        if found is None:
            return None
        row, campaign = found
        if row.request_hash != (request_hash or ""):
            raise ConflictError(
                "That idempotency key was already used for a different campaign.",
                error_code=IDEMPOTENCY_KEY_REUSED_CODE,
            )
        return campaign

    def _inputs(self, campaign: Campaign) -> dict[str, Any]:
        """A draft's composition as `_composition` takes it: constants or mappings."""
        stored = campaign.template_parameters or {}
        body_sources = stored.get("body_sources")
        header_sources = stored.get("header_sources")
        return {
            "account_id": campaign.account_id,
            "template_id": campaign.template_id,
            "variables": None if body_sources is not None else campaign.variables,
            "header_variables": (
                None
                if header_sources is not None
                else [str(value) for value in stored.get("header") or []] or None
            ),
            "button_parameters": {
                int(entry["index"]): str(entry["value"])
                for entry in stored.get("buttons") or []
                if isinstance(entry, dict) and "index" in entry and "value" in entry
            },
            "header_media_id": campaign.header_media_id,
            "body_mappings": (
                [Mapping.from_dict(entry) for entry in body_sources]
                if body_sources is not None
                else None
            ),
            "header_mappings": (
                [Mapping.from_dict(entry) for entry in header_sources]
                if header_sources is not None
                else None
            ),
        }

    async def update_draft(
        self,
        campaign_id: uuid.UUID,
        *,
        version: int | None,
        changes: dict[str, Any],
        actor: User | None = None,
    ) -> tuple[Campaign, bool]:
        """Edit a draft (PG-03); returns it and whether its written list was cleared.

        Drafts only, under the row lock, from the version the editor read:
        another edit in between is refused 409 `campaign_version_conflict` with
        the current version. Validated by the one validator `create` uses. A
        change to the number, the template, the values or the header file
        clears the written list - its people's values were resolved for the
        old composition - and keeps its rules; the page writes it again.
        Audited with the names of the changed fields, never their values.
        """
        unknown = set(changes) - DRAFT_FIELDS
        if unknown:  # pragma: no cover - the request schema admits nothing else
            raise ValidationError(f"Not editable: {', '.join(sorted(unknown))}.")
        campaign = await self._campaigns.lock_by_id(campaign_id)
        if campaign.status is not CampaignStatus.DRAFT:
            raise ConflictError(
                "Only a draft can be edited.", error_code=CAMPAIGN_NOT_EDITABLE_CODE
            )
        if version is None:
            raise ValidationError(
                "Say which version you edited: If-Match or `version`.",
                error_code=CAMPAIGN_VERSION_REQUIRED_CODE,
            )
        if version != campaign.version:
            raise ConflictError(
                "Somebody changed this draft since you opened it. Reload it and edit again.",
                error_code=CAMPAIGN_VERSION_CONFLICT_CODE,
                details={"version": campaign.version},
            )

        changed: set[str] = set()
        cleared = False
        if set(changes) & COMPOSITION_FIELDS:
            inputs = self._inputs(campaign)
            # Constants and mappings replace each other: giving one drops the other.
            for constants, mappings in (
                ("variables", "body_mappings"),
                ("header_variables", "header_mappings"),
            ):
                if constants in changes and mappings not in changes:
                    inputs[mappings] = None
                if mappings in changes and constants not in changes:
                    inputs[constants] = None
            for field in COMPOSITION_FIELDS & set(changes):
                inputs[field] = changes[field]
            inputs["button_parameters"] = dict(inputs["button_parameters"] or {})
            composition = await self._composition(**inputs)
            stored = composition.stored()
            body = list(composition.parameters.body) or None
            before = {
                "account_id": campaign.account_id,
                "template_id": campaign.template_id,
                "variables": campaign.variables,
                "template_parameters": campaign.template_parameters,
                "header_media_id": campaign.header_media_id,
            }
            after = {
                "account_id": inputs["account_id"],
                "template_id": inputs["template_id"],
                "variables": body,
                "template_parameters": stored,
                "header_media_id": inputs["header_media_id"],
            }
            changed |= {field for field in before if before[field] != after[field]}
            if changed:
                campaign.account_id = inputs["account_id"]
                campaign.template_id = inputs["template_id"]
                campaign.template_parameters = stored
                campaign.variables = body
                campaign.header_media_id = inputs["header_media_id"]
                if await self._recipients.delete_all(campaign.id) or campaign.audience_size:
                    cleared = True
                campaign.audience_size = 0
        if "name" in changes:
            name = _validated_name(changes["name"])
            if name != campaign.name:
                campaign.name = name
                changed.add("name")
        if "description" in changes and changes["description"] != campaign.description:
            campaign.description = changes["description"]
            changed.add("description")
        if "messages_per_minute" in changes:
            rate = _validated_rate(changes["messages_per_minute"])
            if rate != campaign.messages_per_minute:
                campaign.messages_per_minute = rate
                changed.add("messages_per_minute")
        if not changed:
            return campaign, False

        campaign.version += 1
        await self._session.flush()
        # `updated_at` is the database's: read back, so the response can show it.
        await self._session.refresh(campaign, ["updated_at"])
        self._audit.record(
            AuditAction.CAMPAIGN_UPDATED,
            actor=actor,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
            meta={"fields": sorted(changed), "audience_cleared": cleared},
        )
        logger.info(
            "campaign.updated",
            extra={"campaign_id": str(campaign.id), "fields": sorted(changed)},
        )
        return campaign, cleared

    async def delete_draft(self, campaign_id: uuid.UUID, *, actor: User | None = None) -> None:
        """Delete a draft and its never-sent list (PG-04).

        A scheduled campaign is cancelled first; anything else is refused 409
        `campaign_not_deletable`. A campaign anybody was ever sent to is never
        deletable, whatever its status says. Its header file stays: another
        campaign may use it, and retention decides when it goes. The audit
        entry keeps the id and the name.
        """
        campaign = await self._campaigns.lock_by_id(campaign_id)
        if campaign.status is not CampaignStatus.DRAFT or await self._recipients.ever_sent(
            campaign.id
        ):
            raise ConflictError(
                "Only a draft nobody was sent can be deleted. Cancel a scheduled campaign first.",
                error_code=CAMPAIGN_NOT_DELETABLE_CODE,
            )
        removed = await self._recipients.delete_all(campaign.id)
        self._audit.record(
            AuditAction.CAMPAIGN_DELETED,
            actor=actor,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
            meta={"name": campaign.name, "recipients": removed},
        )
        await self._session.delete(campaign)
        await self._session.flush()
        logger.info("campaign.deleted", extra={"campaign_id": str(campaign_id)})

    async def duplicate(self, campaign_id: uuid.UUID, *, actor: User | None = None) -> Campaign:
        """A new draft composed like this campaign (PG-02).

        Copies the name (" (copy)"), description, number, template, values and
        their mappings, header file, rate and the audience's rules - never the
        written list, the schedule, results, stop reasons, Meta's media id,
        the lease or counters. Validated like a create: a template withdrawn or
        a number disabled since is refused with create's codes.
        """
        source = await self._campaigns.require_by_id(campaign_id)
        inputs = self._inputs(source)
        composition = await self._composition(**inputs)
        copy = self._campaigns.create(
            account_id=source.account_id,
            template_id=source.template_id,
            name=_copy_name(source.name),
            description=source.description,
            variables=list(composition.parameters.body) or None,
            messages_per_minute=source.messages_per_minute,
            created_by_id=actor.id if actor is not None else None,
        )
        copy.template_parameters = composition.stored()
        copy.header_media_id = source.header_media_id
        copy.audience = dict(source.audience) if source.audience is not None else None
        copy.audience_segment_id = source.audience_segment_id
        await self._session.flush()
        self._audit.record(
            AuditAction.CAMPAIGN_DUPLICATED,
            actor=actor,
            target_type="campaign",
            target_id=copy.id,
            target_label=copy.name,
            meta={"source_id": str(source.id)},
        )
        logger.info(
            "campaign.duplicated",
            extra={"campaign_id": str(copy.id), "source_id": str(source.id)},
        )
        await record_campaign_event("duplicated")
        return copy

    # ------------------------------------------------------------- test send

    async def send_test(
        self,
        campaign_id: uuid.UUID,
        *,
        contact_id: uuid.UUID,
        actor: User,
        sender_role: TenantRole,
        messaging: MessagingService,
        storage: MediaStorage,
        per_day: int,
        now: datetime | None = None,
    ) -> Message:
        """Send this campaign's message to one contact - the admin's own (PG-09).

        The same composition a recipient gets: the one component builder, this
        contact's own values (resolved now), the header file. Sent as a
        `campaign_test` message through the same client and send path, but it
        is not a recipient: never in the list or the statistics, not under the
        per-contact cap or quiet hours. Counted in Meta's messaging limit
        like any business-initiated send outside the service window, and
        refused only when the portfolio has nothing left at all.

        ADR-025 holds: the contact must already have a conversation with this
        number - the admin writes to the business first. At most `per_day`
        tests per workspace in a moving 24 hours (429 `test_send_limit`).
        """
        moment = now or datetime.now(UTC)
        campaign = await self._campaigns.require_by_id(campaign_id)
        if campaign.status is CampaignStatus.CANCELLED:
            raise ConflictError(
                "A cancelled campaign cannot be tested.", error_code=CAMPAIGN_NOT_TESTABLE_CODE
            )
        contact = await self._contacts.require_by_id(contact_id)
        conversation = await self._conversations.get_for_contact(
            contact_id=contact.id, account_id=campaign.account_id
        )
        if conversation is None:
            raise ValidationError(
                "Write to this WhatsApp number from that phone first: a test reaches only a "
                "contact who already has a conversation with it.",
                error_code=TEST_CONTACT_NO_CONVERSATION_CODE,
            )
        if not await ContactConsentRepository(
            self._session, tenant_id=self._tenant_id
        ).accepts_marketing(contact.id, conversation.channel):
            raise ValidationError(
                "This contact has opted out of marketing messages.",
                error_code=TEST_CONTACT_OPTED_OUT_CODE,
            )
        template = await self._templates.require_by_id(campaign.template_id)
        spec = parameter_spec(template.components)
        refused = (
            parameter_refusal(
                spec,
                CampaignParameters.from_campaign(
                    campaign.variables,
                    campaign.template_parameters,
                    header_file_kind=await self._header_file_kind(campaign),
                ),
            )
            if spec is not None
            else None
        )
        if spec is None or refused is not None:
            raise refused or ValidationError("This template's components are not known yet.")

        # The header file first, outside any lock (ADR-093): the campaign's
        # own id while it is fresh, otherwise uploaded for this test - and
        # never written onto the campaign, whose upload is the worker's.
        header_file = await self._test_header_file(campaign, messaging=messaging, storage=storage)

        # The allowance and the record as one step.
        await self._session.execute(
            select(
                func.pg_advisory_xact_lock(
                    advisory_locks.CAMPAIGN_TEST_SENDS, self._tenant_id.int & 0x7FFFFFFF
                )
            )
        )
        since = moment - TEST_SEND_WINDOW
        recent = list(
            (
                await self._session.execute(
                    select(CampaignTestSend.created_at)
                    .where(
                        CampaignTestSend.tenant_id == self._tenant_id,
                        CampaignTestSend.created_at > since,
                    )
                    .order_by(CampaignTestSend.created_at, CampaignTestSend.id)
                )
            ).scalars()
        )
        if len(recent) >= per_day:
            reset_at = recent[0] + TEST_SEND_WINDOW
            raise RateLimitedError(
                f"A workspace may send {per_day} campaign tests in 24 hours.",
                error_code=TEST_SEND_LIMIT_CODE,
                details={"reset_at": reset_at.isoformat()},
            )
        record = CampaignTestSend(
            tenant_id=self._tenant_id,
            campaign_id=campaign.id,
            contact_id=contact.id,
            sent_by_id=actor.id,
        )
        self._session.add(record)
        await self._session.flush()

        renderer = await self._renderer(campaign)
        rendered = (
            renderer((await self._facts([contact]))[contact.id]) if renderer is not None else None
        )
        components = send_components(
            spec,
            _personal(
                CampaignParameters.from_campaign(
                    campaign.variables, campaign.template_parameters, header_file=header_file
                ),
                rendered,
            ),
        )
        try:
            message = await messaging.send_template(
                conversation_id=conversation.id,
                name=template.name,
                language=template.language,
                components=components,
                sent_by_id=actor.id,
                origin=MessageOrigin.CAMPAIGN_TEST,
                sender_role=sender_role,
                campaign_id=campaign.id,
            )
        except PortfolioLimitReachedError as error:
            raise ConflictError(
                "This business has reached its WhatsApp messaging limit for now.",
                error_code=PORTFOLIO_LIMIT_REACHED_CODE,
                details={"available_at": error.resumes_at.isoformat()},
            ) from error
        except MessagingLimitUnknownError as error:
            raise ValidationError(
                "This number's WhatsApp messaging limit has not been read yet.",
                error_code=portfolio_limits.LIMIT_UNKNOWN_CODE,
            ) from error
        record.message_id = message.id
        self._audit.record(
            AuditAction.CAMPAIGN_TEST_SENT,
            actor=actor,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
            meta={"contact_id": str(contact.id)},
        )
        logger.info(
            "campaign.test_sent",
            extra={"campaign_id": str(campaign.id), "message_id": str(message.id)},
        )
        await record_campaign_event("test_sent")
        return message

    async def _test_header_file(
        self, campaign: Campaign, *, messaging: MessagingService, storage: MediaStorage
    ) -> HeaderFileParameter | None:
        if campaign.header_media_id is None:
            return None
        asset = await self._header_files.get_by_id(campaign.header_media_id)
        if asset is None:
            raise ValidationError(
                "This campaign's header file is gone.", error_code=HEADER_MEDIA_NOT_STORED_CODE
            )
        now = datetime.now(UTC)
        if (
            campaign.header_provider_media_id is not None
            and campaign.header_provider_account_id == campaign.account_id
            and campaign.header_provider_uploaded_at is not None
            and now - campaign.header_provider_uploaded_at < self._header_refresh
        ):
            return _header_parameter(asset, campaign.header_provider_media_id)
        loader = CampaignHeaderMediaService(
            session=self._session, tenant_id=self._tenant_id, storage=storage
        )
        try:
            header_bytes = await loader.load_for_upload(asset)
        except HeaderMediaUnavailableError as error:
            raise ValidationError(
                "This campaign's header file is not stored. Upload it again.",
                error_code=HEADER_MEDIA_NOT_STORED_CODE,
            ) from error
        provider_media_id = await messaging.upload_header_file(
            connection_id=campaign.account_id,
            content=header_bytes.content,
            mime_type=header_bytes.mime_type,
            filename=_upload_name(asset),
        )
        return _header_parameter(asset, provider_media_id)

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
        send_until: datetime | None = None,
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
        # A paused or failed campaign scheduled again is resumed (PG-05).
        resumed = campaign.status is not CampaignStatus.DRAFT

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
        if send_until is not None:
            # A deadline, like a start, is an instant: it needs its offset, and
            # it comes after the start it ends (LIM-06).
            if send_until.utcoffset() is None:
                raise ValidationError(
                    "A deadline needs a UTC offset, for example 2026-10-23T23:59:00+03:00.",
                    error_code=SEND_UNTIL_NAIVE_CODE,
                )
            if send_until <= max(when, now):
                raise ValidationError(
                    "The deadline must come after the campaign starts.",
                    error_code=SEND_UNTIL_BEFORE_START_CODE,
                )

        campaign.status = CampaignStatus.SCHEDULED
        campaign.scheduled_at = max(when, now)
        campaign.send_until = send_until
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
            meta={
                "recipients": pending,
                "scheduled_at": campaign.scheduled_at.isoformat(),
                "send_until": send_until.isoformat() if send_until is not None else None,
                "resumed": resumed,
            },
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

        if campaign.send_until is not None and moment >= campaign.send_until:
            # **The deadline has passed** (LIM-06): whoever is still pending is
            # not going to be sent; the campaign completes.
            return await self._close_at_deadline(campaign, moment)

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
                if await self._recipients.capped_pending(
                    campaign.id, since=moment - self._cap_interval
                ):
                    # Everybody left was sent a marketing message in the last
                    # 24 hours (FREQ-01): the campaign waits for the first.
                    return await self._wait(
                        campaign, CampaignWaitReason.CONTACT_CAP, until=max(deferred_until, moment)
                    )
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
            if campaign.send_until is not None and clock() >= campaign.send_until:
                # The deadline passed while this batch was sending (LIM-06).
                closed = await self._close_at_deadline(campaign, clock())
                return BatchOutcome(
                    campaign.id,
                    closed.status,
                    sent=sent,
                    failed=failed,
                    skipped=skipped + closed.skipped,
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
        if campaign.send_until is not None and campaign.send_until < until:
            # Woken at the deadline to close, not after it (LIM-06).
            campaign.next_send_at = campaign.send_until
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

        rendered = (
            Rendered.from_dict(recipient.rendered_parameters)
            if recipient.rendered_parameters
            else None
        )
        if rendered is not None and rendered.contact_id != recipient.contact_id:
            # Values resolved for somebody else are never sent (CR-15): this
            # person is left out rather than sent a stranger's name.
            return self._skip(
                recipient,
                "This copy's values were not written for this contact.",
                RecipientReason.IDENTITY_CHANGED,
            )
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
                    _personal(
                        CampaignParameters.from_campaign(
                            campaign.variables,
                            campaign.template_parameters,
                            # The one id the campaign's upload returned, the
                            # same for everybody (HM-04).
                            header_file=header_file,
                        ),
                        rendered,
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
        except ContactFrequencyCapError as capped:
            # **One marketing message per person per 24 hours** (FREQ-01):
            # another campaign, or a colleague, reached this person less than
            # a day ago. Refused by the claim before anything was staged, so
            # no attempt is spent: the person waits for their moment, and the
            # campaign goes on with the others - unless the deadline comes
            # first, when they are skipped.
            until = capped.available_at + CONTACT_CAP_MARGIN
            if campaign.send_until is not None and until >= campaign.send_until:
                return self._skip(recipient, _CONTACT_CAPPED, RecipientReason.CONTACT_FREQUENCY_CAP)
            recipient.not_before = until
            return _DEFERRED
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
                if code == MARKETING_LIMIT_PER_USER_CODE:
                    # Meta: "wait at least 24 hours before resending" - so
                    # this workspace sends the person no marketing for a day,
                    # whatever campaign asks next (LIM-08, FREQ-01).
                    await contact_cap.push(
                        self._session, tenant_id=self._tenant_id, contact_id=contact.id, at=now
                    )
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

    async def _close_at_deadline(self, campaign: Campaign, now: datetime) -> BatchOutcome:
        """Skip everybody still pending and complete the campaign (LIM-06)."""
        skipped = await self._recipients.skip_pending(
            campaign.id,
            reason=RecipientReason.SEND_WINDOW_CLOSED.value,
            detail=_DEADLINE_PASSED,
        )
        logger.info(
            "campaign.deadline_passed",
            extra={
                "event": "campaign.deadline_passed",
                "campaign_id": str(campaign.id),
                "skipped": skipped,
            },
        )
        outcome = self._complete(campaign, now)
        return BatchOutcome(campaign.id, outcome.status, skipped=skipped)

    def _complete(self, campaign: Campaign, now: datetime) -> BatchOutcome:
        campaign.status = CampaignStatus.COMPLETED
        campaign.completed_at = now
        campaign.next_send_at = None
        campaign.lease_token = None
        _clear_wait(campaign)
        # The system's own end of a campaign, once (PG-05).
        self._audit.record(
            AuditAction.CAMPAIGN_COMPLETED,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
        )
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
        self._audit.record(
            AuditAction.CAMPAIGN_FAILED,
            target_type="campaign",
            target_id=campaign.id,
            target_label=campaign.name,
            meta={"reason": reason.value},
        )
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
# One marketing message per contact per 24 hours (FREQ-01), and the margin a
# capped person waits beyond their moment, so they are never asked a second early.
DEFAULT_CONTACT_CAP_INTERVAL: Final = timedelta(hours=24)
# `CAMPAIGN_PARAMETER_MAX_CHARS` (PER-03).
DEFAULT_PARAMETER_MAX_CHARS: Final = 60
CONTACT_CAP_MARGIN: Final = timedelta(minutes=5)
# Meta's per-user marketing limit (131049).
MARKETING_LIMIT_PER_USER_CODE: Final = 131049
# How often a campaign whose number's messaging limit was never read looks
# again (LIM-01). The deploy step reads every connection's limit; this is what
# a campaign does until it has.
LIMIT_UNKNOWN_RECHECK: Final = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class _Composition:
    """A validated composition: the send's parameters and the mappings behind them."""

    parameters: CampaignParameters
    body_sources: tuple[Mapping, ...] | None = None
    header_sources: tuple[Mapping, ...] | None = None

    def stored(self) -> dict[str, Any] | None:
        stored = self.parameters.as_dict() or {}
        if self.body_sources is not None:
            stored["body_sources"] = [mapping.as_dict() for mapping in self.body_sources]
        if self.header_sources is not None:
            stored["header_sources"] = [mapping.as_dict() for mapping in self.header_sources]
        if self.body_sources is not None or self.header_sources is not None:
            stored.setdefault("header", list(self.parameters.header))
            stored.setdefault("buttons", [])
        return stored or None


@dataclass(frozen=True, slots=True)
class MessagePreview:
    """What one person would receive (PER-04)."""

    contact_id: uuid.UUID
    header: str | None
    body: str
    buttons: tuple[str, ...]
    body_fallbacks: tuple[int, ...]
    header_fallbacks: tuple[int, ...]


def _personal(defaults: CampaignParameters, rendered: Rendered | None) -> CampaignParameters:
    """The campaign's parameters with one person's own values, when there are any (PER-02)."""
    if rendered is None:
        return defaults
    return CampaignParameters(
        body=rendered.body or defaults.body,
        header=rendered.header or defaults.header,
        buttons=defaults.buttons,
        header_file_kind=defaults.header_file_kind,
        header_file=defaults.header_file,
    )


def _header_text(components: Any) -> str | None:
    for component in components if isinstance(components, list) else ():
        if (
            isinstance(component, dict)
            and str(component.get("type", "")).upper() == "HEADER"
            and str(component.get("format", "TEXT")).upper() == "TEXT"
        ):
            text = component.get("text")
            return text if isinstance(text, str) else None
    return None


def _literal(value: str) -> Callable[[re.Match[str]], str]:
    def replace(_match: re.Match[str]) -> str:
        return value

    return replace


def _button_texts(components: Any, values: dict[int, str]) -> tuple[str, ...]:
    """Each button's label, a URL button's link with its suffix filled in."""
    found: list[str] = []
    for component in components if isinstance(components, list) else ():
        if not isinstance(component, dict) or str(component.get("type", "")).upper() != "BUTTONS":
            continue
        for index, button in enumerate(component.get("buttons") or []):
            if not isinstance(button, dict):
                continue
            label = str(button.get("text", ""))
            url = button.get("url")
            if isinstance(url, str) and index in values:
                # A function, not a replacement string: a backslash in the
                # value is text, not a group reference.
                url = PLACEHOLDER.sub(_literal(values[index]), url, count=1)
            found.append(f"{label} ({url})" if isinstance(url, str) else label)
    return tuple(found)


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


_DEADLINE_PASSED: Final = "The campaign's deadline passed before this person was sent."
_CONTACT_CAPPED: Final = (
    "This customer was sent a marketing message in the last 24 hours, "
    "and the campaign's deadline came first."
)

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


def _copy_name(name: str) -> str:
    """A duplicate's name: the original's with " (copy)", within the column (PG-02)."""
    suffix = " (copy)"
    return name[: MAX_CAMPAIGN_NAME_LENGTH - len(suffix)].rstrip() + suffix


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
