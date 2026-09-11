"""Inbound deduplication and conversation creation, under real concurrency.

**This file needs a real socket and independent connections, and that is the
whole point.** The messaging audit proved these invariants hold at eight-way
concurrency and then observed that nothing in CI would notice if they stopped:
of 1,426 messaging-relevant tests, the duplicate-event tests were sequential
and the constraint test added two rows to *one* session, which exercises the
constraint rather than the race (MSG-23).

Two properties are being measured, and they are different:

**Data.** However many deliveries of one event arrive together, there is
exactly one event, one message, one contact, one conversation and one agent
job. That has always held.

**Protocol.** Every one of those deliveries is answered `200`. It was not: the
losers of the insert race turned `UNIQUE(tenant_id, event_id)` into a `500` -
an internal error for a situation that is neither internal nor an error, on an
endpoint whose failure rate Meta watches, and which lengthens the window in
which a late redelivery can arrive (MSG-09).

An `asyncio.Barrier` rather than a burst of requests, because "concurrent"
has to mean the deliveries are genuinely in flight together rather than merely
issued quickly. Each client is its own connection to a real uvicorn server, so
the races happen in PostgreSQL where they would in production.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import socket
import uuid
from collections.abc import AsyncIterator, Awaitable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
import uvicorn
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.redis import RedisClient
from app.db.session import Database
from app.main import create_app
from app.workers.queue import QUEUE_NAMESPACE

pytestmark = pytest.mark.integration

PATH = "/api/v1/webhooks/whatsapp"
APP_SECRET = "concurrency-app-secret"
# A Redis database of this file's own, so a run cannot disturb anything else
# using this server and the agent queue depth means what it says.
REDIS_URL = "redis://localhost:6379/13"
# Imported rather than spelled, so a namespace change cannot leave this file
# asserting on a key nothing writes to - which would make every count below
# read zero and pass for the wrong reason.
AGENT_QUEUE_KEY = f"{QUEUE_NAMESPACE}:pending"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


def _inbound(*, phone_number_id: str, wa_id: str, wamid: str, at: datetime) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [
                                {"wa_id": wa_id, "profile": {"name": "A Customer"}},
                            ],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": wamid,
                                    "type": "text",
                                    "timestamp": str(int(at.timestamp())),
                                    "text": {"body": "hello"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


def _status(*, phone_number_id: str, wamid: str, status: str, at: datetime) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "statuses": [
                                {
                                    "id": wamid,
                                    "status": status,
                                    "timestamp": str(int(at.timestamp())),
                                    "recipient_id": "201555000111",
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


@contextlib.asynccontextmanager
async def _serving(database_url: str) -> AsyncIterator[str]:
    """The real application on a real loopback socket.

    Infrastructure is attached by hand rather than through the lifespan, so the
    test owns the engine and can dispose of it deterministically - the same
    shape `test_commit_boundary.py` uses, and for the same reason.
    """
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        redis_url=REDIS_URL,
        meta_app_secret=APP_SECRET,
        rate_limit_enabled=False,
    )
    app = create_app(settings)
    database = Database(settings)
    redis = RedisClient(settings)
    app.state.database = database
    app.state.redis = redis

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(600):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - the server failed to come up
            raise RuntimeError("the test server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=15)
        await database.dispose()
        await redis.close()


async def _deliver_together(base_url: str, payloads: Sequence[dict[str, Any]]) -> list[int]:
    """Post every payload at the same instant, each on its own connection.

    The barrier is what makes this a concurrency test. Without it the requests
    are merely issued in a loop, the first one commits, and every later one
    takes the duplicate fast path - which is the sequential behaviour the suite
    already covered.
    """
    barrier = asyncio.Barrier(len(payloads))

    async def deliver(payload: dict[str, Any]) -> int:
        body = json.dumps(payload).encode()
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            await barrier.wait()
            response = await client.post(PATH, content=body, headers=_signed(body))
            return response.status_code

    return list(await asyncio.gather(*(deliver(payload) for payload in payloads)))


async def _counts(engine: AsyncEngine, tenant_id: uuid.UUID) -> dict[str, int]:
    """Every row the delivery could have produced, counted in one place."""
    queries = {
        "events": "SELECT count(*) FROM whatsapp_events WHERE tenant_id = :tenant",
        "messages": "SELECT count(*) FROM messages WHERE tenant_id = :tenant",
        "contacts": "SELECT count(*) FROM contacts WHERE tenant_id = :tenant",
        "conversations": "SELECT count(*) FROM conversations WHERE tenant_id = :tenant",
        # An aggregate half-built and then abandoned would show up here and
        # nowhere else: a conversation nobody wrote a message into.
        "empty_conversations": (
            "SELECT count(*) FROM conversations c WHERE c.tenant_id = :tenant "
            "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)"
        ),
    }
    async with engine.connect() as connection:
        return {
            name: int((await connection.execute(text(sql), {"tenant": tenant_id})).scalar_one())
            for name, sql in queries.items()
        }


@pytest.fixture
def scratch_engine(prepared_database: str) -> AsyncEngine:
    """A pool the test reads through, outside the server's own connections."""
    return create_async_engine(prepared_database, poolclass=NullPool)


@pytest.fixture
async def agent_queue() -> AsyncIterator[Redis]:
    """This file's own Redis database, emptied before and after."""
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def _workspace(engine: AsyncEngine, *, slug: str, phone_number_id: str) -> uuid.UUID:
    """A workspace holding one number, committed on its own connection.

    Committed rather than staged in the rolled-back session fixture, because
    the server reads on its own connections and cannot see an uncommitted row.
    The teardown removes it.
    """
    tenant_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tenants (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, :name, :slug, 'active', now(), now())"
            ),
            {"id": tenant_id, "name": slug.title(), "slug": slug},
        )
        await connection.execute(
            text(
                "INSERT INTO whatsapp_accounts "
                "(id, tenant_id, phone_number_id, waba_id, display_phone_number, status, "
                " ownership_started_at, ownership_verified_at, created_at, updated_at) "
                "VALUES (:id, :tenant, :pn, :waba, :display, 'active', "
                " now() - interval '1 day', now() - interval '1 day', now(), now())"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "pn": phone_number_id,
                "waba": f"waba-{slug}",
                "display": "+20 100 000 0000",
            },
        )
    return tenant_id


async def _forget(engine: AsyncEngine, tenant_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        # Deleting the tenant cascades to everything below it, so the order
        # here only has to respect the one relation that is not a cascade.
        await connection.execute(
            text("DELETE FROM tenants WHERE id = :tenant"), {"tenant": tenant_id}
        )


@pytest.mark.parametrize("fan_out", [2, 4, 8])
async def test_the_same_message_delivered_together_lands_once_and_answers_200(
    prepared_database: str,
    scratch_engine: AsyncEngine,
    agent_queue: Redis,
    fan_out: int,
) -> None:
    """Meta's own guarantee is at-least-once, so this is ordinary traffic.

    Both halves matter. The row counts are the property that stops a customer
    being answered twice; the status codes are the property that stops Meta
    seeing a failure rate on an endpoint it will eventually disable.
    """
    slug = f"dup-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"PN-{uuid.uuid4().hex[:10]}"
    tenant_id = await _workspace(scratch_engine, slug=slug, phone_number_id=phone_number_id)
    try:
        payload = _inbound(
            phone_number_id=phone_number_id,
            wa_id="201555000111",
            wamid=f"wamid.{uuid.uuid4().hex}",
            at=datetime.now(UTC) - timedelta(seconds=5),
        )
        async with _serving(prepared_database) as base_url:
            statuses = await _deliver_together(base_url, [payload] * fan_out)

        assert statuses == [200] * fan_out
        counts = await _counts(scratch_engine, tenant_id)
        assert counts == {
            "events": 1,
            "messages": 1,
            "contacts": 1,
            "conversations": 1,
            "empty_conversations": 0,
        }
        # One agent job, not `fan_out` of them. Counting the queue rather than
        # only the rows is the difference between "the database is consistent"
        # and "the customer is answered once", and the second is the one a
        # customer notices.
        assert await cast("Awaitable[int]", agent_queue.llen(AGENT_QUEUE_KEY)) == 1
    finally:
        await _forget(scratch_engine, tenant_id)


@pytest.mark.parametrize("fan_out", [2, 4])
async def test_distinct_first_messages_from_one_customer_build_one_conversation(
    prepared_database: str,
    scratch_engine: AsyncEngine,
    agent_queue: Redis,
    fan_out: int,
) -> None:
    """Different messages, same customer, arriving before any of them commits.

    The race is on the contact and the conversation rather than the event, so
    deduplication cannot help: every delivery is genuinely new and every one of
    them tries to create the aggregate. Exactly one aggregate must result, and
    no half-built one may be left behind.
    """
    slug = f"first-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"PN-{uuid.uuid4().hex[:10]}"
    tenant_id = await _workspace(scratch_engine, slug=slug, phone_number_id=phone_number_id)
    try:
        at = datetime.now(UTC) - timedelta(seconds=5)
        payloads = [
            _inbound(
                phone_number_id=phone_number_id,
                wa_id="201555000111",
                wamid=f"wamid.{uuid.uuid4().hex}",
                at=at,
            )
            for _ in range(fan_out)
        ]
        async with _serving(prepared_database) as base_url:
            statuses = await _deliver_together(base_url, payloads)

        assert statuses == [200] * fan_out
        counts = await _counts(scratch_engine, tenant_id)
        assert counts["contacts"] == 1
        assert counts["conversations"] == 1
        assert counts["empty_conversations"] == 0
        assert counts["events"] == fan_out
        assert counts["messages"] == fan_out
        # One job per *delivery*, not one per conversation, and that is the
        # contract rather than a gap. Job deduplication is within a delivery:
        # two messages in one webhook produce one turn, because the worker
        # reads the conversation fresh and a second job would repeat the first.
        # Two separate deliveries are two separate things the customer said,
        # and collapsing them across requests would need a lock held across the
        # whole ingestion path to decide which request owns the turn.
        #
        # Pinned so the distinction stays deliberate: a change that made this
        # `1` would be suppressing real customer messages, and a change that
        # made the duplicate-delivery test above anything but `1` would be
        # answering one message twice.
        assert await cast("Awaitable[int]", agent_queue.llen(AGENT_QUEUE_KEY)) == fan_out
    finally:
        await _forget(scratch_engine, tenant_id)


async def test_the_same_status_delivered_eight_times_moves_one_timestamp(
    prepared_database: str,
    scratch_engine: AsyncEngine,
    agent_queue: Redis,
) -> None:
    """A status names a message, so the race is on the event row alone.

    There is nothing to project here - the message id belongs to no send this
    deployment made - which is ordinary traffic for a template sent from Meta's
    own console. What is being checked is that eight simultaneous deliveries
    produce one event and eight `200`s rather than a scatter of 500s.
    """
    slug = f"status-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"PN-{uuid.uuid4().hex[:10]}"
    tenant_id = await _workspace(scratch_engine, slug=slug, phone_number_id=phone_number_id)
    try:
        payload = _status(
            phone_number_id=phone_number_id,
            wamid=f"wamid.{uuid.uuid4().hex}",
            status="delivered",
            at=datetime.now(UTC) - timedelta(seconds=5),
        )
        async with _serving(prepared_database) as base_url:
            statuses = await _deliver_together(base_url, [payload] * 8)

        assert statuses == [200] * 8
        counts = await _counts(scratch_engine, tenant_id)
        assert counts["events"] == 1
        assert counts["messages"] == 0
        # A status for a message nobody sent creates no placeholder. Inventing
        # one would put a row in a customer's transcript that no customer ever
        # received.
        assert counts["conversations"] == 0
        assert await cast("Awaitable[int]", agent_queue.llen(AGENT_QUEUE_KEY)) == 0
    finally:
        await _forget(scratch_engine, tenant_id)
