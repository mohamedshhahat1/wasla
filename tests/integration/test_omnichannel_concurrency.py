"""Races on the neutral foundation, run as real concurrent PostgreSQL transactions.

Every delivery here runs in its own session and transaction, released together
by a barrier, and commits - the shape two webhook requests (or a request and a
sweeper) actually have. The suite's rolled-back `db_session` cannot show any of
this: inside one transaction nothing races.

Each race must converge on one logical outcome - one contact, one identity, one
conversation pinned once, one message, one set of files, one agent turn - with
no deadlock, no lost delivery and nothing filed in another workspace:

- a new username sender's first messages, together;
- a known customer's phone and business-scoped id named together, concurrently;
- a brand-new sender named both ways, concurrently (participant pinned once);
- the same provider id on two numbers of one workspace, together;
- an echo-shaped event racing a status for the same message;
- one message with several files delivered several times at once;
- the recovery sweep racing the provider's redelivery of an owed event.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.channels.adapter import ChannelAdapter
from app.core.config import Settings
from app.core.redis import RedisClient
from app.db.models.agent_turn import AgentTurn
from app.db.models.channel import ContactIdentity, IdentityKind
from app.db.models.channel_event import ChannelEvent, ChannelEventState
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.media import MessageMedia
from app.db.session import Database
from app.services import messaging_service as messaging_module
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.inbound_recovery import InboundRecoveryWorker
from app.workers.media_queue import MediaQueue
from app.workers.queue import AgentJob, AgentQueue
from tests.channel_fakes import SyntheticAdapter, synthetic_payload
from tests.fake_queue_redis import FakeQueueRedis

pytestmark = pytest.mark.integration


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[Any] = []

    async def enqueue(self, job: Any) -> None:
        self.jobs.append(job)


class BrokenQueue:
    async def enqueue(self, job: Any) -> None:
        raise RedisError("Redis is down.")


@pytest.fixture
def engine(prepared_database: str) -> Iterator[AsyncEngine]:
    yield create_async_engine(prepared_database, poolclass=NullPool)


@pytest.fixture
async def workspace(engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """A committed workspace with two WhatsApp numbers and a synthetic connection."""
    tenant = uuid.uuid4()
    tag = tenant.hex[:10]
    numbers = [(uuid.uuid4(), f"PN-race-{tag}-{index}") for index in range(2)]
    synthetic = (uuid.uuid4(), f"syn-race-{tag}")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tenants (id, name, slug, status, created_at, updated_at)"
                " VALUES (:id, 'Race', :slug, 'active', now(), now())"
            ),
            {"id": tenant, "slug": f"race-{tag}"},
        )
        for number_id, phone_number_id in numbers:
            await connection.execute(
                text(
                    "INSERT INTO whatsapp_accounts (id, tenant_id, phone_number_id, waba_id,"
                    " display_phone_number, status, ownership_started_at, created_at, updated_at)"
                    " VALUES (:id, :t, :pn, :waba, '+201000000000', 'active',"
                    " now() - interval '1 day', now(), now())"
                ),
                {"id": number_id, "t": tenant, "pn": phone_number_id, "waba": f"waba-{tag}"},
            )
        await connection.execute(
            text(
                "INSERT INTO channel_connections (id, tenant_id, channel, external_account_id,"
                " status, ownership_started_at) VALUES (:id, :t, 'instagram', :key, 'active',"
                " now() - interval '1 day')"
            ),
            {"id": synthetic[0], "t": tenant, "key": synthetic[1]},
        )
    try:
        yield {
            "tenant": tenant,
            "numbers": numbers,
            "waba": f"waba-{tag}",
            "synthetic": synthetic,
        }
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})
        await engine.dispose()


def _whatsapp(
    phone_number_id: str,
    *,
    phone: str | None = None,
    bsuid: str | None = None,
    wamid: str | None = None,
    kind: str = "message",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"phone_number_id": phone_number_id},
    }
    if kind == "message":
        sender: dict[str, Any] = {}
        contact: dict[str, Any] = {"profile": {"name": "Race"}}
        if phone is not None:
            sender["from"] = phone
            contact["wa_id"] = phone
        if bsuid is not None:
            sender["from_user_id"] = bsuid
            contact["user_id"] = bsuid
        value["contacts"] = [contact]
        value["messages"] = [
            {
                "id": wamid or f"wamid.{uuid.uuid4().hex}",
                **sender,
                "type": "text",
                "timestamp": str(int(datetime.now(UTC).timestamp())),
                "text": {"body": "hello"},
            }
        ]
    else:
        value["statuses"] = [
            {
                "id": wamid,
                "status": kind,
                "timestamp": str(int(datetime.now(UTC).timestamp())),
                "recipient_id": phone,
            }
        ]
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba", "changes": [{"field": "messages", "value": value}]}],
    }


async def _together(
    engine: AsyncEngine, work: list[Callable[[AsyncSession], Awaitable[Any]]]
) -> list[Any]:
    """Run each unit of work in its own committed transaction, released at one instant."""
    barrier = asyncio.Barrier(len(work))

    async def run(unit: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
            await barrier.wait()
            return await unit(session)

    return list(await asyncio.wait_for(asyncio.gather(*(run(unit) for unit in work)), timeout=60))


def _deliver(payload: dict[str, Any], queue: Any) -> Callable[[AsyncSession], Awaitable[Any]]:
    async def unit(session: AsyncSession) -> Any:
        return await WhatsAppIngestionService(
            session=session, queue=cast(AgentQueue, queue)
        ).ingest(payload)

    return unit


async def _scalar(engine: AsyncEngine, query: Any) -> Any:
    async with AsyncSession(engine) as session:
        return await session.scalar(query)


async def _rows(engine: AsyncEngine, query: Any) -> list[Any]:
    async with AsyncSession(engine) as session:
        return list((await session.execute(query)).scalars().all())


async def test_a_username_senders_first_messages_together_make_one_person(
    engine: AsyncEngine, workspace: dict[str, Any]
) -> None:
    (_, phone_number_id), _ = workspace["numbers"]
    queue = RecordingQueue()
    payloads = [_whatsapp(phone_number_id, bsuid="EG.0race0username") for _ in range(4)]

    outcomes = await _together(engine, [_deliver(payload, queue) for payload in payloads])

    assert sum(outcome.stored for outcome in outcomes) == 4
    tenant = workspace["tenant"]
    contacts = await _rows(engine, select(Contact).where(Contact.tenant_id == tenant))
    identities = await _rows(
        engine, select(ContactIdentity).where(ContactIdentity.tenant_id == tenant)
    )
    conversations = await _rows(
        engine, select(Conversation).where(Conversation.tenant_id == tenant)
    )
    assert len(contacts) == 1 and contacts[0].wa_id is None
    assert [(i.kind, i.value) for i in identities] == [(IdentityKind.BSUID, "EG.0race0username")]
    (conversation,) = conversations
    assert conversation.participant_identity_id == identities[0].id
    # Four new messages, four turns - each keyed on its own message.
    assert len({job.trigger_message_id for job in queue.jobs}) == 4


async def test_a_pairing_named_by_concurrent_deliveries_is_attached_once(
    engine: AsyncEngine, workspace: dict[str, Any]
) -> None:
    (_, phone_number_id), _ = workspace["numbers"]
    queue = RecordingQueue()
    await _together(engine, [_deliver(_whatsapp(phone_number_id, phone="201000001001"), queue)])

    payloads = [
        _whatsapp(phone_number_id, phone="201000001001", bsuid="EG.0race0pair") for _ in range(4)
    ]
    outcomes = await _together(engine, [_deliver(payload, queue) for payload in payloads])

    assert sum(outcome.stored for outcome in outcomes) == 4
    assert sum(outcome.identity_conflicts for outcome in outcomes) == 0
    tenant = workspace["tenant"]
    identities = await _rows(
        engine, select(ContactIdentity).where(ContactIdentity.tenant_id == tenant)
    )
    assert sorted((i.kind.value, i.value) for i in identities) == [
        ("bsuid", "EG.0race0pair"),
        ("phone", "201000001001"),
    ]
    assert len({identity.contact_id for identity in identities}) == 1
    assert (
        await _scalar(
            engine,
            select(func.count()).select_from(Conversation).where(Conversation.tenant_id == tenant),
        )
        == 1
    )


async def test_a_new_sender_named_both_ways_concurrently_is_pinned_once_to_the_phone(
    engine: AsyncEngine, workspace: dict[str, Any]
) -> None:
    (_, phone_number_id), _ = workspace["numbers"]
    payloads = [
        _whatsapp(phone_number_id, phone="201000001002", bsuid="EG.0race0both") for _ in range(4)
    ]

    await _together(engine, [_deliver(payload, RecordingQueue()) for payload in payloads])

    tenant = workspace["tenant"]
    contacts = await _rows(engine, select(Contact).where(Contact.tenant_id == tenant))
    (conversation,) = await _rows(
        engine, select(Conversation).where(Conversation.tenant_id == tenant)
    )
    phone = await _scalar(
        engine,
        select(ContactIdentity).where(
            ContactIdentity.tenant_id == tenant, ContactIdentity.kind == IdentityKind.PHONE
        ),
    )
    assert len(contacts) == 1
    assert conversation.participant_identity_id == phone.id
    assert (
        await _scalar(
            engine,
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation.id),
        )
        == 4
    )


async def test_one_provider_id_on_two_numbers_at_once_is_one_message(
    engine: AsyncEngine, workspace: dict[str, Any]
) -> None:
    (_, first), (_, second) = workspace["numbers"]
    wamid = f"wamid.race.{uuid.uuid4().hex}"
    queue = RecordingQueue()

    outcomes = await _together(
        engine,
        [
            _deliver(_whatsapp(first, phone="201000001003", wamid=wamid), queue),
            _deliver(_whatsapp(second, phone="201000001004", wamid=wamid), queue),
        ],
    )

    tenant = workspace["tenant"]
    assert (
        await _scalar(
            engine,
            select(func.count())
            .select_from(Message)
            .where(Message.tenant_id == tenant, Message.wa_message_id == wamid),
        )
        == 1
    )
    # One stored; the other is a collision - counted, and while the
    # workspace-wide event key stands (ADR-120) not even stored. Never a second turn.
    assert sum(outcome.stored for outcome in outcomes) == 1
    assert sum(outcome.collisions for outcome in outcomes) == 1
    assert len(queue.jobs) == 1
    assert (
        await _scalar(
            engine,
            select(func.count()).select_from(Conversation).where(Conversation.tenant_id == tenant),
        )
        <= 2
    )


async def test_an_echo_shaped_event_racing_a_status_moves_only_the_status(
    engine: AsyncEngine, workspace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    (_, phone_number_id), _ = workspace["numbers"]
    sent_id = f"wamid.race.sent.{uuid.uuid4().hex}"

    def meta(request: httpx.Request) -> httpx.Response:
        json.loads(request.content)
        return httpx.Response(
            200, json={"messaging_product": "whatsapp", "messages": [{"id": sent_id}]}
        )

    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(meta)),
    )
    await _together(
        engine, [_deliver(_whatsapp(phone_number_id, phone="201000001005"), RecordingQueue())]
    )
    tenant = workspace["tenant"]
    (conversation,) = await _rows(
        engine, select(Conversation).where(Conversation.tenant_id == tenant)
    )
    settings = Settings(_env_file=None, environment="test", meta_access_token="race-token-fixture")

    # The send commits its own transactions (ADR-093), so it runs in a plain session.
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await MessagingService(session=session, settings=settings, tenant_id=tenant).send_text(
            conversation_id=conversation.id, body="Ours.", origin=MessageOrigin.HUMAN
        )
        await session.commit()
    window_before = conversation.last_inbound_at
    queue = RecordingQueue()

    await _together(
        engine,
        [
            _deliver(_whatsapp(phone_number_id, phone="201000001005", wamid=sent_id), queue),
            _deliver(
                _whatsapp(phone_number_id, phone="201000001005", wamid=sent_id, kind="read"), queue
            ),
        ],
    )

    assert queue.jobs == []
    sent = await _scalar(
        engine, select(Message).where(Message.tenant_id == tenant, Message.wa_message_id == sent_id)
    )
    assert sent.status is MessageStatus.READ
    assert (
        await _scalar(
            engine,
            select(func.count())
            .select_from(Message)
            .where(Message.tenant_id == tenant, Message.wa_message_id == sent_id),
        )
        == 1
    )
    after = await _scalar(engine, select(Conversation).where(Conversation.id == conversation.id))
    assert after.last_inbound_at == window_before
    assert (
        await _scalar(
            engine,
            select(func.count())
            .select_from(AgentTurn)
            .where(AgentTurn.trigger_message_id == sent.id),
        )
        == 0
    )


async def test_several_files_delivered_several_times_at_once_are_stored_once(
    engine: AsyncEngine, workspace: dict[str, Any]
) -> None:
    _, key = workspace["synthetic"]
    adapter = SyntheticAdapter()
    payload = synthetic_payload(
        key,
        {
            "type": "message",
            "id": "syn.race.files",
            "from": "igsid-race",
            "at": int(datetime.now(UTC).timestamp()),
            "attachments": [
                {"url": f"https://cdn.synthetic.test/{index}.jpg", "kind": "image"}
                for index in range(3)
            ],
        },
    )

    def unit(session: AsyncSession) -> Awaitable[Any]:
        return ChannelIngestionService(
            session=session,
            adapter=cast(ChannelAdapter, adapter),
            queue=cast(AgentQueue, RecordingQueue()),
            media_queue=cast(MediaQueue, RecordingQueue()),
        ).ingest(adapter.parse(payload))

    await _together(engine, [unit, unit, unit])

    tenant = workspace["tenant"]
    files = await _rows(
        engine,
        select(MessageMedia)
        .where(MessageMedia.tenant_id == tenant)
        .order_by(MessageMedia.position),
    )
    assert [media.position for media in files] == [0, 1, 2]
    assert len({media.message_id for media in files}) == 1


async def test_the_sweep_racing_a_redelivery_queues_one_turn(
    engine: AsyncEngine, workspace: dict[str, Any], prepared_database: str
) -> None:
    (_, phone_number_id), _ = workspace["numbers"]
    payload = _whatsapp(phone_number_id, phone="201000001006")
    # Stored while Redis refused the job: the event owes a turn.
    await _together(engine, [_deliver(payload, BrokenQueue())])
    tenant = workspace["tenant"]
    owed = await _scalar(engine, select(ChannelEvent).where(ChannelEvent.tenant_id == tenant))
    assert owed.state is ChannelEventState.RECEIVED

    settings = Settings(_env_file=None, environment="test", database_url=prepared_database)
    database = Database(settings)
    swept = RecordingQueue()
    live = RecordingQueue()
    try:
        workers = []
        for _ in range(2):
            worker = InboundRecoveryWorker(
                database=database,
                redis=cast(RedisClient, type("R", (), {"client": FakeQueueRedis()})()),
                settings=settings,
                grace_seconds=0.0,
            )
            worker._agent_queue = cast(AgentQueue, swept)
            worker._media_queue = cast(MediaQueue, swept)
            workers.append(worker)
        later = datetime.now(UTC) + timedelta(seconds=1)
        await asyncio.wait_for(
            asyncio.gather(
                *(worker.run_once(now=later) for worker in workers),
                _together(engine, [_deliver(payload, live)]),
            ),
            timeout=60,
        )
    finally:
        await database.dispose()

    assert len(swept.jobs) + len(live.jobs) == 1
    (job,) = swept.jobs + live.jobs
    assert isinstance(job, AgentJob) and job.trigger_message_id is not None
    settled = await _scalar(engine, select(ChannelEvent.state).where(ChannelEvent.id == owed.id))
    assert settled is ChannelEventState.PROCESSED
