"""Marketing consent is per channel (ENT-19, ADR-131; supersedes ADR-122 decision 3).

One person, three ways to reach them: WhatsApp number A, WhatsApp number B, and
an Instagram connection (the synthetic adapter). A STOP is honoured on the
channel it was said on - every WhatsApp number - and nowhere else, and so is a
resume. Every step goes through the production path: the STOP through the
ingestion of the channel it arrives on, the audience through the real audience
builder, a campaign copy through the real send-time guard, a nudge through the
real follow-up dispatch.

Campaigns send WhatsApp templates and nothing else today, so "the Instagram
campaign" is the audience the real builder computes for the Instagram
connection, and the Instagram send is a follow-up's.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.db.models.campaign import OptOutSource, OptOutVia, RecipientStatus
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
    IdentitySource,
)
from app.db.models.conversation import Contact, Conversation
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.repositories.campaign_repository import (
    AudienceFilter,
    AudienceRepository,
    CampaignRecipientRepository,
)
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.follow_up_service import FollowUpService
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.queue import AgentQueue
from tests.channel_fakes import SyntheticAdapter, synthetic_payload
from tests.channel_plans import allow_channels
from tests.consent import consent_of, opted_out_at
from tests.integration.test_campaigns import (
    StubMessaging,
    _campaign,
    _service,
    _template,
)
from tests.integration.test_omnichannel_operations import RecordingQueue

pytestmark = pytest.mark.integration

PHONE = "201000000731"
IGSID = "igsid-0consent"


@dataclass
class Person:
    tenant: Tenant
    contact: Contact
    number_a: WhatsAppAccount
    number_b: WhatsAppAccount
    instagram: ChannelConnection
    adapter: SyntheticAdapter
    registry: ChannelRegistry


def _whatsapp(number: WhatsAppAccount, text: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": number.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": number.phone_number_id},
                            "contacts": [{"wa_id": PHONE}],
                            "messages": [
                                {
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "from": PHONE,
                                    "type": "text",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _whatsapp_writes(session: AsyncSession, number: WhatsAppAccount, text: str) -> None:
    outcome = await WhatsAppIngestionService(
        session=session, queue=cast(AgentQueue, RecordingQueue())
    ).ingest(_whatsapp(number, text))
    assert outcome.stored == 1, outcome
    await session.flush()


async def _instagram_writes(session: AsyncSession, person: Person, text: str) -> None:
    await ChannelIngestionService(
        session=session,
        adapter=cast(ChannelAdapter, person.adapter),
        queue=cast(AgentQueue, RecordingQueue()),
    ).ingest(
        person.adapter.parse(
            synthetic_payload(
                person.instagram.external_account_id,
                {
                    "type": "message",
                    "id": f"m.{uuid.uuid4().hex}",
                    "from": IGSID,
                    "at": int(datetime.now(UTC).timestamp()),
                    "text": text,
                },
            )
        )
    )
    await session.flush()


@pytest_asyncio.fixture
async def person(db_session: AsyncSession) -> AsyncIterator[Person]:
    """One contact writing on WhatsApp numbers A and B and on Instagram."""
    tag = uuid.uuid4().hex[:8]
    tenant = Tenant(name=f"Consent {tag}", slug=f"consent-{tag}")
    db_session.add(tenant)
    await db_session.flush()
    await allow_channels(db_session, tenant.id)
    number_a, number_b = (
        WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"PN-{tag}-{label}",
            waba_id=f"waba-{tag}",
            display_phone_number=f"+20 100 000 07{index}",
        )
        for index, label in enumerate("ab")
    )
    instagram = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"ig-{tag}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=3),
    )
    db_session.add_all([number_a, number_b, instagram])
    await db_session.flush()

    await _whatsapp_writes(db_session, number_a, "hello")
    await _whatsapp_writes(db_session, number_b, "hello again")
    found = await db_session.scalar(
        select(Contact).where(Contact.tenant_id == tenant.id, Contact.wa_id == PHONE)
    )
    assert found is not None
    # The same person on Instagram: their Instagram identity names this contact,
    # so the Instagram ingestion writes into it.
    db_session.add(
        ContactIdentity(
            tenant_id=tenant.id,
            contact_id=found.id,
            channel=Channel.INSTAGRAM,
            kind=IdentityKind.IGSID,
            scope=IdentityScope.CONNECTION,
            scope_ref=str(instagram.id),
            connection_id=instagram.id,
            value=IGSID,
            source=IdentitySource.PROVIDER,
        )
    )
    await db_session.flush()
    adapter = SyntheticAdapter(Channel.INSTAGRAM)
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.INSTAGRAM: cast(ChannelAdapter, adapter),
        }
    )
    person = Person(tenant, found, number_a, number_b, instagram, adapter, registry)
    await _instagram_writes(db_session, person, "hi on instagram")
    conversations = await _conversations(db_session, person)
    assert set(conversations) == {number_a.id, number_b.id, instagram.id}
    yield person


async def _conversations(session: AsyncSession, person: Person) -> dict[uuid.UUID, Conversation]:
    rows = await session.scalars(
        select(Conversation)
        .where(Conversation.contact_id == person.contact.id)
        .execution_options(populate_existing=True)
    )
    return {row.account_id: row for row in rows}


async def _audience(
    session: AsyncSession, person: Person, connection_id: uuid.UUID
) -> set[uuid.UUID]:
    """Who the real audience builder lets a campaign on this connection reach."""
    members = await AudienceRepository(session, tenant_id=person.tenant.id).list_eligible_members(
        account_id=connection_id, filters=AudienceFilter(), limit=100
    )
    return {contact.id for contact, _identity in members}


async def _nudge(
    session: AsyncSession, settings: Settings, person: Person, connection_id: uuid.UUID
) -> FollowUpStatus:
    """Dispatch one due follow-up in the person's conversation on this connection."""
    conversation = (await _conversations(session, person))[connection_id]
    follow_up = FollowUp(
        tenant_id=person.tenant.id,
        conversation_id=conversation.id,
        status=FollowUpStatus.PENDING,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        body="Still interested?",
        created_by_kind=ActorKind.AGENT,
    )
    session.add(follow_up)
    await session.flush()
    outcome = await FollowUpService(
        session=session,
        tenant_id=person.tenant.id,
        settings=settings,
        messaging=MessagingService(
            session=session,
            settings=settings,
            tenant_id=person.tenant.id,
            channels=person.registry,
        ),
        channels=person.registry,
    ).dispatch(follow_up)
    return outcome.follow_up.status


async def test_a_stop_on_whatsapp_covers_every_number_and_not_instagram(
    db_session: AsyncSession, person: Person, settings: Settings
) -> None:
    """M-E28's killer: the opt-out is WhatsApp's, not the person's."""
    everyone = {person.contact.id}
    assert await _audience(db_session, person, person.number_b.id) == everyone

    await _whatsapp_writes(db_session, person.number_a, "STOP")

    whatsapp = await consent_of(db_session, person.contact.id, Channel.WHATSAPP)
    assert whatsapp is not None and whatsapp.marketing_opt_out_at is not None
    assert (whatsapp.opt_out_source, whatsapp.opt_out_via) == (
        OptOutSource.CUSTOMER,
        OptOutVia.MESSAGE,
    )
    assert await consent_of(db_session, person.contact.id, Channel.INSTAGRAM) is None
    # Said on number A; honoured on number B too, and nowhere on Instagram.
    assert await _audience(db_session, person, person.number_a.id) == set()
    assert await _audience(db_session, person, person.number_b.id) == set()
    assert await _audience(db_session, person, person.instagram.id) == everyone
    # A nudge on Instagram still goes out; one on WhatsApp is skipped.
    assert await _nudge(db_session, settings, person, person.instagram.id) is FollowUpStatus.SENT
    assert len(person.adapter.log.sent) == 1
    assert await _nudge(db_session, settings, person, person.number_b.id) is FollowUpStatus.SKIPPED


async def test_a_campaign_copy_already_queued_is_skipped_after_a_stop_on_whatsapp(
    db_session: AsyncSession, person: Person
) -> None:
    """The send-time guard reads the campaign's channel: a stop said on number A
    after the audience of a campaign on number B was built still stops its copy."""
    template = await _template(db_session, tenant=person.tenant, account=person.number_b)
    campaign = await _campaign(
        db_session, tenant=person.tenant, account=person.number_b, template=template, rate=60
    )
    messaging = StubMessaging(db_session, tenant_id=person.tenant.id)
    service = _service(db_session, person.tenant, messaging=messaging)
    await service.set_audience(campaign_id=campaign.id, filters=AudienceFilter())
    await db_session.flush()
    assert campaign.audience_size == 1

    await _whatsapp_writes(db_session, person.number_a, "STOP")
    await service.schedule(campaign_id=campaign.id)
    outcome = await service.dispatch_batch(campaign)
    await db_session.flush()

    assert (outcome.sent, outcome.skipped) == (0, 1)
    assert messaging.sends == []
    [recipient] = await CampaignRecipientRepository(
        db_session, tenant_id=person.tenant.id
    ).list_for_campaign(campaign.id)
    assert recipient.status is RecipientStatus.SKIPPED


async def test_a_stop_on_instagram_opts_out_instagram_alone(
    db_session: AsyncSession, person: Person
) -> None:
    """M-E29's killer: the writer records the channel it was told, not WhatsApp's."""
    await _instagram_writes(db_session, person, "stop")

    instagram = await consent_of(db_session, person.contact.id, Channel.INSTAGRAM)
    assert instagram is not None and instagram.marketing_opt_out_at is not None
    assert instagram.opt_out_via is OptOutVia.MESSAGE
    assert await opted_out_at(db_session, person.contact.id, Channel.WHATSAPP) is None
    assert await _audience(db_session, person, person.number_a.id) == {person.contact.id}
    assert await _audience(db_session, person, person.instagram.id) == set()


async def test_a_resume_on_whatsapp_resumes_whatsapp_alone(
    db_session: AsyncSession, person: Person
) -> None:
    service = _service(db_session, person.tenant)
    for channel in (Channel.WHATSAPP, Channel.INSTAGRAM):
        await service.set_opt_out(
            contact_id=person.contact.id, channel=channel, source=OptOutSource.CUSTOMER
        )
    await db_session.flush()

    await service.clear_opt_out(person.contact.id, channel=Channel.WHATSAPP)

    whatsapp = await consent_of(db_session, person.contact.id, Channel.WHATSAPP)
    assert whatsapp is not None and whatsapp.marketing_opt_out_at is None
    assert (whatsapp.resume_source, whatsapp.resume_via) == (OptOutSource.TEAM, OptOutVia.TEAM)
    assert await opted_out_at(db_session, person.contact.id, Channel.INSTAGRAM) is not None
    assert await _audience(db_session, person, person.number_a.id) == {person.contact.id}
    assert await _audience(db_session, person, person.instagram.id) == set()


async def test_another_workspaces_consent_is_never_read(
    db_session: AsyncSession, person: Person
) -> None:
    """A refusal is keyed by its workspace: a rival's row for the same contact id
    cannot exist, and the rival's own opt-outs never shrink this audience."""
    rival = Tenant(name="Rival consent", slug=f"rival-consent-{uuid.uuid4().hex[:8]}")
    db_session.add(rival)
    await db_session.flush()
    rival_contact = Contact(tenant_id=rival.id, wa_id=PHONE)
    db_session.add(rival_contact)
    await db_session.flush()
    await _service(db_session, rival).set_opt_out(
        contact_id=rival_contact.id, channel=Channel.WHATSAPP, source=OptOutSource.CUSTOMER
    )
    await db_session.flush()

    assert await _audience(db_session, person, person.number_a.id) == {person.contact.id}
    with pytest.raises(NotFoundError):
        await _service(db_session, rival).set_opt_out(
            contact_id=person.contact.id, channel=Channel.WHATSAPP, source=OptOutSource.TEAM
        )
