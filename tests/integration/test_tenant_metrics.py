"""The reporting queries, against real rows.

These are derived metrics (ADR-028), so the only way to know they are right is
to write the rows a real workspace would have and check the arithmetic. The
definitions that could be read two ways are each pinned by a test: what counts
as a wait, what counts as resolved by the AI, and which side of a window
boundary a row falls on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.analytics import AnalyticsEventType, AnalyticsSource
from app.db.models.campaign import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    RecipientStatus,
)
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.lead import Lead, LeadSource, LeadStatus
from app.db.models.sentiment import MessageSentiment, SentimentLabel
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)
from app.repositories.analytics_repository import AnalyticsEventRepository
from app.repositories.metrics_repository import TenantMetricsRepository
from app.services.analytics_service import AnalyticsService

pytestmark = pytest.mark.integration

SINCE = datetime(2026, 8, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 1, tzinfo=UTC)
NOON = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


async def _tenant(session, slug: str = "acme") -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _account(session, tenant):
    """A number of its own per conversation.

    `whatsapp_accounts` is unique on `phone_number_id` across the platform, and
    the suite runs inside one transaction per test, so the id is randomised
    rather than derived from the slug.
    """
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{tenant.slug}-{uuid.uuid4().hex[:8]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()
    return account


async def _conversation(session, tenant, *, wa_id="201000000001", created_at=NOON):
    account_id = (await _account(session, tenant)).id

    contact = Contact(tenant_id=tenant.id, wa_id=wa_id)
    session.add(contact)
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account_id,
        created_at=created_at,
    )
    session.add(conversation)
    await session.flush()
    return conversation


def _message(tenant, conversation, *, inbound: bool, at: datetime, status=None):
    return Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND if inbound else MessageDirection.OUTBOUND,
        kind=MessageKind.TEXT,
        status=(
            status
            if status is not None
            else (MessageStatus.RECEIVED if inbound else MessageStatus.SENT)
        ),
        body="...",
        created_at=at,
    )


def _metrics(session, tenant) -> TenantMetricsRepository:
    return TenantMetricsRepository(session, tenant_id=tenant.id)


# ------------------------------------------------------------------- messages


async def test_traffic_is_counted_in_both_directions(db_session):
    tenant = await _tenant(db_session)
    conversation = await _conversation(db_session, tenant)
    db_session.add_all(
        [
            _message(tenant, conversation, inbound=True, at=NOON),
            _message(tenant, conversation, inbound=False, at=NOON + timedelta(minutes=1)),
            _message(
                tenant,
                conversation,
                inbound=False,
                at=NOON + timedelta(minutes=2),
                status=MessageStatus.FAILED,
            ),
        ]
    )
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).messages(since=SINCE, until=UNTIL)
    assert metrics.received == 1
    assert metrics.sent == 1
    # A message that never left is not traffic the business sent.
    assert metrics.failed == 1


async def test_a_burst_of_customer_messages_is_one_wait(db_session):
    """Four messages in a row is one customer waiting once.

    Measuring each of them would divide the same wait by four and flatter the
    figure exactly when service is worst.
    """
    tenant = await _tenant(db_session)
    conversation = await _conversation(db_session, tenant)
    db_session.add_all(
        [
            _message(tenant, conversation, inbound=True, at=NOON),
            _message(tenant, conversation, inbound=True, at=NOON + timedelta(seconds=30)),
            _message(tenant, conversation, inbound=True, at=NOON + timedelta(seconds=60)),
            _message(tenant, conversation, inbound=False, at=NOON + timedelta(seconds=120)),
        ]
    )
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).messages(since=SINCE, until=UNTIL)
    assert metrics.average_response_seconds == 120.0
    assert metrics.unanswered == 0


async def test_two_separate_waits_are_averaged(db_session):
    tenant = await _tenant(db_session)
    conversation = await _conversation(db_session, tenant)
    db_session.add_all(
        [
            _message(tenant, conversation, inbound=True, at=NOON),
            _message(tenant, conversation, inbound=False, at=NOON + timedelta(seconds=60)),
            _message(tenant, conversation, inbound=True, at=NOON + timedelta(seconds=120)),
            _message(tenant, conversation, inbound=False, at=NOON + timedelta(seconds=300)),
        ]
    )
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).messages(since=SINCE, until=UNTIL)
    assert metrics.average_response_seconds == 120.0


async def test_a_customer_still_waiting_is_counted_not_averaged(db_session):
    """An unanswered customer has no response time, and reporting one as zero
    would say the opposite of the truth."""
    tenant = await _tenant(db_session)
    conversation = await _conversation(db_session, tenant)
    db_session.add(_message(tenant, conversation, inbound=True, at=NOON))
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).messages(since=SINCE, until=UNTIL)
    assert metrics.average_response_seconds is None
    assert metrics.unanswered == 1


async def test_a_failed_reply_is_not_a_reply(db_session):
    tenant = await _tenant(db_session)
    conversation = await _conversation(db_session, tenant)
    db_session.add_all(
        [
            _message(tenant, conversation, inbound=True, at=NOON),
            _message(
                tenant,
                conversation,
                inbound=False,
                at=NOON + timedelta(seconds=30),
                status=MessageStatus.FAILED,
            ),
        ]
    )
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).messages(since=SINCE, until=UNTIL)
    assert metrics.unanswered == 1
    assert metrics.average_response_seconds is None


# --------------------------------------------------------------- conversations


async def test_a_conversation_nobody_took_over_counts_as_resolved(db_session):
    tenant = await _tenant(db_session)
    await _conversation(db_session, tenant, wa_id="201000000001")
    handed = await _conversation(db_session, tenant, wa_id="201000000002")

    AnalyticsEventRepository(db_session, tenant_id=tenant.id).record(
        event_type=AnalyticsEventType.HANDOFF,
        source=AnalyticsSource.AGENT,
        conversation_id=handed.id,
        occurred_at=NOON,
    )
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).conversations(since=SINCE, until=UNTIL)
    assert metrics.created == 2
    assert metrics.handed_off == 1
    assert metrics.ai_resolved == 1
    assert metrics.ai_resolution_rate == 0.5


async def test_one_conversation_handed_over_twice_is_still_one(db_session):
    """Counting events instead of conversations could drive the rate negative."""
    tenant = await _tenant(db_session)
    conversation = await _conversation(db_session, tenant)
    events = AnalyticsEventRepository(db_session, tenant_id=tenant.id)
    for _ in range(3):
        events.record(
            event_type=AnalyticsEventType.HANDOFF,
            source=AnalyticsSource.USER,
            conversation_id=conversation.id,
            occurred_at=NOON,
        )
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).conversations(since=SINCE, until=UNTIL)
    assert metrics.created == 1
    assert metrics.handed_off == 1
    assert metrics.ai_resolution_rate == 0.0


async def test_a_workspace_with_no_traffic_reports_zero_not_an_error(db_session):
    tenant = await _tenant(db_session)
    metrics = await _metrics(db_session, tenant).conversations(since=SINCE, until=UNTIL)
    assert metrics.created == 0
    assert metrics.ai_resolution_rate == 0.0


# ----------------------------------------------------------------------- leads


async def test_pipeline_counts_come_from_the_leads_themselves(db_session):
    tenant = await _tenant(db_session)
    db_session.add_all(
        [
            Lead(tenant_id=tenant.id, source=LeadSource.AGENT, status=LeadStatus.NEW),
            Lead(tenant_id=tenant.id, source=LeadSource.AGENT, status=LeadStatus.QUALIFIED),
            Lead(tenant_id=tenant.id, source=LeadSource.MANUAL, status=LeadStatus.WON),
            Lead(tenant_id=tenant.id, source=LeadSource.MANUAL, status=LeadStatus.LOST),
        ]
    )
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).leads(since=SINCE, until=UNTIL)
    assert metrics.created == 4
    # Won counts as qualified: it passed through the gate on its way here.
    assert metrics.qualified == 2
    assert metrics.won == 1
    assert metrics.lost == 1
    assert metrics.conversion_rate == 0.25


# ------------------------------------------------------------------- sentiment


async def test_an_unhappy_customer_is_counted_once_however_often_they_complain(db_session):
    tenant = await _tenant(db_session)
    conversation = await _conversation(db_session, tenant)
    message = _message(tenant, conversation, inbound=True, at=NOON)
    db_session.add(message)
    await db_session.flush()

    for label in (SentimentLabel.ANGRY, SentimentLabel.NEGATIVE, SentimentLabel.ANGRY):
        another = _message(tenant, conversation, inbound=True, at=NOON)
        db_session.add(another)
        await db_session.flush()
        db_session.add(
            MessageSentiment(
                tenant_id=tenant.id,
                message_id=another.id,
                conversation_id=conversation.id,
                label=label,
                score=-0.8,
                confidence=0.9,
                escalated=False,
                created_at=NOON,
            )
        )
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).sentiment(since=SINCE, until=UNTIL)
    assert metrics.readings == 3
    assert metrics.unhappy_conversations == 1
    assert metrics.by_label[SentimentLabel.ANGRY] == 2


# ------------------------------------------------------------------- campaigns


async def test_delivery_is_read_from_the_message_not_the_recipient(db_session):
    """A recipient is `sent` when Meta accepts the request. Delivered is a later
    fact that arrives as a status webhook, and conflating them would report a
    delivery rate of one hundred per cent forever."""
    tenant = await _tenant(db_session)
    conversation = await _conversation(db_session, tenant)
    template = WhatsAppTemplate(
        tenant_id=tenant.id,
        account_id=conversation.account_id,
        name="offer",
        language="ar_EG",
        category=TemplateCategory.MARKETING,
        status=TemplateStatus.APPROVED,
    )
    db_session.add(template)
    await db_session.flush()

    second = await _conversation(db_session, tenant, wa_id="201000000099")
    campaign = Campaign(
        tenant_id=tenant.id,
        account_id=conversation.account_id,
        template_id=template.id,
        name="August",
        status=CampaignStatus.COMPLETED,
        audience_size=2,
        messages_per_minute=60,
    )
    db_session.add(campaign)
    delivered = _message(tenant, conversation, inbound=False, at=NOON, status=MessageStatus.READ)
    pending = _message(tenant, conversation, inbound=False, at=NOON, status=MessageStatus.SENT)
    db_session.add_all([delivered, pending])
    await db_session.flush()

    db_session.add_all(
        [
            CampaignRecipient(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                contact_id=conversation.contact_id,
                status=RecipientStatus.SENT,
                message_id=delivered.id,
                created_at=NOON,
            ),
            CampaignRecipient(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                # A second person: one campaign reaches a contact once, and the
                # database enforces it.
                contact_id=second.contact_id,
                status=RecipientStatus.SENT,
                message_id=pending.id,
                created_at=NOON,
            ),
        ]
    )
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).campaigns(since=SINCE, until=UNTIL)
    assert metrics.sent == 2
    assert metrics.delivered == 1


# ------------------------------------------------------------ window and scope


async def test_a_row_on_the_upper_bound_belongs_to_the_next_window(db_session):
    tenant = await _tenant(db_session)
    await _conversation(db_session, tenant, wa_id="201000000001", created_at=SINCE)
    await _conversation(db_session, tenant, wa_id="201000000002", created_at=UNTIL)
    await db_session.flush()

    metrics = await _metrics(db_session, tenant).conversations(since=SINCE, until=UNTIL)
    assert metrics.created == 1


async def test_a_report_never_reaches_another_workspace(db_session):
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    conversation = await _conversation(db_session, acme, wa_id="201000000001")
    db_session.add_all(
        [
            _message(acme, conversation, inbound=True, at=NOON),
            _message(acme, conversation, inbound=False, at=NOON + timedelta(seconds=30)),
            Lead(tenant_id=acme.id, source=LeadSource.AGENT, status=LeadStatus.WON),
        ]
    )
    await db_session.flush()

    report = await AnalyticsService(db_session, tenant_id=rival.id).report(
        since=SINCE,
        until=UNTIL,
    )
    assert report.conversations.created == 0
    assert report.messages.received == 0
    assert report.leads.created == 0
    assert report.messages.average_response_seconds is None

    own = await AnalyticsService(db_session, tenant_id=acme.id).report(since=SINCE, until=UNTIL)
    assert own.conversations.created == 1
    assert own.messages.received == 1
    assert own.leads.won == 1


async def test_the_report_carries_the_window_it_applied(db_session):
    """A figure without its period is not quotable."""
    tenant = await _tenant(db_session)
    report = await AnalyticsService(db_session, tenant_id=tenant.id).report()
    assert report.window.until > report.window.since
    assert (report.window.until - report.window.since) == timedelta(days=30)
