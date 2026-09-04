"""Campaigns against real PostgreSQL.

Everything that makes a campaign safe is a database property, so almost nothing
here can be checked without one.

The audience rule is a join: only contacts with an existing conversation on the
sending number, minus anyone opted out. `SKIP LOCKED` is what stops two replicas
sending the same person the same message. And the one-row-per-person guarantee
is a unique constraint, not a check in a service.

Meta is replaced by a stub. What is under test is which branch the compliance
logic takes and what it writes down, not whether httpx works.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DependencyUnavailableError,
    ExternalServiceError,
    TenantIsolationError,
    ValidationError,
)
from app.db.models.campaign import (
    MAX_RECIPIENT_ATTEMPTS,
    Campaign,
    CampaignStatus,
    OptOutSource,
)
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDeliveryState,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.lead import Lead, LeadSource, LeadStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)
from app.repositories.campaign_repository import (
    AudienceFilter,
    CampaignRecipientRepository,
)
from app.schemas.campaign import CampaignRead
from app.services.campaign_service import CampaignService
from tests.fakes import as_messaging

pytestmark = pytest.mark.integration


class StubMessaging:
    """Stands in for MessagingService, writing the message row a real send would."""

    def __init__(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, outcome: str = "sent"
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self.outcome = outcome
        self.sends: list[tuple[str, str]] = []

    async def send_template(
        self,
        *,
        conversation_id: uuid.UUID,
        name: str,
        language: str,
        components: Sequence[Any] | None = None,
        sent_by_id: uuid.UUID | None = None,
        link: Callable[[Message], None] | None = None,
    ) -> Message:
        """The real protocol's shape, minus the commits (ADR-093).

        `link` is honoured rather than ignored: the real service calls it
        inside the transaction that commits the send intent, and a stub that
        dropped it would let the recipient row look untouched in exactly the
        state the guard exists for.

        `"uncertain"` is Meta not answering: the row stays `PENDING` and
        `REQUESTED`, which is what a read timeout leaves behind.
        """
        if self.outcome == "raise":
            raise ExternalServiceError("The network went away.")
        self.sends.append((name, language))
        uncertain = self.outcome == "uncertain"
        rejected = self.outcome == "rejected"
        message = Message(
            tenant_id=self._tenant_id,
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            kind=MessageKind.TEMPLATE,
            status=(
                MessageStatus.PENDING
                if uncertain
                else MessageStatus.FAILED
                if rejected
                else MessageStatus.SENT
            ),
            delivery_state=(
                MessageDeliveryState.REQUESTED
                if uncertain
                else MessageDeliveryState.UNDELIVERED
                if rejected
                else MessageDeliveryState.SENT
            ),
            failure_reason="Meta said no." if rejected else None,
            template_name=name,
            template_language=language,
        )
        self._session.add(message)
        await self._session.flush()
        if link is not None:
            link(message)
        return message


async def _tenant(session: AsyncSession, *, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _account(session: AsyncSession, *, tenant: Tenant, suffix: str = "a") -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{tenant.slug}-{suffix}",
        waba_id=f"waba-{tenant.slug}",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()
    return account


async def _template(
    session: AsyncSession,
    *,
    tenant: Tenant,
    account: WhatsAppAccount,
    name: str = "spring_offer",
    status: TemplateStatus = TemplateStatus.APPROVED,
    variables: int = 0,
) -> WhatsAppTemplate:
    template = WhatsAppTemplate(
        tenant_id=tenant.id,
        account_id=account.id,
        name=name,
        language="ar_EG",
        category=TemplateCategory.MARKETING,
        status=status,
        body_text="Hello {{1}}" if variables else "Hello.",
        variable_count=variables,
    )
    session.add(template)
    await session.flush()
    return template


async def _customer(
    session: AsyncSession,
    *,
    tenant: Tenant,
    account: WhatsAppAccount,
    wa_id: str,
    last_inbound_at: datetime | None = None,
    opted_out: bool = False,
    with_conversation: bool = True,
) -> Contact:
    """A contact, and by default the conversation that makes them reachable."""
    contact = Contact(
        tenant_id=tenant.id,
        wa_id=wa_id,
        marketing_opt_out_at=datetime.now(UTC) if opted_out else None,
        opt_out_source=OptOutSource.CUSTOMER if opted_out else None,
    )
    session.add(contact)
    await session.flush()

    if with_conversation:
        session.add(
            Conversation(
                tenant_id=tenant.id,
                contact_id=contact.id,
                account_id=account.id,
                status=ConversationStatus.OPEN,
                mode=ConversationMode.AI,
                last_inbound_at=last_inbound_at or datetime.now(UTC),
            )
        )
        await session.flush()
    return contact


def _service(
    session: AsyncSession,
    tenant: Tenant,
    *,
    messaging: StubMessaging | UnavailableMessaging | None = None,
) -> CampaignService:
    return CampaignService(
        session=session,
        tenant_id=tenant.id,
        messaging=as_messaging(messaging) if messaging is not None else None,
    )


async def _campaign(
    session: AsyncSession,
    *,
    tenant: Tenant,
    account: WhatsAppAccount,
    template: WhatsAppTemplate,
    rate: int = 60,
) -> Campaign:
    campaign = await _service(session, tenant).create(
        account_id=account.id,
        template_id=template.id,
        name="Spring offer",
        messages_per_minute=rate,
    )
    await session.flush()
    return campaign


# ------------------------------------------------------------------ composing


async def test_a_campaign_needs_a_template_whatsapp_approved(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="unapproved")
    account = await _account(db_session, tenant=tenant)
    template = await _template(
        db_session, tenant=tenant, account=account, status=TemplateStatus.PENDING
    )

    with pytest.raises(ValidationError):
        await _service(db_session, tenant).create(
            account_id=account.id,
            template_id=template.id,
            name="Nope",
        )


async def test_a_campaign_cannot_use_another_numbers_template(db_session: AsyncSession) -> None:
    """Meta renders a template from the account that owns it, and no other."""
    tenant = await _tenant(db_session, slug="wrong-number")
    sending = await _account(db_session, tenant=tenant, suffix="sending")
    other = await _account(db_session, tenant=tenant, suffix="other")
    template = await _template(db_session, tenant=tenant, account=other)

    with pytest.raises(ValidationError):
        await _service(db_session, tenant).create(
            account_id=sending.id,
            template_id=template.id,
            name="Nope",
        )


async def test_a_campaign_cannot_send_from_a_disabled_number(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="disabled-number")
    account = await _account(db_session, tenant=tenant)
    account.status = WhatsAppAccountStatus.DISABLED
    template = await _template(db_session, tenant=tenant, account=account)

    with pytest.raises(ValidationError):
        await _service(db_session, tenant).create(
            account_id=account.id,
            template_id=template.id,
            name="Nope",
        )


async def test_the_variables_must_match_what_the_template_expects(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="variables")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account, variables=2)

    with pytest.raises(ValidationError) as raised:
        await _service(db_session, tenant).create(
            account_id=account.id,
            template_id=template.id,
            name="Nope",
            variables=["only one"],
        )

    assert "2 variable" in str(raised.value)


async def test_a_new_campaign_is_a_draft_with_nobody_in_it(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="fresh-draft")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)

    campaign = await _campaign(db_session, tenant=tenant, account=account, template=template)

    assert campaign.status is CampaignStatus.DRAFT
    assert campaign.audience_size == 0
    assert campaign.scheduled_at is None


# ------------------------------------------------------------------ audience


async def test_the_audience_is_people_who_wrote_to_this_business(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="audience-basic")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)
    await _customer(db_session, tenant=tenant, account=account, wa_id="201000000001")
    # A contact with no conversation on this number: never written to us here.
    await _customer(
        db_session,
        tenant=tenant,
        account=account,
        wa_id="201000000002",
        with_conversation=False,
    )
    campaign = await _campaign(db_session, tenant=tenant, account=account, template=template)

    updated = await _service(db_session, tenant).set_audience(
        campaign_id=campaign.id,
        filters=AudienceFilter(),
    )
    await db_session.flush()

    assert updated.audience_size == 1


async def test_somebody_who_opted_out_is_never_in_an_audience(db_session: AsyncSession) -> None:
    """Not a filter a caller can omit: it is part of the base population."""
    tenant = await _tenant(db_session, slug="audience-opt-out")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)
    await _customer(db_session, tenant=tenant, account=account, wa_id="201000000001")
    opted_out = await _customer(
        db_session,
        tenant=tenant,
        account=account,
        wa_id="201000000002",
        opted_out=True,
    )
    campaign = await _campaign(db_session, tenant=tenant, account=account, template=template)

    await _service(db_session, tenant).set_audience(
        campaign_id=campaign.id,
        filters=AudienceFilter(contact_ids=(opted_out.id,)),
    )
    await db_session.flush()

    assert campaign.audience_size == 0


async def test_the_recency_filter_narrows_by_when_they_last_wrote(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="audience-recency")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)
    await _customer(db_session, tenant=tenant, account=account, wa_id="201000000001")
    await _customer(
        db_session,
        tenant=tenant,
        account=account,
        wa_id="201000000002",
        last_inbound_at=datetime.now(UTC) - timedelta(days=90),
    )
    campaign = await _campaign(db_session, tenant=tenant, account=account, template=template)

    await _service(db_session, tenant).set_audience(
        campaign_id=campaign.id,
        filters=AudienceFilter(last_inbound_within_days=30),
    )
    await db_session.flush()

    assert campaign.audience_size == 1


async def test_the_lead_filter_narrows_to_an_opportunity_stage(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="audience-leads")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)
    qualified = await _customer(db_session, tenant=tenant, account=account, wa_id="201000000001")
    await _customer(db_session, tenant=tenant, account=account, wa_id="201000000002")
    db_session.add(
        Lead(
            tenant_id=tenant.id,
            contact_id=qualified.id,
            status=LeadStatus.QUALIFIED,
            source=LeadSource.WHATSAPP,
        )
    )
    await db_session.flush()
    campaign = await _campaign(db_session, tenant=tenant, account=account, template=template)

    await _service(db_session, tenant).set_audience(
        campaign_id=campaign.id,
        filters=AudienceFilter(lead_statuses=(LeadStatus.QUALIFIED,)),
    )
    await db_session.flush()

    assert campaign.audience_size == 1


async def test_setting_the_audience_twice_does_not_duplicate_anyone(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session, slug="audience-twice")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)
    await _customer(db_session, tenant=tenant, account=account, wa_id="201000000001")
    campaign = await _campaign(db_session, tenant=tenant, account=account, template=template)

    service = _service(db_session, tenant)
    await service.set_audience(campaign_id=campaign.id, filters=AudienceFilter())
    await db_session.flush()
    await service.set_audience(campaign_id=campaign.id, filters=AudienceFilter())
    await db_session.flush()

    assert campaign.audience_size == 1
    rows = await CampaignRecipientRepository(db_session, tenant_id=tenant.id).list_for_campaign(
        campaign.id
    )
    assert len(rows) == 1


async def test_the_audience_cannot_be_changed_once_it_is_sending(db_session: AsyncSession) -> None:
    """Rebuilding a part-sent list would duplicate some people and drop others."""
    tenant = await _tenant(db_session, slug="audience-locked")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)
    await _customer(db_session, tenant=tenant, account=account, wa_id="201000000001")
    campaign = await _campaign(db_session, tenant=tenant, account=account, template=template)

    service = _service(db_session, tenant)
    await service.set_audience(campaign_id=campaign.id, filters=AudienceFilter())
    await db_session.flush()
    await service.schedule(campaign_id=campaign.id)
    await db_session.flush()

    with pytest.raises(ValidationError):
        await service.set_audience(campaign_id=campaign.id, filters=AudienceFilter())


async def test_a_preview_counts_without_writing_anything(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="audience-preview")
    account = await _account(db_session, tenant=tenant)
    await _customer(db_session, tenant=tenant, account=account, wa_id="201000000001")
    await _customer(db_session, tenant=tenant, account=account, wa_id="201000000002")

    size = await _service(db_session, tenant).preview_audience(
        account_id=account.id,
        filters=AudienceFilter(),
    )

    assert size == 2


# ------------------------------------------------------------------ lifecycle


async def test_a_campaign_with_nobody_in_it_cannot_be_scheduled(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session, slug="empty-schedule")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)
    campaign = await _campaign(db_session, tenant=tenant, account=account, template=template)

    with pytest.raises(ValidationError):
        await _service(db_session, tenant).schedule(campaign_id=campaign.id)


async def _ready(
    session: AsyncSession, *, slug: str, customers: int = 1, rate: int = 60
) -> tuple[Any, ...]:
    tenant = await _tenant(session, slug=slug)
    account = await _account(session, tenant=tenant)
    template = await _template(session, tenant=tenant, account=account)
    for index in range(customers):
        await _customer(session, tenant=tenant, account=account, wa_id=f"2010000{index:05d}")
    campaign = await _campaign(
        session,
        tenant=tenant,
        account=account,
        template=template,
        rate=rate,
    )
    await _service(session, tenant).set_audience(campaign_id=campaign.id, filters=AudienceFilter())
    await session.flush()
    return tenant, account, template, campaign


async def test_scheduling_hands_the_campaign_to_the_worker(db_session: AsyncSession) -> None:
    tenant, _, _, campaign = await _ready(db_session, slug="schedule-now")

    await _service(db_session, tenant).schedule(campaign_id=campaign.id)

    assert campaign.status is CampaignStatus.SCHEDULED
    assert campaign.scheduled_at is not None


async def test_a_paused_campaign_can_be_scheduled_again(db_session: AsyncSession) -> None:
    """The way back a person hesitating over a half-sent broadcast needs."""
    tenant, _, _, campaign = await _ready(db_session, slug="pause-resume")
    service = _service(db_session, tenant)

    await service.schedule(campaign_id=campaign.id)
    await service.pause(campaign_id=campaign.id)
    assert campaign.status is CampaignStatus.PAUSED

    await service.schedule(campaign_id=campaign.id)
    status: CampaignStatus = campaign.status
    assert status is CampaignStatus.SCHEDULED


async def test_a_cancelled_campaign_is_finished_for_good(db_session: AsyncSession) -> None:
    tenant, _, _, campaign = await _ready(db_session, slug="cancel-final")
    service = _service(db_session, tenant)

    await service.schedule(campaign_id=campaign.id)
    await service.cancel(campaign_id=campaign.id)

    assert campaign.status is CampaignStatus.CANCELLED
    assert campaign.cancelled_at is not None
    with pytest.raises(ValidationError):
        await service.schedule(campaign_id=campaign.id)


async def test_cancelling_twice_changes_nothing(db_session: AsyncSession) -> None:
    """Losing that race is not the caller's mistake."""
    tenant, _, _, campaign = await _ready(db_session, slug="cancel-twice")
    service = _service(db_session, tenant)

    await service.schedule(campaign_id=campaign.id)
    await service.cancel(campaign_id=campaign.id)
    first = campaign.cancelled_at
    assert first is not None
    await service.cancel(campaign_id=campaign.id)

    assert campaign.cancelled_at == first


async def test_a_draft_cannot_be_paused(db_session: AsyncSession) -> None:
    tenant, _, _, campaign = await _ready(db_session, slug="pause-draft")

    with pytest.raises(ValidationError):
        await _service(db_session, tenant).pause(campaign_id=campaign.id)


# -------------------------------------------------------------------- sending


async def test_a_batch_sends_the_template_to_everyone_pending(db_session: AsyncSession) -> None:
    tenant, _, template, campaign = await _ready(db_session, slug="send-all", customers=3)
    messaging = StubMessaging(db_session, tenant_id=tenant.id)
    service = _service(db_session, tenant, messaging=messaging)

    await service.schedule(campaign_id=campaign.id)
    outcome = await service.dispatch_batch(campaign)
    await db_session.flush()

    assert outcome.sent == 3
    assert [name for name, _ in messaging.sends] == [template.name] * 3
    assert campaign.status is CampaignStatus.COMPLETED
    assert campaign.completed_at is not None


async def test_the_rate_limit_is_written_down_rather_than_slept(db_session: AsyncSession) -> None:
    """A sleep would hold the lock and would not survive a restart."""
    tenant, _, _, campaign = await _ready(db_session, slug="rate-limit", customers=4, rate=2)
    service = _service(db_session, tenant, messaging=StubMessaging(db_session, tenant_id=tenant.id))
    moment = datetime.now(UTC)

    await service.schedule(campaign_id=campaign.id)
    outcome = await service.dispatch_batch(campaign, now=moment)
    await db_session.flush()

    # Two per minute, so two go now and the rest wait a minute.
    assert outcome.sent == 2
    assert campaign.status is CampaignStatus.RUNNING
    assert campaign.next_send_at == moment + timedelta(minutes=1)


async def test_the_next_batch_finishes_the_campaign(db_session: AsyncSession) -> None:
    tenant, _, _, campaign = await _ready(db_session, slug="second-batch", customers=4, rate=2)
    service = _service(db_session, tenant, messaging=StubMessaging(db_session, tenant_id=tenant.id))

    await service.schedule(campaign_id=campaign.id)
    await service.dispatch_batch(campaign)
    await db_session.flush()
    await service.dispatch_batch(campaign)
    await db_session.flush()

    assert campaign.status is CampaignStatus.COMPLETED
    statistics = await service.statistics(campaign.id)
    assert (statistics.sent, statistics.pending) == (4, 0)


async def test_somebody_who_opts_out_mid_campaign_is_skipped(db_session: AsyncSession) -> None:
    """Checked again at send time, not only when the audience was built."""
    tenant, account, _, campaign = await _ready(db_session, slug="opt-out-midway", customers=2)
    service = _service(db_session, tenant, messaging=StubMessaging(db_session, tenant_id=tenant.id))
    recipients = await CampaignRecipientRepository(
        db_session, tenant_id=tenant.id
    ).list_for_campaign(campaign.id)
    await service.set_opt_out(
        contact_id=recipients[0].contact_id,
        source=OptOutSource.CUSTOMER,
    )
    await db_session.flush()

    await service.schedule(campaign_id=campaign.id)
    outcome = await service.dispatch_batch(campaign)
    await db_session.flush()

    assert (outcome.sent, outcome.skipped) == (1, 1)
    statistics = await service.statistics(campaign.id)
    assert statistics.skipped == 1


async def test_a_rejected_send_is_retried_until_the_attempts_run_out(
    db_session: AsyncSession,
) -> None:
    tenant, _, _, campaign = await _ready(db_session, slug="rejected", customers=1)
    service = _service(
        db_session,
        tenant,
        messaging=StubMessaging(db_session, tenant_id=tenant.id, outcome="rejected"),
    )

    await service.schedule(campaign_id=campaign.id)
    for _ in range(MAX_RECIPIENT_ATTEMPTS):
        await service.dispatch_batch(campaign)
        await db_session.flush()

    statistics = await service.statistics(campaign.id)
    assert statistics.failed == 1
    assert statistics.pending == 0


async def test_a_campaign_whose_template_was_paused_stops_rather_than_grinding(
    db_session: AsyncSession,
) -> None:
    """The condition will not improve by trying the next person."""
    tenant, _, template, campaign = await _ready(db_session, slug="template-paused", customers=2)
    service = _service(db_session, tenant, messaging=StubMessaging(db_session, tenant_id=tenant.id))
    await service.schedule(campaign_id=campaign.id)
    template.status = TemplateStatus.PAUSED
    await db_session.flush()

    outcome = await service.dispatch_batch(campaign)
    await db_session.flush()

    assert outcome.status is CampaignStatus.FAILED
    assert campaign.last_error is not None and "paused" in campaign.last_error
    statistics = await service.statistics(campaign.id)
    assert statistics.pending == 2


async def test_a_campaign_whose_number_was_disabled_stops(db_session: AsyncSession) -> None:
    tenant, account, _, campaign = await _ready(db_session, slug="number-disabled")
    service = _service(db_session, tenant, messaging=StubMessaging(db_session, tenant_id=tenant.id))
    await service.schedule(campaign_id=campaign.id)
    account.status = WhatsAppAccountStatus.DISABLED
    await db_session.flush()

    outcome = await service.dispatch_batch(campaign)

    assert outcome.status is CampaignStatus.FAILED


async def test_a_paused_campaign_sends_nothing(db_session: AsyncSession) -> None:
    tenant, _, _, campaign = await _ready(db_session, slug="paused-sends-nothing")
    messaging = StubMessaging(db_session, tenant_id=tenant.id)
    service = _service(db_session, tenant, messaging=messaging)

    await service.schedule(campaign_id=campaign.id)
    await service.pause(campaign_id=campaign.id)
    outcome = await service.dispatch_batch(campaign)

    assert outcome.attempted == 0
    assert messaging.sends == []


async def test_a_provider_failure_leaves_the_recipient_for_the_next_batch(
    db_session: AsyncSession,
) -> None:
    tenant, _, _, campaign = await _ready(db_session, slug="provider-down")
    service = _service(
        db_session,
        tenant,
        messaging=StubMessaging(db_session, tenant_id=tenant.id, outcome="raise"),
    )

    await service.schedule(campaign_id=campaign.id)
    outcome = await service.dispatch_batch(campaign)
    await db_session.flush()

    assert outcome.failed == 1
    statistics = await service.statistics(campaign.id)
    assert statistics.pending == 1


# ------------------------------------------------------------------ statistics


async def test_delivery_counts_come_from_the_message_rows(db_session: AsyncSession) -> None:
    """A message Meta accepted is sent; whether it arrived is Meta's news."""
    tenant, _, _, campaign = await _ready(db_session, slug="delivery-counts", customers=2)
    service = _service(db_session, tenant, messaging=StubMessaging(db_session, tenant_id=tenant.id))
    await service.schedule(campaign_id=campaign.id)
    await service.dispatch_batch(campaign)
    await db_session.flush()

    recipients = await CampaignRecipientRepository(
        db_session, tenant_id=tenant.id
    ).list_for_campaign(campaign.id)
    first = await db_session.get(Message, recipients[0].message_id)
    assert first is not None
    first.status = MessageStatus.READ
    await db_session.flush()

    statistics = await service.statistics(campaign.id)
    assert statistics.sent == 2
    # A read message was delivered, so counting `delivered` alone would
    # undercount by everyone who read it.
    assert (statistics.delivered, statistics.read) == (1, 1)


# ------------------------------------------------------------------- isolation


async def test_one_workspace_cannot_see_anothers_campaign(db_session: AsyncSession) -> None:
    mine = await _tenant(db_session, slug="campaign-mine")
    theirs = await _tenant(db_session, slug="campaign-theirs")
    account = await _account(db_session, tenant=theirs)
    template = await _template(db_session, tenant=theirs, account=account)
    campaign = await _campaign(db_session, tenant=theirs, account=account, template=template)

    with pytest.raises(TenantIsolationError):
        await _service(db_session, mine).get(campaign.id)


async def test_an_audience_never_crosses_a_workspace_boundary(db_session: AsyncSession) -> None:
    mine = await _tenant(db_session, slug="audience-mine")
    theirs = await _tenant(db_session, slug="audience-theirs")
    my_account = await _account(db_session, tenant=mine)
    their_account = await _account(db_session, tenant=theirs)
    await _customer(db_session, tenant=theirs, account=their_account, wa_id="201000000001")

    size = await _service(db_session, mine).preview_audience(
        account_id=my_account.id,
        filters=AudienceFilter(),
    )

    assert size == 0


# ------------------------------------------------- conditions outside itself


class UnavailableMessaging:
    """Stands in for a deployment with no WhatsApp credential configured."""

    async def send_template(self, **_: Any) -> None:
        raise DependencyUnavailableError("The WhatsApp access token is not configured.")


async def test_a_missing_credential_stops_the_campaign_rather_than_looping(
    db_session: AsyncSession,
) -> None:
    """Left per-recipient it would retry forever without exhausting anyone.

    The client refuses to be built at all without a token, so no attempt is
    made and nothing increments. A campaign of ten thousand would stage a
    message row per recipient per sweep, on a deployment that cannot send.
    """
    tenant, _, _, campaign = await _ready(db_session, slug="no-credential", customers=2)
    service = _service(db_session, tenant, messaging=UnavailableMessaging())

    await service.schedule(campaign_id=campaign.id)
    outcome = await service.dispatch_batch(campaign)
    await db_session.flush()

    assert outcome.status is CampaignStatus.FAILED
    assert campaign.last_error is not None
    statistics = await service.statistics(campaign.id)
    assert statistics.pending == 2


async def test_a_failed_campaign_can_be_resumed_once_the_cause_is_fixed(
    db_session: AsyncSession,
) -> None:
    """Everything that fails a campaign is something a workspace then fixes."""
    tenant, account, _, campaign = await _ready(db_session, slug="resume-failed", customers=2)
    service = _service(db_session, tenant, messaging=StubMessaging(db_session, tenant_id=tenant.id))
    await service.schedule(campaign_id=campaign.id)
    account.status = WhatsAppAccountStatus.DISABLED
    await db_session.flush()

    await service.dispatch_batch(campaign)
    await db_session.flush()
    assert campaign.status is CampaignStatus.FAILED

    account.status = WhatsAppAccountStatus.ACTIVE
    await db_session.flush()
    await service.schedule(campaign_id=campaign.id)
    outcome = await service.dispatch_batch(campaign)
    await db_session.flush()

    assert outcome.sent == 2
    status: CampaignStatus = campaign.status
    assert status is CampaignStatus.COMPLETED


async def test_resuming_sends_to_nobody_twice(db_session: AsyncSession) -> None:
    """The pending recipients are exactly the ones not yet written to."""
    tenant, _, template, campaign = await _ready(db_session, slug="resume-once", customers=3)
    messaging = StubMessaging(db_session, tenant_id=tenant.id)
    service = _service(db_session, tenant, messaging=messaging)

    await service.schedule(campaign_id=campaign.id)
    await service.dispatch_batch(campaign, batch_limit=2)
    await db_session.flush()
    assert len(messaging.sends) == 2

    campaign.status = CampaignStatus.FAILED
    await db_session.flush()
    await service.schedule(campaign_id=campaign.id)
    await service.dispatch_batch(campaign)
    await db_session.flush()

    assert len(messaging.sends) == 3
    statistics = await service.statistics(campaign.id)
    assert (statistics.sent, statistics.pending) == (3, 0)


async def test_a_new_campaign_can_be_serialised_without_a_further_flush(
    db_session: AsyncSession,
) -> None:
    """The 500 a stubbed endpoint test cannot see.

    A route returns the row the service just staged and the request commits
    afterwards, so anything the database fills in at flush — the primary key
    default, the server-default timestamps — has to exist by the time the
    response is built. Building the response schema here is what the route does.
    """
    tenant = await _tenant(db_session, slug="serialisable")
    account = await _account(db_session, tenant=tenant)
    template = await _template(db_session, tenant=tenant, account=account)

    campaign = await _service(db_session, tenant).create(
        account_id=account.id,
        template_id=template.id,
        name="Spring offer",
    )

    read = CampaignRead.from_model(campaign)
    assert read.id is not None
    assert read.created_at is not None
    assert read.updated_at is not None


async def test_a_failed_campaign_can_be_closed_for_good(db_session: AsyncSession) -> None:
    """Failure is recoverable, so a workspace needs a way to say "not this one"."""
    tenant, account, _, campaign = await _ready(db_session, slug="close-failed")
    service = _service(db_session, tenant, messaging=StubMessaging(db_session, tenant_id=tenant.id))
    await service.schedule(campaign_id=campaign.id)
    account.status = WhatsAppAccountStatus.DISABLED
    await db_session.flush()
    await service.dispatch_batch(campaign)
    await db_session.flush()
    assert campaign.status is CampaignStatus.FAILED

    await service.cancel(campaign_id=campaign.id)

    status: CampaignStatus = campaign.status
    assert status is CampaignStatus.CANCELLED
    assert campaign.cancelled_at is not None
    with pytest.raises(ValidationError):
        await service.schedule(campaign_id=campaign.id)


async def test_a_completed_campaign_cannot_be_cancelled_into_something_else(
    db_session: AsyncSession,
) -> None:
    tenant, _, _, campaign = await _ready(db_session, slug="close-completed")
    service = _service(db_session, tenant, messaging=StubMessaging(db_session, tenant_id=tenant.id))
    await service.schedule(campaign_id=campaign.id)
    await service.dispatch_batch(campaign)
    await db_session.flush()
    assert campaign.status is CampaignStatus.COMPLETED

    await service.cancel(campaign_id=campaign.id)

    assert campaign.status is CampaignStatus.COMPLETED
