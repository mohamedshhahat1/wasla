"""What happens to an inbound message the queue refused, and who owns it after.

Three properties, and the first is the one that made MSG-02 a production
blocker rather than an inconvenience.

**A stored event that still owes work says so.** Swallowing an enqueue failure
is right - a non-2xx makes Meta retry the whole delivery and eventually disable
the subscription - but until `whatsapp_events.state` meant something, a message
nobody would ever answer was indistinguishable from one that had been answered.
Every event ever stored sat at `received`; `PROCESSED` and `FAILED` were
unreachable from any code path.

**Recovery re-derives rather than replays**, so running it twice produces one
agent turn rather than two. A replay would re-project the message, re-cancel
its follow-ups and re-meter the delivery.

**Two sweepers produce one outcome.** `FOR UPDATE SKIP LOCKED` decides it, and
what is being recovered ends in a message to somebody's customer - so this is
the assertion that separates "the backlog drains" from "the customer is
answered twice".

Real PostgreSQL and real Redis throughout. A broken queue is simulated by a
client pointed at a closed port rather than by a mock, because what is being
tested is the behaviour of the code around a `RedisError`, and a mock that
raises one is a test of the mock.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.redis import RedisClient
from app.db.models.conversation import Contact, Conversation, Message, MessageKind
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import (
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventState,
)
from app.db.session import Database
from app.repositories.whatsapp_repository import InboundEventSweep
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.inbound_recovery import InboundRecoveryWorker
from app.workers.queue import QUEUE_NAMESPACE, AgentQueue

pytestmark = pytest.mark.integration

REDIS_URL = "redis://localhost:6379/12"
# A port nothing is listening on. The client built against it raises
# `RedisError` on every command, which is exactly what a Redis outage looks
# like from inside the webhook.
DEAD_REDIS_URL = "redis://127.0.0.1:6399/0"
PENDING = f"{QUEUE_NAMESPACE}:pending"
PHONE_NUMBER_ID = "PN-RECOVERY"
CUSTOMER = "201555000222"


def _inbound(wamid: str, *, at: datetime, text: str = "anyone there?") -> dict[str, object]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Nadia"}}],
                            "messages": [
                                {
                                    "from": CUSTOMER,
                                    "id": wamid,
                                    "type": "text",
                                    "timestamp": str(int(at.timestamp())),
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


@pytest.fixture
async def live_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def working_queue() -> AgentQueue:
    return AgentQueue(
        Redis.from_url(REDIS_URL, decode_responses=True),
        visibility_timeout_seconds=60,
    )


@pytest.fixture
def broken_queue() -> AgentQueue:
    """A queue that cannot be reached, which is the whole scenario."""
    return AgentQueue(
        Redis.from_url(DEAD_REDIS_URL, decode_responses=True, socket_connect_timeout=1),
        visibility_timeout_seconds=60,
    )


async def _workspace(session: AsyncSession) -> tuple[Tenant, WhatsAppAccount]:
    tenant = Tenant(name="Recovery", slug=f"recovery-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=PHONE_NUMBER_ID,
        waba_id="waba-recovery",
        display_phone_number="+20 100 000 0001",
        status=WhatsAppAccountStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=2),
        ownership_verified_at=datetime.now(UTC) - timedelta(days=2),
    )
    session.add(account)
    await session.flush()
    return tenant, account


async def _event_state(session: AsyncSession, wamid: str) -> tuple[str, str | None]:
    event = (
        await session.execute(select(WhatsAppEvent).where(WhatsAppEvent.event_id == wamid))
    ).scalar_one()
    return event.state.value, event.error


async def test_a_message_stored_while_redis_is_down_is_marked_as_still_owing(
    db_session: AsyncSession,
    live_redis: Redis,
    broken_queue: AgentQueue,
) -> None:
    """The webhook still answers, the message still lands, and the debt is recorded.

    Before this, all three of those were true except the last - and the last is
    what made the first two safe to do. `docs/RUNBOOK.md` told an operator to
    requeue these conversations and nothing could find them (MSG-02).
    """
    _, account = await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"

    service = WhatsAppIngestionService(session=db_session, queue=broken_queue)
    outcome = await service.ingest(_inbound(wamid, at=datetime.now(UTC)))
    await db_session.flush()

    # Stored and projected, exactly as it would be on a healthy deployment.
    assert outcome.stored == 1
    assert outcome.queued == 0
    message = (
        await db_session.execute(select(Message).where(Message.wa_message_id == wamid))
    ).scalar_one()
    assert message.conversation_id is not None

    # And the event says what is missing, in a bounded token rather than a
    # provider message or a fragment of the payload.
    state, error = await _event_state(db_session, wamid)
    assert state == WhatsAppEventState.RECEIVED.value
    assert error == "agent_enqueue_failed"
    assert await live_redis.llen(PENDING) == 0
    assert account.tenant_id == message.tenant_id


async def test_a_healthy_delivery_marks_the_event_finished(
    db_session: AsyncSession,
    live_redis: Redis,
    working_queue: AgentQueue,
) -> None:
    """`PROCESSED` means the handoff landed, not merely that a row exists.

    The negative control for the test above: the same path with a reachable
    queue must leave nothing owing, or the sweeper would churn through every
    healthy event on the deployment.
    """
    await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"

    service = WhatsAppIngestionService(session=db_session, queue=working_queue)
    outcome = await service.ingest(_inbound(wamid, at=datetime.now(UTC)))
    await db_session.flush()

    assert outcome.queued == 1
    state, error = await _event_state(db_session, wamid)
    assert state == WhatsAppEventState.PROCESSED.value
    assert error is None
    assert await live_redis.llen(PENDING) == 1


async def test_a_redelivery_does_not_re_project_and_does_not_re_queue(
    db_session: AsyncSession,
    live_redis: Redis,
    broken_queue: AgentQueue,
    working_queue: AgentQueue,
) -> None:
    """Meta's retry is still a duplicate, even when the first attempt owed work.

    The tempting fix - let a redelivery re-enqueue what the first delivery
    could not - would mean two paths racing to queue one turn, and the losing
    one is whichever the sweeper also picks up. Recovery has a single owner,
    and this pins that a redelivery is not it (ADR-102).
    """
    await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"
    payload = _inbound(wamid, at=datetime.now(UTC))

    await WhatsAppIngestionService(session=db_session, queue=broken_queue).ingest(payload)
    await db_session.flush()

    # Redis is back, and Meta redelivers. The event is a duplicate and stops
    # there: no second message, and no job from this path.
    again = await WhatsAppIngestionService(session=db_session, queue=working_queue).ingest(payload)
    await db_session.flush()

    assert again.stored == 0
    assert again.duplicates == 1
    assert again.queued == 0
    messages = (
        (await db_session.execute(select(Message).where(Message.wa_message_id == wamid)))
        .scalars()
        .all()
    )
    assert len(messages) == 1
    state, _ = await _event_state(db_session, wamid)
    assert state == WhatsAppEventState.RECEIVED.value
    assert await live_redis.llen(PENDING) == 0


@pytest.fixture
async def recovery_database(prepared_database: str) -> AsyncIterator[Database]:
    """A pool of the sweeper's own, because it commits on its own connections."""
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=prepared_database,
        redis_url=REDIS_URL,
    )
    database = Database(settings)
    try:
        yield database
    finally:
        await database.dispose()


def _worker(database: Database) -> InboundRecoveryWorker:
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=str(database.engine.url),
        redis_url=REDIS_URL,
    )
    return InboundRecoveryWorker(
        database=database,
        redis=RedisClient(settings),
        settings=settings,
        # Zero grace, so the test does not have to wait five minutes for an
        # event it created a moment ago to become claimable.
        grace_seconds=0.0,
    )


async def test_the_sweeper_queues_the_turn_the_outage_lost_and_only_once(
    recovery_database: Database,
    live_redis: Redis,
    broken_queue: AgentQueue,
) -> None:
    """The end of MSG-02, measured: stored during an outage, answered after it.

    Committed on the sweeper's own pool rather than staged in the rolled-back
    session fixture, because the worker opens its own connections and cannot
    see an uncommitted row - the same reason `test_commit_boundary.py` tidies
    up after itself.
    """
    wamid = f"wamid.{uuid.uuid4().hex}"
    async with recovery_database.session() as session:
        tenant, _ = await _workspace(session)
        tenant_id = tenant.id
        await WhatsAppIngestionService(session=session, queue=broken_queue).ingest(
            _inbound(wamid, at=datetime.now(UTC))
        )
        await session.commit()

    try:
        assert await live_redis.llen(PENDING) == 0

        first = await _worker(recovery_database).run_once()
        assert first.claimed == 1
        assert first.agent_jobs == 1
        assert first.completed == 1
        assert await live_redis.llen(PENDING) == 1

        async with recovery_database.session() as session:
            state, error = await _event_state(session, wamid)
        assert state == WhatsAppEventState.PROCESSED.value
        assert error is None

        # Run it again. The event is finished, so there is nothing to claim and
        # the customer is not answered a second time.
        second = await _worker(recovery_database).run_once()
        assert second.claimed == 0
        assert await live_redis.llen(PENDING) == 1
    finally:
        await _cleanup(recovery_database, tenant_id)


async def test_two_sweepers_running_together_queue_one_turn_between_them(
    recovery_database: Database,
    live_redis: Redis,
    broken_queue: AgentQueue,
) -> None:
    """The assertion that separates draining a backlog from double-answering.

    Deployments run more than one worker container, and both will sweep. If the
    claim were an ordinary read, each would find the same event and each would
    queue a turn - and the customer would receive two replies to one message.
    """
    wamid = f"wamid.{uuid.uuid4().hex}"
    async with recovery_database.session() as session:
        tenant, _ = await _workspace(session)
        tenant_id = tenant.id
        await WhatsAppIngestionService(session=session, queue=broken_queue).ingest(
            _inbound(wamid, at=datetime.now(UTC))
        )
        await session.commit()

    try:
        outcomes = await asyncio.gather(
            _worker(recovery_database).run_once(),
            _worker(recovery_database).run_once(),
        )
        assert sorted(outcome.claimed for outcome in outcomes) == [0, 1]
        assert sum(outcome.agent_jobs for outcome in outcomes) == 1
        assert await live_redis.llen(PENDING) == 1
    finally:
        await _cleanup(recovery_database, tenant_id)


async def test_a_claimed_event_is_invisible_to_another_sweeper_holding_it_open(
    recovery_database: Database,
    live_redis: Redis,
    broken_queue: AgentQueue,
) -> None:
    """The lock itself, held open across a second sweeper's claim.

    The two-sweeper test above drives the whole worker, and two `run_once`
    calls can finish one after the other without ever overlapping - so it
    passes whether or not the claim locks anything. This one makes the overlap
    explicit: the first transaction claims and *stays open*, and only then does
    the second one look. With `FOR UPDATE SKIP LOCKED` the second sees nothing;
    without it the second sees the same event and would go on to queue a second
    agent turn for the same customer message.

    Two sessions, so the two claims are genuinely separate transactions on
    separate connections. One session would share a transaction and the lock
    would be invisible by construction.
    """
    wamid = f"wamid.{uuid.uuid4().hex}"
    async with recovery_database.session() as session:
        tenant, _ = await _workspace(session)
        tenant_id = tenant.id
        await WhatsAppIngestionService(session=session, queue=broken_queue).ingest(
            _inbound(wamid, at=datetime.now(UTC))
        )
        await session.commit()

    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=1)
        async with recovery_database.session() as first:
            claimed_by_first = await InboundEventSweep(first).claim_unprocessed(
                older_than=cutoff, limit=10
            )
            assert [event.event_id for event in claimed_by_first] == [wamid]

            # The first transaction is still open and still holding the row.
            async with recovery_database.session() as second:
                claimed_by_second = await InboundEventSweep(second).claim_unprocessed(
                    older_than=cutoff, limit=10
                )
            assert claimed_by_second == []
    finally:
        await _cleanup(recovery_database, tenant_id)


async def _cleanup(database: Database, tenant_id: uuid.UUID) -> None:
    """Remove what the sweeper tests committed.

    They cannot use the rolled-back session fixture, so they tidy up after
    themselves - and deleting the workspace cascades to everything below it.
    """
    async with database.session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is not None:
            await session.delete(tenant)
        await session.commit()


def _typed(wamid: str, *, message_type: str, at: datetime) -> dict[str, object]:
    """An inbound message of a type Wasla stores but cannot read."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Nadia"}}],
                            "messages": [
                                {
                                    "from": CUSTOMER,
                                    "id": wamid,
                                    "type": message_type,
                                    "timestamp": str(int(at.timestamp())),
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


@pytest.mark.parametrize("message_type", ["reaction", "order", "system", "contacts"])
async def test_a_message_wasla_cannot_read_costs_no_inference(
    db_session: AsyncSession,
    live_redis: Redis,
    working_queue: AgentQueue,
    message_type: str,
) -> None:
    """A thumbs-up is not a question, so nothing is asked to answer it.

    Every non-media inbound message was handed to an agent whatever its type.
    The model was told a customer had sent `[unsupported]` - a message with no
    content - and answered anyway: one billed inference and possibly one
    WhatsApp reply to nothing, three times over for somebody tapping a reaction
    on three messages (MSG-19).

    Still stored, and still acknowledged. The raw event is kept whole so a
    message type Meta ships tomorrow can be understood later, and the message
    row keeps the conversation's history honest. It is the turn that is
    refused, not the record.
    """
    await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"

    outcome = await WhatsAppIngestionService(session=db_session, queue=working_queue).ingest(
        _typed(wamid, message_type=message_type, at=datetime.now(UTC))
    )
    await db_session.flush()

    assert outcome.stored == 1
    assert outcome.queued == 0
    assert await live_redis.llen(PENDING) == 0

    message = (
        await db_session.execute(select(Message).where(Message.wa_message_id == wamid))
    ).scalar_one()
    assert message.kind is MessageKind.UNSUPPORTED

    # And the event is finished rather than left owing, or the sweeper would
    # pick it up on every pass and eventually queue the turn anyway.
    state, error = await _event_state(db_session, wamid)
    assert state == WhatsAppEventState.PROCESSED.value
    assert error is None


async def test_an_ordinary_text_message_still_gets_its_turn(
    db_session: AsyncSession,
    live_redis: Redis,
    working_queue: AgentQueue,
) -> None:
    """The negative control for the four above.

    "Never enqueue" satisfies all of them and breaks the product, so this is
    what says the refusal is about the message type rather than about the path.
    """
    await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"

    outcome = await WhatsAppIngestionService(session=db_session, queue=working_queue).ingest(
        _inbound(wamid, at=datetime.now(UTC))
    )
    await db_session.flush()

    assert outcome.queued == 1
    assert await live_redis.llen(PENDING) == 1


async def test_a_status_event_owes_nothing_and_is_finished_immediately(
    db_session: AsyncSession,
    live_redis: Redis,
    working_queue: AgentQueue,
) -> None:
    """Including a status for a message this deployment never sent.

    That is ordinary traffic - a template sent from Meta's own console - and it
    must not leave an event owing work for ever, because the sweeper would then
    pick it up on every pass and the backlog gauge would never reach zero.
    """
    await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "statuses": [
                                {
                                    "id": wamid,
                                    "status": "delivered",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "recipient_id": CUSTOMER,
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    outcome = await WhatsAppIngestionService(session=db_session, queue=working_queue).ingest(
        payload
    )
    await db_session.flush()

    assert outcome.stored == 1
    state, error = await _event_state(db_session, f"{wamid}:delivered")
    assert state == WhatsAppEventState.PROCESSED.value
    assert error is None
    # Nothing to answer, so nothing was queued and no placeholder was invented.
    assert await live_redis.llen(PENDING) == 0
    assert (
        await db_session.execute(select(Conversation).where(Conversation.contact_id.isnot(None)))
    ).scalars().all() == []
    assert (
        await db_session.execute(select(Contact).where(Contact.wa_id == CUSTOMER))
    ).scalars().all() == []
