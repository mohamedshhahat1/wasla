"""A job that reaches a worker before its creating transaction commits.

Every Redis queue in this system is fed from *inside* an uncommitted
transaction. The webhook handler stores a message, enqueues an agent job and
returns; `CommittingRoute` commits afterwards. The upload handler does the same
with a document. So a worker blocked on `BLMOVE` can receive a job naming a row
that is real, is about to be visible, and is not visible yet - and a scoped
repository answers that with `TenantIsolationError`, which classifies as
`not_found`.

Reproduced before it was fixed, against this same PostgreSQL and this same
Redis:

    API transaction state      : open, uncommitted
    conversation visible to it : 0 rows
    dead-letter depth          : 1
      category=not_found attempts=1
    API transaction state      : COMMITTED
    conversation now visible   : 1 rows
    jobs left to answer it     : 0

    RESULT: the customer is never answered.

The ingestion queue failed identically and left the document at `pending` for
ever with no error on the row. The media queue failed *worse*: it read the
missing row as "the message was deleted", released the job and wrote nothing
down at all, so the photograph was never downloaded, never read and never
answered, and no dead-letter record existed to say so.

**Barriers, not sleeps.** The producer's transaction is held open explicitly
across the worker's attempt, so the ordering these tests assert is the one they
arrange rather than one they hope for. The retry's backoff is skipped by
promoting against a clock moved forward, not by waiting two seconds.

**Real commits, over an engine of this file's own.** The suite's `db_session`
joins the test's transaction as a savepoint, which would make an uncommitted
producer indistinguishable from a committed one - exactly the property under
test.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.models.agent import Agent, AgentStatus
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.knowledge import Document, DocumentStatus, KnowledgeBase
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.workers.ai_worker import AGENT_RETRY, AgentWorker
from app.workers.ingestion_queue import IngestionJob
from app.workers.ingestion_worker import IngestionWorker
from app.workers.media_queue import MediaJob
from app.workers.media_worker import MediaWorker
from app.workers.queue import AgentJob, AgentQueue, ReliableQueue

pytestmark = pytest.mark.integration

# A database of its own, so a run cannot disturb whatever else uses this Redis.
REDIS_URL = "redis://localhost:6379/12"

# Far enough past any backoff either policy can produce, so a promotion is a
# decision rather than a race with the clock.
WELL_PAST_THE_BACKOFF = timedelta(seconds=300)


class _RedisClient:
    """What a worker asks a `RedisClient` for, and nothing else."""

    def __init__(self, client: Redis) -> None:
        self.client = client


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        redis_url=REDIS_URL,
        jwt_secret="queue-visibility-secret-not-for-deployment",
        # Non-empty so the provider clients construct. Nothing in this file
        # reaches a provider: every job fails on the lookup that precedes one.
        openai_api_key="sk-no-request-is-ever-made-from-this-file",
    )


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover - Redis is not running
        await client.aclose()
        pytest.skip("No Redis reachable; these need one to observe a real queue.")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, and one connection per producer."""
    engine = create_async_engine(prepared_database, pool_size=8, max_overflow=4)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    """The worker's own pool, so its sessions cannot see the producer's."""
    instance = Database(_settings(prepared_database))
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest_asyncio.fixture
async def tenant_id(committing: async_sessionmaker[AsyncSession]) -> AsyncIterator[uuid.UUID]:
    """One workspace, removed afterwards along with everything under it."""
    async with committing() as session:
        tenant = Tenant(name="Visibility", slug=f"visibility-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.commit()
        created = tenant.id
    try:
        yield created
    finally:
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == created))
            await session.commit()


# ------------------------------------------------------------------ producers


async def _uncommitted_conversation(
    session: AsyncSession, tenant: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """A conversation the webhook has written and not yet committed."""
    account = WhatsAppAccount(
        tenant_id=tenant,
        phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant, wa_id=f"2012{uuid.uuid4().int % 10**8:08d}")
    agent = Agent(
        tenant_id=tenant,
        name="Sales",
        model="gpt-4.1-mini",
        system_prompt="You answer questions about apartment finishing.",
        status=AgentStatus.ACTIVE,
        is_default=True,
    )
    session.add_all([account, contact, agent])
    await session.flush()

    conversation = Conversation(tenant_id=tenant, contact_id=contact.id, account_id=account.id)
    session.add(conversation)
    await session.flush()

    message = Message(
        tenant_id=tenant,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.TEXT,
        status=MessageStatus.DELIVERED,
        body="Do you finish apartments?",
    )
    session.add(message)
    await session.flush()
    return conversation.id, message.id


async def _uncommitted_document(session: AsyncSession, tenant: uuid.UUID) -> uuid.UUID:
    """A document the upload handler has written and not yet committed."""
    base = KnowledgeBase(tenant_id=tenant, name=f"Handbook {uuid.uuid4().hex[:6]}")
    session.add(base)
    await session.flush()

    document = Document(
        tenant_id=tenant,
        knowledge_base_id=base.id,
        title="Pricing",
        content="A page of prices.",
        content_hash=uuid.uuid4().hex * 2,
        status=DocumentStatus.PENDING,
    )
    session.add(document)
    await session.flush()
    return document.id


async def _uncommitted_media(session: AsyncSession, tenant: uuid.UUID) -> uuid.UUID:
    """An attachment the webhook has written and not yet committed."""
    conversation_id, message_id = await _uncommitted_conversation(session, tenant)
    media = MessageMedia(
        tenant_id=tenant,
        message_id=message_id,
        conversation_id=conversation_id,
        wa_media_id=f"wamid-{uuid.uuid4().hex[:10]}",
        mime_type="image/png",
        status=MediaStatus.PENDING,
    )
    session.add(media)
    await session.flush()
    return media.id


async def _promote(queue: ReliableQueue) -> int:
    """Make a scheduled retry due, without waiting out its backoff."""
    return await queue.promote_due(now=datetime.now(UTC) + WELL_PAST_THE_BACKOFF)


# --------------------------------------------------------------- agent queue


async def test_an_agent_job_arriving_before_the_commit_is_retried(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> None:
    worker = AgentWorker(database=database, redis=_RedisClient(redis), settings=_settings(""))  # type: ignore[arg-type]
    producer = committing()
    try:
        conversation_id, _ = await _uncommitted_conversation(producer, tenant_id)
        await worker.queue.enqueue(AgentJob(tenant_id=tenant_id, conversation_id=conversation_id))

        assert await worker.run_once(wait_seconds=1) is True

        assert await worker.queue.failed_depth() == 0
        assert await worker.queue.delayed_depth() == 1
    finally:
        await producer.rollback()
        await producer.close()


async def test_an_agent_job_succeeds_once_its_creator_commits(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole sequence, and the count that matters is one reply.

    A retry that answered twice would be a worse defect than the one being
    fixed, so the send is counted rather than merely prevented.
    """
    from app.agents.orchestrator import AgentOrchestrator, AgentOutcome
    from app.integrations.openai.types import TokenUsage
    from app.services.messaging_service import MessagingService

    answered: list[uuid.UUID] = []
    sent: list[str] = []

    async def compose(
        self: object, *, conversation_id: uuid.UUID, **kwargs: object
    ) -> AgentOutcome:
        answered.append(conversation_id)
        return AgentOutcome(
            reply="We do.",
            handed_off=False,
            tools_run=(),
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            rounds=1,
            model="stub",
        )

    async def send(self: object, *, conversation_id: uuid.UUID, body: str) -> None:
        sent.append(body)

    monkeypatch.setattr(AgentOrchestrator, "answer", compose)
    monkeypatch.setattr(MessagingService, "send_text", send)

    worker = AgentWorker(database=database, redis=_RedisClient(redis), settings=_settings(""))  # type: ignore[arg-type]
    producer = committing()
    conversation_id, _ = await _uncommitted_conversation(producer, tenant_id)
    await worker.queue.enqueue(AgentJob(tenant_id=tenant_id, conversation_id=conversation_id))

    await worker.run_once(wait_seconds=1)
    assert answered == []
    assert await worker.queue.delayed_depth() == 1

    await producer.commit()
    await producer.close()

    assert await _promote(worker.queue) == 1
    assert await worker.run_once(wait_seconds=1) is True

    assert answered == [conversation_id]
    assert sent == ["We do."]
    assert await worker.queue.failed_depth() == 0
    assert await worker.queue.depth() == 0
    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.inflight_depth() == 0


async def test_a_conversation_that_never_existed_dead_letters_boundedly(
    redis: Redis,
    database: Database,
    tenant_id: uuid.UUID,
) -> None:
    """Two attempts, not three, and not for ever."""
    worker = AgentWorker(database=database, redis=_RedisClient(redis), settings=_settings(""))  # type: ignore[arg-type]
    await worker.queue.enqueue(AgentJob(tenant_id=tenant_id, conversation_id=uuid.uuid4()))

    await worker.run_once(wait_seconds=1)
    await _promote(worker.queue)
    await worker.run_once(wait_seconds=1)

    assert await worker.queue.failed_depth() == 1
    assert await worker.queue.delayed_depth() == 0
    assert await worker.queue.depth() == 0

    record = json.loads((await worker.queue.dead_letters(limit=1))[0])
    assert record["category"] == "not_found"
    assert record["attempts"] == 2
    assert record["attempts"] < AGENT_RETRY.max_attempts


async def test_the_retried_agent_job_keeps_its_workspace_and_conversation(
    redis: Redis,
    database: Database,
    tenant_id: uuid.UUID,
) -> None:
    worker = AgentWorker(database=database, redis=_RedisClient(redis), settings=_settings(""))  # type: ignore[arg-type]
    conversation_id = uuid.uuid4()
    await worker.queue.enqueue(AgentJob(tenant_id=tenant_id, conversation_id=conversation_id))

    await worker.run_once(wait_seconds=1)

    (scheduled,) = await redis.zrange("agent:jobs:delayed", 0, -1)
    envelope = json.loads(scheduled)
    assert envelope["attempt"] == 2
    assert envelope["last_failure"] == "not_found"
    retried = AgentJob.decode(envelope["body"])
    assert retried.tenant_id == tenant_id
    assert retried.conversation_id == conversation_id


# ----------------------------------------------------------- ingestion queue


async def test_an_ingestion_job_arriving_before_the_commit_is_retried(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> None:
    """The same race on the queue the audit did not name.

    A document uploaded successfully was dead-lettered on attempt one and left
    at `pending` for ever, with no error on the row to explain it.
    """
    worker = IngestionWorker(
        database=database,
        redis=_RedisClient(redis),  # type: ignore[arg-type]
        settings=_settings(""),
    )
    producer = committing()
    try:
        document_id = await _uncommitted_document(producer, tenant_id)
        await worker.queue.enqueue(IngestionJob(tenant_id=tenant_id, document_id=document_id))

        assert await worker.run_once(wait_seconds=1) is True

        assert await worker.queue.failed_depth() == 0
        assert await worker.queue.delayed_depth() == 1
    finally:
        await producer.rollback()
        await producer.close()


async def test_a_document_that_never_existed_dead_letters_boundedly(
    redis: Redis,
    database: Database,
    tenant_id: uuid.UUID,
) -> None:
    worker = IngestionWorker(
        database=database,
        redis=_RedisClient(redis),  # type: ignore[arg-type]
        settings=_settings(""),
    )
    await worker.queue.enqueue(IngestionJob(tenant_id=tenant_id, document_id=uuid.uuid4()))

    await worker.run_once(wait_seconds=1)
    await _promote(worker.queue)
    await worker.run_once(wait_seconds=1)

    record = json.loads((await worker.queue.dead_letters(limit=1))[0])
    assert record["category"] == "not_found"
    assert record["attempts"] == 2
    assert await worker.queue.delayed_depth() == 0


# --------------------------------------------------------------- media queue


async def test_a_media_job_arriving_before_the_commit_is_retried_not_discarded(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> None:
    """This queue used to lose the job silently, which is worse than a dead letter.

    A missing row was read as "the message was deleted", the job was released
    and nothing was written down - so the attachment was never downloaded,
    never read, and the agent job that a finished attachment releases was never
    enqueued either.
    """
    worker = MediaWorker(
        database=database,
        redis=_RedisClient(redis),  # type: ignore[arg-type]
        settings=_settings(""),
    )
    producer = committing()
    try:
        media_id = await _uncommitted_media(producer, tenant_id)
        await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))

        assert await worker.run_once(wait_seconds=1) is True

        assert await worker.queue.failed_depth() == 0
        assert await worker.queue.delayed_depth() == 1
        # And nothing was queued for an agent to answer a file nobody read.
        assert await AgentQueue(redis).depth() == 0
    finally:
        await producer.rollback()
        await producer.close()


async def test_an_attachment_that_never_existed_dead_letters_boundedly(
    redis: Redis,
    database: Database,
    tenant_id: uuid.UUID,
) -> None:
    worker = MediaWorker(
        database=database,
        redis=_RedisClient(redis),  # type: ignore[arg-type]
        settings=_settings(""),
    )
    await worker.queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=uuid.uuid4()))

    await worker.run_once(wait_seconds=1)
    await _promote(worker.queue)
    await worker.run_once(wait_seconds=1)

    record = json.loads((await worker.queue.dead_letters(limit=1))[0])
    assert record["category"] == "not_found"
    assert record["attempts"] == 2


# ------------------------------------------------------------ tenant isolation


async def test_another_workspaces_conversation_is_still_not_found(
    redis: Redis,
    database: Database,
    committing: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> None:
    """The retry asks the same scoped question again, not a wider one.

    A committed conversation belonging to somebody else must answer `not_found`
    on both attempts and dead-letter, exactly as a row that does not exist
    does - the one-shot door widens the retry budget, never the query.
    """
    async with committing() as owner:
        other = Tenant(name="Somebody else", slug=f"other-{uuid.uuid4().hex[:8]}")
        owner.add(other)
        await owner.flush()
        conversation_id, _ = await _uncommitted_conversation(owner, other.id)
        await owner.commit()
        other_id = other.id

    try:
        worker = AgentWorker(database=database, redis=_RedisClient(redis), settings=_settings(""))  # type: ignore[arg-type]
        # The job names *this* workspace and the other workspace's conversation.
        await worker.queue.enqueue(AgentJob(tenant_id=tenant_id, conversation_id=conversation_id))

        await worker.run_once(wait_seconds=1)
        await _promote(worker.queue)
        await worker.run_once(wait_seconds=1)

        record = json.loads((await worker.queue.dead_letters(limit=1))[0])
        assert record["category"] == "not_found"
        assert record["tenant_id"] == str(tenant_id)
    finally:
        async with committing() as cleanup:
            await cleanup.execute(delete(Tenant).where(Tenant.id == other_id))
            await cleanup.commit()


# ---------------------------------------------------- the producers themselves


def test_the_environment_agrees_this_is_worth_testing() -> None:
    """A guard against the fixtures quietly stopping.

    `prepared_database` skips without PostgreSQL and `redis` skips without
    Redis, which is correct locally and would be a silent hole in CI. CI's
    skip policy is what catches that, and this states the two variables it
    depends on so the reason is findable from here.
    """
    assert os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
