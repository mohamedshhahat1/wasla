"""The media signals an operator alerts on are really emitted (MEDIA-15).

Against real Redis for the cross-process counters and real PostgreSQL for the
gauges, rendered through the same `MetricsService` `/metrics` uses. Each test
proves a series is present with the value its scenario implies - a metric that
exists only in the catalogue is exactly the failure this suite is for.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.metrics import MetricsRegistry
from app.core.telemetry import MEDIA_OUTCOMES, set_counter_sink
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.media_purge import MediaPurgeObject
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.integrations.openai.transcription import TranscriptionClient
from app.services.media_horizons import claim_lease
from app.services.media_outcomes import MediaReason
from app.services.metrics_service import MetricsService
from tests import media_harness as h

pytestmark = pytest.mark.integration

REDIS_URL = "redis://localhost:6379/11"


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def sink(redis: Redis) -> Iterator[Redis]:
    set_counter_sink(redis)
    try:
        yield redis
    finally:
        set_counter_sink(None)


def _sample(exposition: str, name: str) -> float | None:
    """An unlabelled gauge's value."""
    for line in exposition.splitlines():
        if line.startswith((f"{name} ", f"{name}{{}} ")):
            return float(line.rsplit(" ", 1)[1])
    return None


def _counter(exposition: str, name: str, outcome: str) -> float | None:
    for line in exposition.splitlines():
        if line.startswith(f"{name}{{") and f'outcome="{outcome}"' in line:
            return float(line.rsplit(" ", 1)[1])
    return None


def test_the_outcome_label_domain_is_exactly_the_reason_vocabulary() -> None:
    assert frozenset({reason.value for reason in MediaReason} | {"ready"}) == MEDIA_OUTCOMES


async def test_each_terminal_outcome_is_counted_under_its_reason(
    sink: Redis, db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    ready = await h.attachment(db_session, where)
    await h.run(h.worker(db_session, tmp_path, settings, reader=h.StubReader()), ready)
    video = await h.attachment(db_session, where, mime_type="video/mp4")
    await h.run(
        h.worker(
            db_session,
            tmp_path,
            settings,
            whatsapp=h.StubWhatsApp(mime_type="video/mp4"),
            reader=h.StubReader(),
        ),
        video,
    )

    exposition = await MetricsService(sink, registry=MetricsRegistry()).render()

    assert _counter(exposition, "wasla_media_outcomes_total", "ready") == 1.0
    assert _counter(exposition, "wasla_media_outcomes_total", "unsupported_type") == 1.0


async def test_a_transcription_call_is_counted_as_a_provider_call(sink: Redis) -> None:
    responses = iter([httpx.Response(503), httpx.Response(200, json={"text": "hello"})])

    async def no_sleep(_: float) -> None:
        return None

    client = TranscriptionClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(responses))),
        api_key="sk-sentinel",
        model="whisper-1",
        sleep=no_sleep,
    )
    await client.transcribe(content=b"OggS" + b"0" * 32, mime_type="audio/ogg")

    exposition = await MetricsService(sink, registry=MetricsRegistry()).render()
    finals = [
        line
        for line in exposition.splitlines()
        if line.startswith("wasla_provider_requests_total{") and 'operation="transcribe"' in line
    ]
    attempts = [
        line
        for line in exposition.splitlines()
        if line.startswith("wasla_provider_attempts_total{") and 'operation="transcribe"' in line
    ]
    assert any(
        'outcome="success"' in line and float(line.rsplit(" ", 1)[1]) == 1.0 for line in finals
    )
    assert len(finals) == 1
    assert any('outcome="failure"' in line for line in attempts)
    assert "sk-sentinel" not in exposition


# ------------------------------------------------------------------- gauges


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, pool_size=4, max_overflow=2)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_stranded_media_and_owed_purge_deletes_are_gauged(
    committing: async_sessionmaker[AsyncSession], prepared_database: str
) -> None:
    settings = Settings(_env_file=None, environment="test", database_url=prepared_database)
    now = datetime.now(UTC)
    async with committing() as session:
        tenant = Tenant(name="Gauged", slug=f"gauged-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        account = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"pn-{uuid.uuid4().hex[:8]}",
            waba_id="555000111",
            display_phone_number="+201000000000",
        )
        contact = Contact(tenant_id=tenant.id, wa_id="201234567890")
        session.add_all([account, contact])
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, contact_id=contact.id, account_id=account.id
        )
        session.add(conversation)
        await session.flush()
        for claimed_at in (now, now - claim_lease(settings) - timedelta(minutes=10)):
            message = Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                wa_message_id=f"wamid.{uuid.uuid4().hex}",
                direction=MessageDirection.INBOUND,
                kind=MessageKind.IMAGE,
                status=MessageStatus.RECEIVED,
                origin=MessageOrigin.CUSTOMER,
            )
            session.add(message)
            await session.flush()
            session.add(
                MessageMedia(
                    tenant_id=tenant.id,
                    message_id=message.id,
                    conversation_id=conversation.id,
                    wa_media_id="m",
                    status=MediaStatus.DOWNLOADING,
                    claim_id=uuid.uuid4(),
                    claimed_at=claimed_at,
                    attempts=1,
                )
            )
        session.add(
            MediaPurgeObject(
                tenant_id=tenant.id,
                storage_key=f"{tenant.id}/2026/09/{uuid.uuid4()}.png",
                not_before=now,
                attempts=3,
                created_at=now - timedelta(hours=7),
            )
        )
        await session.commit()
        tenant_id = tenant.id

    database = Database(settings)
    try:
        exposition = await MetricsService(
            None, registry=MetricsRegistry(), database=database, settings=settings
        ).render(now=now)
    finally:
        await database.dispose()
        async with committing() as session:
            await session.execute(
                delete(MediaPurgeObject).where(MediaPurgeObject.tenant_id == tenant_id)
            )
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.commit()

    # One live claim, one abandoned: only the second is stranded.
    assert _sample(exposition, "wasla_media_stranded") == 1.0
    stranded_age = _sample(exposition, "wasla_media_stranded_oldest_age_seconds")
    assert stranded_age is not None and stranded_age > claim_lease(settings).total_seconds()
    assert _sample(exposition, "wasla_media_purge_deletes_owed") == 1.0
    owed_age = _sample(exposition, "wasla_media_purge_deletes_oldest_age_seconds")
    assert owed_age is not None and owed_age >= 7 * 3600 - 5
