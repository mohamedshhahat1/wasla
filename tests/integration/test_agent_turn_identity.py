"""One inbound message, one agent turn, however many envelopes carry it.

The queue is at-least-once by design and that is not being changed here. What is
being asserted is the thing the queue cannot assert for itself: that two
envelopes naming one customer message produce **one** logical turn, and that the
saving is taken before the expensive, billable, externally-visible part of the
turn rather than after it (WQ-01).

The distinction matters enough to be the point of this file. An idempotency key
on the final send alone would make `whatsapp_sends == 1` true while leaving two
sentiment classifications, two inferences, two tool loops and two bills - a
customer who is no longer answered twice, and a workspace still charged twice for
being nearly answered twice. So every assertion below counts provider calls, not
just messages.

Real PostgreSQL and real Redis. The two outbound hosts are the only things
stubbed, because what has to be counted is requests that genuinely left.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.redis import RedisClient
from app.db.models.agent import Agent, AgentStatus
from app.db.models.agent_turn import AgentTurn, AgentTurnState
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import (
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)
from app.db.session import Database
from app.workers.ai_worker import AgentWorker
from app.workers.inbound_recovery import InboundRecoveryWorker
from app.workers.queue import AgentJob, AgentQueue

pytestmark = pytest.mark.integration

REDIS_URL = "redis://localhost:6379/13"
REPLY = "Yes, we are here."


class _Provider:
    """Counts what genuinely left the process, split by which API it went to.

    Both hosts share one transport so the counts cannot drift apart: a test that
    stubbed them separately could pass while one of them was never reached.
    """

    def __init__(self) -> None:
        self.sentiment = 0
        self.inference = 0
        self.sends = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "openai" in host:
            body = request.content.decode("utf-8", "replace") if request.content else ""
            if "sentiment" in body.lower():
                self.sentiment += 1
            else:
                self.inference += 1
            return httpx.Response(
                200,
                json={
                    "id": "resp_" + uuid.uuid4().hex[:8],
                    "model": "gpt-4o-mini",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": REPLY}],
                        }
                    ],
                    "usage": {"input_tokens": 11, "output_tokens": 5},
                },
            )
        if "facebook" in host:
            self.sends += 1
            return httpx.Response(
                200,
                json={
                    "messages": [{"id": "wamid.OUT" + uuid.uuid4().hex[:10]}],
                    "contacts": [{"wa_id": "201555000222"}],
                },
            )
        raise AssertionError(f"the turn reached an unexpected host: {host}")


@pytest.fixture
def provider() -> _Provider:
    return _Provider()


@pytest.fixture
def stubbed_hosts(provider: _Provider, monkeypatch: pytest.MonkeyPatch) -> _Provider:
    """Point both outbound clients at the counting transport."""
    import app.integrations.openai.client as openai_client
    import app.services.messaging_service as messaging_module
    import app.workers.ai_worker as ai_worker

    transport = provider.transport()

    def openai(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="https://api.openai.com")

    def whatsapp(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="https://graph.facebook.com")

    monkeypatch.setattr(openai_client, "build_http_client", openai)
    monkeypatch.setattr(ai_worker, "build_http_client", openai)
    for name in ("build_http_client", "build_whatsapp_http_client"):
        if hasattr(messaging_module, name):
            monkeypatch.setattr(messaging_module, name, whatsapp)
    return provider


@pytest.fixture
async def turn_database(prepared_database: str) -> AsyncIterator[Database]:
    """A pool of the workers' own, because they commit on their own connections."""
    database = Database(_settings(prepared_database))
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def live_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


def _settings(url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=url,
        redis_url=REDIS_URL,
        openai_api_key="sk-test-not-real",
        meta_access_token="test-platform-token-not-real",
    )


def _namespace() -> str:
    """A queue of this test's own, so nothing here can see another test's jobs."""
    return f"test:agent-turn:{uuid.uuid4().hex[:10]}"


async def _seed(
    session: AsyncSession,
    *,
    conversations: int = 1,
) -> tuple[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID]]]:
    """A workspace with a live number, a default agent, and N waiting messages.

    Each message gets an event aged past any grace window and left `RECEIVED`,
    which is exactly the state a webhook leaves behind when Redis refused it.
    Returns the workspace and the (conversation, message) pairs.
    """
    tenant = Tenant(name="Turns", slug=f"turns-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-{uuid.uuid4().hex[:8]}",
        waba_id="waba-turns",
        display_phone_number="+20 100 000 0001",
        status=WhatsAppAccountStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=2),
        ownership_verified_at=datetime.now(UTC) - timedelta(days=2),
    )
    session.add(account)
    session.add(
        Agent(
            tenant_id=tenant.id,
            name="Helper",
            is_default=True,
            status=AgentStatus.ACTIVE,
            model="gpt-4o-mini",
            system_prompt="Answer briefly.",
        )
    )
    await session.flush()

    aged = datetime.now(UTC) - timedelta(hours=1)
    pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
    for index in range(conversations):
        # A conversation is unique per (workspace, contact, number), so N
        # conversations need N contacts.
        contact = Contact(
            tenant_id=tenant.id,
            wa_id=f"2015550{index:05d}",
            display_name=f"Nadia {index}",
        )
        session.add(contact)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            contact_id=contact.id,
            account_id=account.id,
            # Inside the service window, so a reply is allowed to leave.
            last_inbound_at=datetime.now(UTC),
            last_message_at=datetime.now(UTC),
        )
        session.add(conversation)
        await session.flush()

        wamid = f"wamid.{uuid.uuid4().hex}"
        message = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            wa_message_id=wamid,
            direction=MessageDirection.INBOUND,
            kind=MessageKind.TEXT,
            body="anyone there?",
            origin=MessageOrigin.CUSTOMER,
        )
        session.add(message)
        event = WhatsAppEvent(
            tenant_id=tenant.id,
            account_id=account.id,
            event_id=wamid,
            kind=WhatsAppEventKind.MESSAGE,
            payload={"seeded": True},
            received_at=aged,
            state=WhatsAppEventState.RECEIVED,
        )
        session.add(event)
        await session.flush()
        await session.execute(
            WhatsAppEvent.__table__.update()
            .where(WhatsAppEvent.id == event.id)
            .values(created_at=aged)
        )
        pairs.append((conversation.id, message.id))

    return tenant.id, pairs


def _sweeper(database: Database, redis: Redis, namespace: str) -> InboundRecoveryWorker:
    settings = _settings(str(database.engine.url))
    worker = InboundRecoveryWorker(
        database=database,
        redis=RedisClient(settings),
        settings=settings,
        # Zero grace, so the test need not wait five minutes for an event it
        # created a moment ago to become claimable.
        grace_seconds=0.0,
        batch_limit=500,
    )
    worker._agent_queue = AgentQueue(redis, namespace=namespace, visibility_timeout_seconds=60)
    return worker


def _agent_worker(database: Database, redis: Redis, namespace: str) -> AgentWorker:
    settings = _settings(str(database.engine.url))
    worker = AgentWorker(database=database, redis=RedisClient(settings), settings=settings)
    worker._queue = AgentQueue(redis, namespace=namespace, visibility_timeout_seconds=60)
    return worker


async def _drain(worker: AgentWorker, *, budget: int) -> int:
    """Consume the queue, and report how many envelopes were actually taken.

    The count is an assertion in its own right: a test that proved one send
    because the second job was never consumed would prove nothing at all.
    """
    consumed = 0
    while consumed < budget and await worker.run_once(wait_seconds=1):
        consumed += 1
    return consumed


async def _outbound(database: Database, tenant_id: uuid.UUID) -> list[str | None]:
    async with database.session() as session:
        return list(
            (
                await session.execute(
                    select(Message.idempotency_key).where(
                        Message.tenant_id == tenant_id,
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _turns(database: Database, tenant_id: uuid.UUID) -> int:
    async with database.session() as session:
        return (
            await session.execute(
                select(func.count()).select_from(AgentTurn).where(AgentTurn.tenant_id == tenant_id)
            )
        ).scalar_one()


async def _cleanup(database: Database, tenant_id: uuid.UUID) -> None:
    """Remove what these tests committed; deleting the workspace cascades."""
    async with database.session() as session:
        await session.execute(Tenant.__table__.delete().where(Tenant.id == tenant_id))


@contextlib.asynccontextmanager
async def _commit_fails(database: Database) -> AsyncIterator[None]:
    """Make the next unit of work publish, then fail where `Database.session` commits.

    The precise shape of WQ-01: the sweeper's jobs are already on the queue when
    the transaction that would have marked them done rolls back, so the events
    stay owed and a later pass publishes them again.
    """
    original = database.session

    @contextlib.asynccontextmanager
    async def failing(*args: object, **kwargs: object) -> AsyncIterator[AsyncSession]:
        session = database.session_factory()
        try:
            yield session
            raise RuntimeError("the sweep published and then could not commit")
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    database.session = failing  # type: ignore[method-assign]
    try:
        yield
    finally:
        database.session = original  # type: ignore[method-assign]


async def test_two_jobs_for_one_message_produce_one_turn_and_one_reply(
    turn_database: Database,
    live_redis: Redis,
    stubbed_hosts: _Provider,
) -> None:
    """The narrow case, stated without any recovery machinery around it.

    Two identical publications of one turn, both consumed by the real worker.
    The assertion is on provider calls, not only on sends: the second job must
    add **zero** inferences, because a turn that is not run is the saving, and a
    turn that is run and then discarded at the send is not.
    """
    namespace = _namespace()
    async with turn_database.session() as session:
        tenant_id, pairs = await _seed(session)
    conversation_id, message_id = pairs[0]

    try:
        queue = AgentQueue(live_redis, namespace=namespace, visibility_timeout_seconds=60)
        job = AgentJob(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            trigger_message_id=message_id,
        )
        await queue.enqueue(job)
        await queue.enqueue(job)

        consumed = await _drain(_agent_worker(turn_database, live_redis, namespace), budget=6)

        assert consumed == 2, "both envelopes must reach the worker, or this proves nothing"
        assert await _turns(turn_database, tenant_id) == 1
        assert stubbed_hosts.inference == 1
        assert stubbed_hosts.sentiment == 1
        assert stubbed_hosts.sends == 1
        assert await _outbound(turn_database, tenant_id) == [f"agent-turn:{message_id}"]
    finally:
        await _cleanup(turn_database, tenant_id)


async def test_the_sweepers_failed_commit_still_answers_the_customer_once(
    turn_database: Database,
    live_redis: Redis,
    stubbed_hosts: _Provider,
) -> None:
    """The audit's own reproduction, end to end, with the expected result inverted.

    Publish, fail the commit, sweep again, then drive both envelopes through the
    real worker. Before this fix the customer got two identical replies and the
    workspace was billed for two turns.
    """
    namespace = _namespace()
    async with turn_database.session() as session:
        tenant_id, _ = await _seed(session)

    try:
        sweeper = _sweeper(turn_database, live_redis, namespace)

        async with _commit_fails(turn_database):
            with pytest.raises(RuntimeError):
                await sweeper.run_once()

        pending = f"{namespace}:pending"
        assert await live_redis.llen(pending) == 1, "the publish must survive the failed commit"

        # A healthy pass finds the event still owed and publishes a second time.
        await sweeper.run_once()
        assert await live_redis.llen(pending) == 2, "the window must genuinely be reproduced"

        consumed = await _drain(_agent_worker(turn_database, live_redis, namespace), budget=6)

        assert consumed == 2
        assert await _turns(turn_database, tenant_id) == 1
        assert stubbed_hosts.inference == 1
        assert stubbed_hosts.sentiment == 1
        assert stubbed_hosts.sends == 1
        assert len(await _outbound(turn_database, tenant_id)) == 1
    finally:
        await _cleanup(turn_database, tenant_id)


async def test_a_whole_batch_losing_its_commit_costs_n_turns_and_not_two_n(
    turn_database: Database,
    live_redis: Redis,
    stubbed_hosts: _Provider,
) -> None:
    """The blast radius, which is what made WQ-01 an incident rather than a blemish.

    `InboundRecoveryWorker` publishes for a whole batch inside one transaction
    and commits once at the end, so a single failed commit duplicated every
    conversation it was holding - up to `BATCH_LIMIT` of them. Eight here rather
    than a hundred: the property is the ratio, and the ratio is visible at eight.
    """
    conversations = 8
    namespace = _namespace()
    async with turn_database.session() as session:
        tenant_id, pairs = await _seed(session, conversations=conversations)

    try:
        sweeper = _sweeper(turn_database, live_redis, namespace)

        async with _commit_fails(turn_database):
            with pytest.raises(RuntimeError):
                await sweeper.run_once()
        await sweeper.run_once()

        pending = f"{namespace}:pending"
        assert await live_redis.llen(pending) == 2 * conversations

        consumed = await _drain(
            _agent_worker(turn_database, live_redis, namespace),
            budget=4 * conversations,
        )

        assert consumed == 2 * conversations, "every duplicate must reach the worker"
        assert await _turns(turn_database, tenant_id) == conversations
        assert stubbed_hosts.inference == conversations
        assert stubbed_hosts.sends == conversations

        keys = await _outbound(turn_database, tenant_id)
        assert len(keys) == conversations
        assert sorted(keys) == sorted(f"agent-turn:{message_id}" for _, message_id in pairs)
    finally:
        await _cleanup(turn_database, tenant_id)


async def test_two_messages_in_one_conversation_are_still_two_turns(
    turn_database: Database,
    live_redis: Redis,
    stubbed_hosts: _Provider,
) -> None:
    """The other half of the guarantee, and the one a cruder fix would break.

    Deduplicating on the conversation would make this test fail by answering
    only the first message. A customer who writes twice is owed two answers; it
    is the *same* message arriving twice that must be answered once.
    """
    namespace = _namespace()
    async with turn_database.session() as session:
        tenant_id, pairs = await _seed(session)
        conversation_id, first_message = pairs[0]
        second = Message(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            wa_message_id=f"wamid.{uuid.uuid4().hex}",
            direction=MessageDirection.INBOUND,
            kind=MessageKind.TEXT,
            body="still there?",
            origin=MessageOrigin.CUSTOMER,
        )
        session.add(second)
        await session.flush()
        second_message = second.id

    try:
        queue = AgentQueue(live_redis, namespace=namespace, visibility_timeout_seconds=60)
        for trigger in (first_message, second_message):
            await queue.enqueue(
                AgentJob(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    trigger_message_id=trigger,
                )
            )

        consumed = await _drain(_agent_worker(turn_database, live_redis, namespace), budget=6)

        assert consumed == 2
        assert await _turns(turn_database, tenant_id) == 2
        assert stubbed_hosts.inference == 2
        assert stubbed_hosts.sends == 2
    finally:
        await _cleanup(turn_database, tenant_id)


async def test_a_turn_that_engaged_is_never_run_again(
    turn_database: Database,
    live_redis: Redis,
    stubbed_hosts: _Provider,
) -> None:
    """The business-level engagement barrier, asserted where it bites.

    A turn recorded as `ENGAGED` may already have reached a provider and may
    already have put a reply on somebody's phone. No later attempt may run it,
    ever - no lease, no adoption, no second opinion. This is `ReservationStage`'s
    judgement (ADR-074) made about the business fact rather than the envelope,
    and it is what stops a replayed or recovered job answering twice.
    """
    namespace = _namespace()
    async with turn_database.session() as session:
        tenant_id, pairs = await _seed(session)
    conversation_id, message_id = pairs[0]

    async with turn_database.session() as session:
        session.add(
            AgentTurn(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                trigger_message_id=message_id,
                state=AgentTurnState.ENGAGED,
                engaged_at=datetime.now(UTC),
            )
        )

    try:
        queue = AgentQueue(live_redis, namespace=namespace, visibility_timeout_seconds=60)
        await queue.enqueue(
            AgentJob(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                trigger_message_id=message_id,
            )
        )

        consumed = await _drain(_agent_worker(turn_database, live_redis, namespace), budget=4)

        assert consumed == 1, "the job must be consumed, not merely left on the queue"
        assert stubbed_hosts.inference == 0
        assert stubbed_hosts.sentiment == 0
        assert stubbed_hosts.sends == 0
        assert await _outbound(turn_database, tenant_id) == []
    finally:
        await _cleanup(turn_database, tenant_id)
