"""The campaign invariants CR-01..CR-10 count what they claim to (non-vacuity).

`scripts/omnichannel_invariants.py` carries the checks the campaigns audit ran
by hand after its probes (section 30). Each test here writes exactly one
violation of one invariant inside the test's rolled-back transaction and
asserts that invariant - and only it, where the shapes allow - counts one more.
A query made vacuous (mutant M-C38) fails its test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.db.models.campaign_media import CampaignHeaderMedia
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDeliveryState,
    MessageKind,
    MessageOrigin,
)
from app.db.models.enums import TenantStatus
from app.db.models.media import MediaStorageState
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.db.models.whatsapp_template import TemplateCategory, WhatsAppTemplate
from app.repositories.conversation_repository import MessageRepository
from app.services.usage_service import UsageRecorder
from scripts.omnichannel_invariants import campaign_violations
from tests.consent import seed_opt_out
from tests.integration.test_campaigns import _account, _campaign, _customer, _template, _tenant

pytestmark = pytest.mark.integration


async def _counts(session: AsyncSession) -> dict[str, int]:
    await session.flush()
    return await campaign_violations(await session.connection(), read_only=False)


async def _added(session: AsyncSession, before: dict[str, int]) -> dict[str, int]:
    after = await _counts(session)
    return {name: after[name] - before[name] for name in after if after[name] != before[name]}


async def _world(
    session: AsyncSession, slug: str
) -> tuple[Campaign, CampaignRecipient, Conversation]:
    tenant = await _tenant(session, slug=slug)
    account = await _account(session, tenant=tenant)
    template = await _template(session, tenant=tenant, account=account)
    contact = await _customer(
        session, tenant=tenant, account=account, wa_id=f"20100{abs(hash(slug)) % 10**7:07d}"
    )
    campaign = await _campaign(session, tenant=tenant, account=account, template=template)
    recipient = CampaignRecipient(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        contact_id=contact.id,
        status=RecipientStatus.PENDING,
        attempts=0,
    )
    session.add(recipient)
    await session.flush()
    conversation = await session.scalar(
        select(Conversation).where(Conversation.contact_id == contact.id)
    )
    assert conversation is not None
    return campaign, recipient, conversation


async def _message(
    session: AsyncSession, conversation: Conversation, state: MessageDeliveryState
) -> Message:
    message = await MessageRepository(session, tenant_id=conversation.tenant_id).stage_outbound(
        conversation_id=conversation.id,
        kind=MessageKind.TEMPLATE,
        body=None,
        template_name="spring_offer",
        template_language="ar_EG",
        origin=MessageOrigin.CAMPAIGN,
    )
    await session.flush()
    message.delivery_state = state
    return message


def _meter(session: AsyncSession, campaign: Campaign, message_id: object) -> None:
    UsageRecorder(session, tenant_id=campaign.tenant_id).record(
        UsageEventType.CAMPAIGN_MESSAGE,
        occurred_at=datetime.now(UTC),
        meta={"campaign_id": str(campaign.id), "message_id": str(message_id)},
    )


async def test_cr01_two_messages_of_one_campaign_in_one_conversation(
    db_session: AsyncSession,
) -> None:
    campaign, recipient, conversation = await _world(db_session, "cr01")
    first = await _message(db_session, conversation, MessageDeliveryState.SENT)
    second = await _message(db_session, conversation, MessageDeliveryState.SENT)
    recipient.status = RecipientStatus.SENT
    recipient.sent_at = datetime.now(UTC)
    _meter(db_session, campaign, first.id)
    before = await _counts(db_session)

    _meter(db_session, campaign, second.id)

    added = await _added(db_session, before)
    assert added["cr01_conversation_with_two_messages_of_one_campaign"] == 1


async def test_cr02_meters_differ_from_recipients_sent(db_session: AsyncSession) -> None:
    campaign, _, conversation = await _world(db_session, "cr02")
    message = await _message(db_session, conversation, MessageDeliveryState.SENT)
    before = await _counts(db_session)

    _meter(db_session, campaign, message.id)

    assert await _added(db_session, before) == {
        "cr02_campaign_meters_differ_from_recipients_sent": 1
    }


async def test_cr03_pending_recipient_linked_to_an_undelivered_message(
    db_session: AsyncSession,
) -> None:
    _, recipient, conversation = await _world(db_session, "cr03")
    message = await _message(db_session, conversation, MessageDeliveryState.UNDELIVERED)
    before = await _counts(db_session)

    recipient.message_id = message.id

    assert await _added(db_session, before) == {
        "cr03_pending_recipient_linked_to_an_undelivered_message": 1
    }


async def test_cr04_recipient_added_after_scheduling(db_session: AsyncSession) -> None:
    campaign, _, _ = await _world(db_session, "cr04")
    before = await _counts(db_session)

    db_session.add(
        AuditLog(
            tenant_id=campaign.tenant_id,
            action=AuditAction.CAMPAIGN_SCHEDULED,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="campaign",
            target_id=campaign.id,
            occurred_at=datetime.now(UTC) - timedelta(days=1),
        )
    )

    assert await _added(db_session, before) == {
        "cr04_recipient_added_after_its_campaign_was_scheduled": 1
    }


async def test_cr05_live_campaign_of_a_suspended_workspace(db_session: AsyncSession) -> None:
    campaign, _, _ = await _world(db_session, "cr05")
    campaign.status = CampaignStatus.SCHEDULED
    before = await _counts(db_session)

    workspace = await db_session.get(Tenant, campaign.tenant_id)
    assert workspace is not None
    workspace.status = TenantStatus.SUSPENDED

    assert await _added(db_session, before) == {"cr05_live_campaign_of_a_workspace_not_served": 1}


async def test_cr06_live_campaign_on_a_utility_template(db_session: AsyncSession) -> None:
    campaign, _, _ = await _world(db_session, "cr06")
    campaign.status = CampaignStatus.RUNNING
    before = await _counts(db_session)

    template = await db_session.get(WhatsAppTemplate, campaign.template_id)
    assert template is not None
    template.category = TemplateCategory.UTILITY

    assert await _added(db_session, before) == {
        "cr06_live_campaign_on_a_template_that_is_not_marketing": 1
    }


async def test_cr07_recipient_sent_after_opting_out(db_session: AsyncSession) -> None:
    campaign, recipient, _ = await _world(db_session, "cr07")
    await seed_opt_out(
        db_session,
        tenant_id=campaign.tenant_id,
        contact_id=recipient.contact_id,
        at=datetime.now(UTC) - timedelta(hours=1),
    )
    before = await _counts(db_session)

    recipient.status = RecipientStatus.SENT
    recipient.sent_at = datetime.now(UTC)

    # A sent recipient with no meter also moves CR-02; this is about CR-07.
    added = await _added(db_session, before)
    assert added["cr07_recipient_sent_after_opting_out_on_that_channel"] == 1


def _header_file(
    campaign: Campaign, *, tenant_id: uuid.UUID | None = None, kind: str = "image"
) -> CampaignHeaderMedia:
    return CampaignHeaderMedia(
        tenant_id=tenant_id or campaign.tenant_id,
        kind=kind,
        mime_type="image/png" if kind == "image" else "application/pdf",
        byte_size=10,
        content_hash="0" * 64,
        storage_key=f"{campaign.tenant_id}/2026/10/{campaign.id}{kind}.bin",
        storage_state=MediaStorageState.STORED,
    )


async def _image_header(session: AsyncSession, campaign: Campaign) -> None:
    template = await session.get(WhatsAppTemplate, campaign.template_id)
    assert template is not None
    template.components = [
        {"type": "HEADER", "format": "IMAGE"},
        {"type": "BODY", "text": "Spring sale."},
    ]


async def test_cr08_live_image_header_campaign_without_its_file(db_session: AsyncSession) -> None:
    campaign, _, _ = await _world(db_session, "cr08")
    await _image_header(db_session, campaign)
    image = _header_file(campaign)
    db_session.add(image)
    await db_session.flush()
    campaign.header_media_id = image.id
    campaign.status = CampaignStatus.SCHEDULED
    before = await _counts(db_session)

    campaign.header_media_id = None

    assert await _added(db_session, before) == {
        "cr08_live_media_header_campaign_without_a_matching_file": 1
    }


async def test_cr08_a_file_of_another_kind_counts_too(db_session: AsyncSession) -> None:
    campaign, _, _ = await _world(db_session, "cr08k")
    await _image_header(db_session, campaign)
    image, document = _header_file(campaign), _header_file(campaign, kind="document")
    db_session.add_all([image, document])
    await db_session.flush()
    campaign.header_media_id = image.id
    campaign.status = CampaignStatus.PAUSED
    before = await _counts(db_session)

    campaign.header_media_id = document.id

    assert await _added(db_session, before) == {
        "cr08_live_media_header_campaign_without_a_matching_file": 1
    }


async def test_cr09_header_uploaded_through_another_number(db_session: AsyncSession) -> None:
    campaign, _, _ = await _world(db_session, "cr09")
    tenant = await db_session.get(Tenant, campaign.tenant_id)
    assert tenant is not None
    other = await _account(db_session, tenant=tenant, suffix="b")
    image = _header_file(campaign)
    db_session.add(image)
    await db_session.flush()
    campaign.header_media_id = image.id
    campaign.header_provider_media_id = "1234567890"
    campaign.header_provider_uploaded_at = datetime.now(UTC)
    campaign.header_provider_account_id = campaign.account_id
    before = await _counts(db_session)

    campaign.header_provider_account_id = other.id

    assert await _added(db_session, before) == {
        "cr09_header_media_uploaded_through_another_number": 1
    }


async def test_cr10_campaign_naming_another_workspaces_file(db_session: AsyncSession) -> None:
    campaign, _, _ = await _world(db_session, "cr10")
    stranger = await _tenant(db_session, slug="cr10-other")
    theirs = _header_file(campaign, tenant_id=stranger.id)
    db_session.add(theirs)
    await db_session.flush()
    before = await _counts(db_session)

    # The composite key refuses this write; the invariant is the second line,
    # so the key's trigger is set aside for this rolled-back transaction only.
    await db_session.execute(text("SET LOCAL session_replication_role = replica"))
    campaign.header_media_id = theirs.id
    await db_session.flush()
    await db_session.execute(text("SET LOCAL session_replication_role = origin"))

    assert await _added(db_session, before) == {
        "cr10_campaign_naming_another_workspaces_header_file": 1
    }
