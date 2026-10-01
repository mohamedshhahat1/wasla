"""Every live agent job names the customer message it answers.

Found on 2026-10-01 while remediating the omnichannel audit, at `354db53`: the
webhook path read a new message's id before the row had been flushed. The id
is generated at insert, so for every plain text message the agent job carried
no trigger. Since TOOL-17 the worker refuses a job without a trigger as
malformed and dead-letters it without a retry, and the inbound event had
already been marked processed, so the recovery sweeper never looked at it
again. The customer's message was stored and never answered.

A message carrying a file was unaffected - that path flushes before it reads
the id - which is why the only suite asserting a trigger on a webhook job
(`test_media_parser_containment.py`) kept passing.

Both halves are pinned: the job the webhook hands off names the stored
message, and the worker that takes it claims the turn rather than
dead-lettering it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.redis import RedisClient
from app.db.models.agent_turn import AgentTurn
from app.db.models.conversation import Message
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.main import create_app
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.ai_worker import AgentWorker
from app.workers.queue import AgentJob, AgentQueue
from tests.redis_url import redis_url_for

pytestmark = pytest.mark.integration

PATH = "/api/v1/webhooks/whatsapp"
APP_SECRET = "live-turn-identity-app-secret"
# A Redis database of this file's own: the worker below reads the queue the
# webhook wrote, and nothing else may share it.
REDIS_URL = redis_url_for(10)
CUSTOMER = "201000000310"


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[AgentJob] = []

    async def enqueue(self, job: AgentJob) -> None:
        self.jobs.append(job)


def _text_delivery(phone_number_id: str, wamid: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-live-turn",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Mona"}}],
                            "messages": [
                                {
                                    "from": CUSTOMER,
                                    "id": wamid,
                                    "type": "text",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "text": {"body": "Do you deliver on Fridays?"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def test_a_text_message_hands_off_a_turn_naming_that_message(
    db_session: AsyncSession,
) -> None:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Live {tag}", slug=f"live-{tag}")
    db_session.add(tenant)
    await db_session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-live-{tag}",
        waba_id=f"waba-live-{tag}",
        display_phone_number="+20 100 000 0310",
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(account)
    await db_session.flush()
    wamid = f"wamid.{uuid.uuid4().hex}"
    queue = RecordingQueue()

    outcome = await WhatsAppIngestionService(
        session=db_session, queue=cast("AgentQueue", queue)
    ).ingest(_text_delivery(account.phone_number_id, wamid))

    assert (outcome.stored, outcome.queued) == (1, 1)
    stored = await db_session.scalar(select(Message.id).where(Message.wa_message_id == wamid))
    assert stored is not None
    (job,) = queue.jobs
    assert job.trigger_message_id == stored


@pytest.fixture
def scratch_engine(prepared_database: str) -> AsyncEngine:
    return create_async_engine(prepared_database, poolclass=NullPool)


@pytest.fixture
async def queue_redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def test_a_live_text_turn_is_claimed_rather_than_dead_lettered(
    prepared_database: str, scratch_engine: AsyncEngine, queue_redis: Redis
) -> None:
    """The signed route, the queue it writes and the worker that reads it - the live path."""
    tenant_id = uuid.uuid4()
    phone_number_id = f"PN-live-{uuid.uuid4().hex[:10]}"
    async with scratch_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tenants (id, name, slug, status, created_at, updated_at) "
                "VALUES (:id, 'Live turn', :slug, 'active', now(), now())"
            ),
            {"id": tenant_id, "slug": f"live-{tenant_id.hex[:10]}"},
        )
        await connection.execute(
            text(
                "INSERT INTO whatsapp_accounts "
                "(id, tenant_id, phone_number_id, waba_id, display_phone_number, status, "
                " ownership_started_at, ownership_verified_at, created_at, updated_at) "
                "VALUES (:id, :tenant, :pn, 'waba-live', '+20 100 000 0311', 'active', "
                " now() - interval '1 day', now() - interval '1 day', now(), now())"
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id, "pn": phone_number_id},
        )
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=prepared_database,
        redis_url=REDIS_URL,
        meta_app_secret=APP_SECRET,
        rate_limit_enabled=False,
    )
    database = Database(settings)
    redis = RedisClient(settings)
    app = create_app(settings)
    app.state.database = database
    app.state.redis = redis
    wamid = f"wamid.{uuid.uuid4().hex}"
    try:
        body = json.dumps(_text_delivery(phone_number_id, wamid)).encode()
        digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                PATH,
                content=body,
                headers={
                    "X-Hub-Signature-256": f"sha256={digest}",
                    "Content-Type": "application/json",
                },
            )
        assert response.status_code == 200

        worker = AgentWorker(database=database, redis=redis, settings=settings)
        assert await worker.run_once(wait_seconds=1) is True

        categories = [json.loads(entry)["category"] for entry in await worker.queue.dead_letters()]
        assert "malformed" not in categories
        async with scratch_engine.connect() as connection:
            message_id = (
                await connection.execute(select(Message.id).where(Message.wa_message_id == wamid))
            ).scalar_one()
            claimed = (
                await connection.execute(
                    select(AgentTurn.id).where(
                        AgentTurn.tenant_id == tenant_id,
                        AgentTurn.trigger_message_id == message_id,
                    )
                )
            ).scalar_one_or_none()
        assert claimed is not None
    finally:
        await database.dispose()
        await redis.close()
        async with scratch_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"), {"tenant": tenant_id}
            )
        await scratch_engine.dispose()
