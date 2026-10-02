"""The foundation's oracles, swept over a population built through the real write paths.

Three oracles (OMNI-R10), each stated once as a query in
`scripts/omnichannel_invariants.py` and held here at zero over data that only
production code wrote - WhatsApp ingestion with phone, username and paired
senders, an echo-shaped collision, replies by people and agents, a campaign
audience, and a synthetic second channel with an echo and several files on one
message:

- **Wrong-channel.** Every outbound message traces to a conversation, its
  connection and its participant identity, and all four agree on workspace
  and channel - so no send chose its address from the contact.
- **Echo.** No agent turn is owed or claimed for a message that is not a
  customer's inbound message of that turn's conversation.
- **Identity.** The identities the database holds partition the senders
  exactly as an independently written expectation says they should: one
  person per provider assertion, never one per matching value.

The sweep compares the database before and after the population is written, so
what it proves is that *these* writes added no violation.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.db.models.campaign import Campaign, CampaignStatus
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ConnectionStatus,
    ContactIdentity,
)
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDirection,
    MessageOrigin,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.repositories.agent_turn_repository import AgentTurnRepository
from app.repositories.campaign_repository import AudienceFilter
from app.services import messaging_service as messaging_module
from app.services.campaign_service import CampaignService
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.media_queue import MediaQueue
from app.workers.queue import AgentJob, AgentQueue
from scripts.omnichannel_invariants import CENSUS, INVARIANTS, census, violations
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

pytestmark = pytest.mark.integration


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[Any] = []

    async def enqueue(self, job: Any) -> None:
        self.jobs.append(job)


class Meta:
    """Meta's messages endpoint, accepting every send and naming it."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.ids: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            sent = f"wamid.oracle.{uuid.uuid4().hex}"
            self.ids.append(sent)
            return httpx.Response(
                200, json={"messaging_product": "whatsapp", "messages": [{"id": sent}]}
            )

        return httpx.MockTransport(handle)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        meta_access_token="test-access-token",
    )


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> Iterator[Meta]:
    fake = Meta()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=fake.transport()),
    )
    yield fake


async def _tenant(session: AsyncSession, label: str) -> Tenant:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Oracle {label} {tag}", slug=f"oracle-{label}-{tag}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _number(session: AsyncSession, tenant: Tenant, *, waba: str) -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-{uuid.uuid4().hex[:12]}",
        waba_id=waba,
        display_phone_number="+20 100 000 0000",
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(account)
    await session.flush()
    return account


async def _synthetic(session: AsyncSession, tenant: Tenant) -> ChannelConnection:
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"syn-{uuid.uuid4().hex[:12]}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(connection)
    await session.flush()
    return connection


def _whatsapp(
    account: WhatsAppAccount,
    *,
    phone: str | None = None,
    bsuid: str | None = None,
    wamid: str | None = None,
    name: str = "Someone",
) -> dict[str, Any]:
    sender: dict[str, Any] = {}
    contact: dict[str, Any] = {"profile": {"name": name}}
    if phone is not None:
        sender["from"] = phone
        contact["wa_id"] = phone
    if bsuid is not None:
        sender["from_user_id"] = bsuid
        contact["user_id"] = bsuid
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": account.phone_number_id},
                            "contacts": [contact],
                            "messages": [
                                {
                                    "id": wamid or f"wamid.{uuid.uuid4().hex}",
                                    **sender,
                                    "type": "text",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "text": {"body": "hello"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _deliver(session: AsyncSession, payload: dict[str, Any]) -> RecordingQueue:
    queue = RecordingQueue()
    await WhatsAppIngestionService(session=session, queue=cast(AgentQueue, queue)).ingest(payload)
    return queue


async def _deliver_synthetic(
    session: AsyncSession, adapter: SyntheticAdapter, payload: dict[str, Any]
) -> RecordingQueue:
    queue = RecordingQueue()
    await ChannelIngestionService(
        session=session,
        adapter=cast(ChannelAdapter, adapter),
        queue=cast(AgentQueue, queue),
        media_queue=cast(MediaQueue, RecordingQueue()),
    ).ingest(adapter.parse(payload))
    return queue


async def _claim_every_turn(session: AsyncSession, jobs: list[AgentJob]) -> None:
    for job in jobs:
        assert job.trigger_message_id is not None
        await AgentTurnRepository(session, tenant_id=job.tenant_id).claim(
            conversation_id=job.conversation_id,
            trigger_message_id=job.trigger_message_id,
            worker_id="oracle",
        )


async def _sweep(session: AsyncSession) -> tuple[dict[str, int], dict[str, int]]:
    connection = await session.connection()
    return (
        await violations(connection, read_only=False),
        await census(connection, read_only=False),
    )


async def test_no_write_path_breaks_an_invariant(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    before, census_before = await _sweep(db_session)

    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    sales = await _number(db_session, acme, waba="waba-oracle-1")
    support = await _number(db_session, acme, waba="waba-oracle-1")
    abroad = await _number(db_session, acme, waba="waba-oracle-2")
    theirs = await _number(db_session, rival, waba="waba-oracle-3")

    jobs: list[AgentJob] = []
    # A phone sender, a username sender, and a sender Meta named both ways.
    jobs += (await _deliver(db_session, _whatsapp(sales, phone="201000000501"))).jobs
    jobs += (await _deliver(db_session, _whatsapp(sales, bsuid="EG.0oracle0user0a"))).jobs
    jobs += (
        await _deliver(db_session, _whatsapp(sales, phone="201000000502", bsuid="EG.0oracle0b"))
    ).jobs
    # The same person on a second number of the same account, and the same
    # business-scoped value under another account and in another workspace.
    jobs += (await _deliver(db_session, _whatsapp(support, phone="201000000501"))).jobs
    jobs += (await _deliver(db_session, _whatsapp(abroad, bsuid="EG.0oracle0user0a"))).jobs
    jobs += (await _deliver(db_session, _whatsapp(theirs, phone="201000000501"))).jobs
    await _claim_every_turn(db_session, jobs)

    # Replies, by a person and by an agent, to every WhatsApp conversation -
    # phone-pinned and business-scoped-id-pinned alike.
    conversations = list(
        (
            await db_session.execute(
                select(Conversation)
                .where(Conversation.tenant_id.in_([acme.id, rival.id]))
                .order_by(Conversation.created_at, Conversation.id)
            )
        ).scalars()
    )
    for index, conversation in enumerate(conversations):
        await MessagingService(
            session=db_session, settings=settings, tenant_id=conversation.tenant_id
        ).send_text(
            conversation_id=conversation.id,
            body="Thanks for writing.",
            origin=MessageOrigin.HUMAN if index % 2 else MessageOrigin.AGENT,
        )
    # Echo-shaped collisions: inbound events carrying the ids of this
    # workspace's own sends, on the number that sent one and on another. Named
    # by workspace and number, not by send order: an id another workspace was
    # given is invisible to this one's screen, so it would be a new message.
    for number in (support, sales):
        ours = (
            await db_session.execute(
                select(Message.wa_message_id)
                .where(
                    Message.tenant_id == acme.id,
                    Message.connection_id == number.id,
                    Message.direction == MessageDirection.OUTBOUND,
                    Message.wa_message_id.is_not(None),
                )
                .order_by(Message.created_at, Message.id)
                .limit(1)
            )
        ).scalar_one()
        assert ours in meta.ids
        echo_shaped = await _deliver(
            db_session, _whatsapp(support, phone="201000000509", wamid=ours)
        )
        assert echo_shaped.jobs == []

    # A campaign audience on the sales number.
    template = WhatsAppTemplate(
        tenant_id=acme.id,
        account_id=sales.id,
        name="oracle",
        language="en",
        category=TemplateCategory.MARKETING,
        status=TemplateStatus.APPROVED,
    )
    db_session.add(template)
    await db_session.flush()
    campaign = Campaign(
        tenant_id=acme.id,
        account_id=sales.id,
        template_id=template.id,
        name="Oracle",
        status=CampaignStatus.DRAFT,
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(campaign)
    await db_session.flush()
    await CampaignService(session=db_session, tenant_id=acme.id).set_audience(
        campaign_id=campaign.id, filters=AudienceFilter()
    )

    # A second channel: several files on one message, and an echo of a reply a
    # colleague typed in the provider's own app - projected as `external` (OMNI-037).
    adapter = SyntheticAdapter(Channel.INSTAGRAM)
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.INSTAGRAM: cast(ChannelAdapter, adapter),
        },
    )
    instagram = await _synthetic(db_session, acme)
    now = int(datetime.now(UTC).timestamp())
    synthetic_jobs = await _deliver_synthetic(
        db_session,
        adapter,
        synthetic_payload(
            instagram.external_account_id,
            {
                "type": "message",
                "id": "syn.oracle.1",
                "from": "igsid-oracle",
                "at": now,
                "text": "two photos",
                "attachments": [
                    {"url": "https://cdn.synthetic.test/a.jpg", "kind": "image"},
                    {"url": "https://cdn.synthetic.test/b.jpg", "kind": "image"},
                ],
            },
            {"type": "echo", "id": "syn.oracle.2", "to": "igsid-oracle", "at": now, "text": "x"},
        ),
    )
    await _claim_every_turn(db_session, synthetic_jobs.jobs)
    (on_instagram,) = (
        await db_session.execute(
            select(Conversation).where(Conversation.account_id == instagram.id)
        )
    ).scalars()
    await MessagingService(
        session=db_session, settings=settings, tenant_id=acme.id, channels=registry
    ).send_text(conversation_id=on_instagram.id, body="We see them.", origin=MessageOrigin.HUMAN)
    await db_session.flush()

    after, census_after = await _sweep(db_session)

    assert set(after) == {check.name for check in INVARIANTS}
    added = {name: after[name] - before[name] for name in after if after[name] != before[name]}
    assert added == {}, f"the population broke invariants: {added}"
    grew = {name: census_after[name] - census_before[name] for name in census_after}
    assert set(grew) == {check.name for check in CENSUS}
    # The population really contains what the oracles judge.
    assert grew["q1_contacts_with_phone_and_business_scoped_id"] >= 1
    assert grew["q1_contacts_with_business_scoped_id_only"] >= 1
    assert grew["q7_messages_with_several_files"] == 1
    assert grew["q4_echo_shaped_ids_same_connection"] == 0
    assert grew["q6_conversations_without_messages"] == 0
    outbound = await db_session.scalar(
        text(
            "SELECT count(*) FROM messages WHERE direction = 'outbound'"
            " AND tenant_id IN (:acme, :rival)"
        ),
        {"acme": acme.id, "rival": rival.id},
    )
    # A reply to every conversation, the reply on the second channel, and the
    # external reply its echo projected.
    assert outbound == len(conversations) + 2


async def test_the_identity_partition_is_exactly_what_providers_asserted(
    db_session: AsyncSession,
) -> None:
    """The identity oracle: an expectation written without the code under test.

    Each sender is labelled with the person the scenario says it is; the
    database must group identities into contacts exactly by those labels.
    """
    acme = await _tenant(db_session, "partition-a")
    rival = await _tenant(db_session, "partition-b")
    first = await _number(db_session, acme, waba="waba-partition-1")
    second = await _number(db_session, acme, waba="waba-partition-2")
    rivals = await _number(db_session, rival, waba="waba-partition-1")
    adapter = SyntheticAdapter(Channel.INSTAGRAM)
    page_a = await _synthetic(db_session, acme)
    page_b = await _synthetic(db_session, acme)
    now = int(datetime.now(UTC).timestamp())

    # (label of the person, how they wrote). Same label => same contact.
    scenario: list[tuple[str, Any]] = [
        ("p-phone", (first, {"phone": "201000000601"})),
        # Same phone, other workspace: another person.
        ("q-phone", (rivals, {"phone": "201000000601"})),
        # The phone again with its business-scoped id: provider-asserted, same person.
        ("p-phone", (first, {"phone": "201000000601", "bsuid": "EG.0partition0p"})),
        # That business-scoped id alone later: still the same person.
        ("p-phone", (first, {"bsuid": "EG.0partition0p"})),
        # The same value under another WhatsApp Business Account: another scope.
        ("r-other-account", (second, {"bsuid": "EG.0partition0p"})),
        # Two senders with the same name: never linked by it.
        ("s-name", (first, {"phone": "201000000602"})),
        ("t-name", (first, {"phone": "201000000603"})),
    ]
    for _, (account, sender) in scenario:
        await _deliver(db_session, _whatsapp(account, name="Same Name", **sender))
    # The same Instagram-scoped value on two connections, and a value equal to
    # a WhatsApp phone: three more people, linked to nobody.
    for label, connection, value in (
        ("u-page-a", page_a, "201000000601"),
        ("v-page-b", page_b, "201000000601"),
    ):
        scenario.append((label, (connection, {"igsid": value})))
        await _deliver_synthetic(
            db_session,
            adapter,
            synthetic_payload(
                connection.external_account_id,
                {"type": "message", "id": f"syn.{uuid.uuid4().hex}", "from": value, "at": now},
            ),
        )

    expected_people = {label for label, _ in scenario}
    rows = (
        await db_session.execute(
            select(ContactIdentity).where(ContactIdentity.tenant_id.in_([acme.id, rival.id]))
        )
    ).scalars()
    contacts_by_identity: dict[tuple[uuid.UUID, str, str, str, str], uuid.UUID] = {}
    for identity in rows:
        contacts_by_identity[
            (
                identity.tenant_id,
                identity.channel.value,
                identity.kind.value,
                identity.scope_ref,
                identity.value,
            )
        ] = identity.contact_id

    def key(target: Any, sender: dict[str, str]) -> list[tuple[uuid.UUID, str, str, str, str]]:
        if isinstance(target, ChannelConnection):
            return [(target.tenant_id, "instagram", "igsid", str(target.id), sender["igsid"])]
        keys = []
        if "phone" in sender:
            keys.append((target.tenant_id, "whatsapp", "phone", "", sender["phone"]))
        if "bsuid" in sender:
            keys.append((target.tenant_id, "whatsapp", "bsuid", target.waba_id, sender["bsuid"]))
        return keys

    people: dict[str, set[uuid.UUID]] = defaultdict(set)
    for label, (target, sender) in scenario:
        for identity_key in key(target, sender):
            assert identity_key in contacts_by_identity, (label, identity_key)
            people[label].add(contacts_by_identity[identity_key])

    mismatches = [label for label, contacts in people.items() if len(contacts) != 1]
    assert mismatches == [], f"one person spread over several contacts: {mismatches}"
    owners = [next(iter(contacts)) for contacts in people.values()]
    assert len(set(owners)) == len(expected_people), "two people merged into one contact"
    assert set(contacts_by_identity.values()) == set(owners), "an identity nobody expected"


async def test_the_operator_checks_run_on_a_read_only_replica(prepared_database: str) -> None:
    """`default_transaction_read_only = on`, as a replica or restored copy has it."""
    engine = create_async_engine(
        prepared_database,
        poolclass=NullPool,
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                found = await violations(connection)
                counted = await census(connection)
            # And the session really could not have written anything.
            with pytest.raises(DBAPIError) as refused:
                async with connection.begin():
                    await connection.execute(text("CREATE TEMPORARY TABLE oracle_probe (x int)"))
        assert "read-only" in str(refused.value).lower()
        assert set(found) == {check.name for check in INVARIANTS}
        assert set(counted) == {check.name for check in CENSUS}
    finally:
        await engine.dispose()


async def test_the_oracles_count_a_violation_when_one_exists(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    """Not vacuous: rows no application path writes, inserted by hand, are seen.

    A turn owed for Wasla's own reply (what probe Y1 produced), and an outbound
    message on a WhatsApp conversation pinned to an identity WhatsApp cannot
    address. The keys allow both shapes; only the oracles object.
    """
    before, _ = await _sweep(db_session)
    tenant = await _tenant(db_session, "violation")
    number = await _number(db_session, tenant, waba="waba-violation")
    await _deliver(db_session, _whatsapp(number, phone="201000000701"))
    (conversation,) = (
        await db_session.execute(select(Conversation).where(Conversation.account_id == number.id))
    ).scalars()
    reply = await MessagingService(
        session=db_session, settings=settings, tenant_id=tenant.id
    ).send_text(conversation_id=conversation.id, body="Ours.", origin=MessageOrigin.HUMAN)

    await db_session.execute(
        text(
            "INSERT INTO agent_turns (id, tenant_id, conversation_id, trigger_message_id, state,"
            " created_at, updated_at) VALUES (gen_random_uuid(), :t, :c, :m, 'claimed',"
            " now(), now())"
        ),
        {"t": tenant.id, "c": conversation.id, "m": reply.id},
    )
    contact_id = conversation.contact_id
    psid = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO contact_identities (id, tenant_id, contact_id, channel, kind, scope,"
            " scope_ref, value, source) VALUES (:id, :t, :k, 'whatsapp', 'psid', 'workspace',"
            " '', 'not-addressable', 'provider')"
        ),
        {"id": psid, "t": tenant.id, "k": contact_id},
    )
    await db_session.execute(
        text("UPDATE conversations SET participant_identity_id = :p WHERE id = :c"),
        {"p": psid, "c": conversation.id},
    )
    after, _ = await _sweep(db_session)

    added = {name: after[name] - before[name] for name in after if after[name] != before[name]}
    assert added == {
        "agent_turn_triggered_by_a_non_customer_message": 1,
        "outbound_message_with_a_disagreeing_route": 1,
    }
