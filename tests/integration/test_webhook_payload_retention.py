"""Raw WhatsApp webhook payloads are kept for a bounded window (DB-011).

Every inbound message and delivery status was stored whole, customer text and
phone numbers included, for the workspace's lifetime. Now a processed event's
payload is cleared once it is older than the retention window, and the event
row stays: its event id still deduplicates Meta's retries.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.storage import LocalMediaStorage
from app.core.telemetry import COUNTER_PREFIX, set_counter_sink
from app.db.models import (
    Tenant,
    WhatsAppAccount,
    WhatsAppEvent,
    WhatsAppEventKind,
    WhatsAppEventState,
)
from app.db.session import Database
from app.repositories import WhatsAppAccountRepository, WhatsAppEventRepository
from app.repositories.whatsapp_repository import WebhookPayloadRetention
from app.workers.retention_worker import RetentionWorker
from tests.fake_queue_redis import FakeQueueRedis
from tests.fakes import as_redis

pytestmark = pytest.mark.integration

# Long before anything else the suite writes, so a sweep in these tests can
# only ever reach rows these tests made.
EPOCH = datetime(2019, 3, 1, 12, 0, tzinfo=UTC)
NOW = EPOCH + timedelta(days=90)
WINDOW = timedelta(days=30)
CUTOFF = NOW - WINDOW
PAYLOAD = {"entry": [{"changes": [{"value": {"messages": [{"text": {"body": "hi"}}]}}]}]}


async def _workspace(session: AsyncSession) -> tuple[Tenant, WhatsAppAccount]:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name="Retention", slug=f"retention-{tag}")
    session.add(tenant)
    await session.flush()
    account = await WhatsAppAccountRepository(session, tenant_id=tenant.id).connect(
        phone_number_id=f"PN{tag}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    await session.flush()
    return tenant, account


async def _event(
    session: AsyncSession,
    tenant: Tenant,
    account: WhatsAppAccount,
    *,
    state: WhatsAppEventState = WhatsAppEventState.PROCESSED,
    processed_at: datetime | None = EPOCH,
    event_id: str | None = None,
) -> WhatsAppEvent:
    event, created = await WhatsAppEventRepository(session, tenant_id=tenant.id).record(
        account_id=account.id,
        event_id=event_id or f"wamid.{uuid.uuid4().hex}",
        kind=WhatsAppEventKind.MESSAGE,
        payload=PAYLOAD,
        received_at=processed_at or EPOCH,
    )
    assert created
    event.state = state
    event.processed_at = processed_at
    await session.flush()
    return event


CLEARED = "cleared"


async def _stored(session: AsyncSession, event: WhatsAppEvent) -> tuple[object, object]:
    """The payload and its redaction moment, as the database holds them.

    A cleared payload must be SQL NULL. The JSON value `null` decodes to None
    too, but it IS NOT NULL: the sweep would count it as still held and clear
    it again for ever - which is exactly what the first version did.
    """
    row = (
        await session.execute(
            text(
                "SELECT payload IS NULL, payload, payload_redacted_at"
                " FROM whatsapp_events WHERE id = :id"
            ),
            {"id": event.id},
        )
    ).one()
    return (CLEARED if row[0] else row[1]), row[2]


# ------------------------------------------------------- what is cleared


async def test_only_old_processed_payloads_are_cleared(db_session: AsyncSession) -> None:
    tenant, account = await _workspace(db_session)
    old = await _event(db_session, tenant, account, processed_at=CUTOFF - timedelta(days=1))
    fresh = await _event(db_session, tenant, account, processed_at=CUTOFF + timedelta(days=1))
    # Never processed, or failed: kept until somebody has dealt with them,
    # however old - recovery and an operator both read the payload.
    stuck = await _event(
        db_session, tenant, account, state=WhatsAppEventState.RECEIVED, processed_at=None
    )
    failed = await _event(
        db_session,
        tenant,
        account,
        state=WhatsAppEventState.FAILED,
        processed_at=CUTOFF - timedelta(days=10),
    )

    retention = WebhookPayloadRetention(db_session)
    assert await retention.pending(older_than=CUTOFF) == 1
    assert await retention.redact(older_than=CUTOFF, now=NOW, limit=100) == 1
    assert await retention.pending(older_than=CUTOFF) == 0

    assert await _stored(db_session, old) == (CLEARED, NOW)
    for kept in (fresh, stuck, failed):
        assert await _stored(db_session, kept) == (PAYLOAD, None)

    # The identity stays: same row, same event id, state, workspace, moment.
    row = (
        await db_session.execute(
            select(
                WhatsAppEvent.event_id,
                WhatsAppEvent.state,
                WhatsAppEvent.tenant_id,
                WhatsAppEvent.processed_at,
            ).where(WhatsAppEvent.id == old.id)
        )
    ).one()
    assert tuple(row) == (old.event_id, WhatsAppEventState.PROCESSED, tenant.id, old.processed_at)


async def test_a_redacted_event_still_deduplicates_metas_retry(db_session: AsyncSession) -> None:
    tenant, account = await _workspace(db_session)
    old = await _event(db_session, tenant, account, event_id="wamid.retried-after-retention")
    assert await WebhookPayloadRetention(db_session).redact(older_than=CUTOFF, now=NOW, limit=100)

    again, created = await WhatsAppEventRepository(db_session, tenant_id=tenant.id).record(
        account_id=account.id,
        event_id="wamid.retried-after-retention",
        kind=WhatsAppEventKind.MESSAGE,
        payload=PAYLOAD,
        received_at=NOW,
    )
    assert created is False
    assert again.id == old.id
    # And the retry does not bring the payload back.
    assert await _stored(db_session, old) == (CLEARED, NOW)


async def test_every_workspace_ages_out_alike(db_session: AsyncSession) -> None:
    """One platform rule: the sweep neither skips nor favours a workspace."""
    a_tenant, a_account = await _workspace(db_session)
    b_tenant, b_account = await _workspace(db_session)
    a_old = await _event(db_session, a_tenant, a_account)
    b_old = await _event(db_session, b_tenant, b_account)
    b_fresh = await _event(db_session, b_tenant, b_account, processed_at=NOW - timedelta(days=1))

    retention = WebhookPayloadRetention(db_session)
    assert await retention.redact(older_than=CUTOFF, now=NOW, limit=100) == 2
    assert (await _stored(db_session, a_old))[0] == CLEARED
    assert (await _stored(db_session, b_old))[0] == CLEARED
    assert (await _stored(db_session, b_fresh))[0] == PAYLOAD


async def test_a_payload_disappears_only_by_redaction(db_session: AsyncSession) -> None:
    """The CHECK: no payload without the moment retention removed it."""
    tenant, account = await _workspace(db_session)
    event = await _event(db_session, tenant, account)
    with pytest.raises(IntegrityError) as refused:
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE whatsapp_events SET payload = NULL WHERE id = :id"),
                {"id": event.id},
            )
    assert "ck_whatsapp_events_payload_present_or_redacted" in str(refused.value)


async def test_the_sweep_reads_its_backlog_by_index(db_session: AsyncSession) -> None:
    """The partial index keeps the sweep off the whole event log."""
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        row[0]
        for row in (
            await db_session.execute(
                text(
                    "EXPLAIN SELECT id FROM whatsapp_events WHERE state = 'processed'"
                    " AND payload IS NOT NULL AND processed_at < now()"
                    " ORDER BY processed_at, id LIMIT 1000"
                )
            )
        ).all()
    )
    assert "ix_whatsapp_events_redactable" in plan


# ------------------------------------------------------ the worker's pass


@pytest.fixture
def counters() -> Iterator[FakeQueueRedis]:
    redis = FakeQueueRedis()
    set_counter_sink(as_redis(redis))
    yield redis
    set_counter_sink(None)


@pytest.fixture
def small_batches(prepared_database: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=prepared_database,
        whatsapp_event_redaction_batch_size=3,
    )


@pytest_asyncio.fixture
async def committed(small_batches: Settings) -> AsyncIterator[tuple[Database, list[uuid.UUID]]]:
    """A real Database, and the workspaces a test commits, removed afterwards."""
    database = Database(small_batches)
    tenants: list[uuid.UUID] = []
    try:
        yield database, tenants
    finally:
        async with database.session() as session:
            for tenant_id in tenants:
                await session.execute(
                    delete(WhatsAppEvent).where(WhatsAppEvent.tenant_id == tenant_id)
                )
                await session.execute(
                    delete(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant_id)
                )
                await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await database.dispose()


async def test_the_worker_drains_a_backlog_in_short_batches(
    committed: tuple[Database, list[uuid.UUID]],
    small_batches: Settings,
    counters: FakeQueueRedis,
    tmp_path: Path,
) -> None:
    """Eight old payloads with a batch of three: three transactions, all cleared."""
    database, tenants = committed
    async with database.session() as session:
        tenant, account = await _workspace(session)
        tenants.append(tenant.id)
        old = [await _event(session, tenant, account) for _ in range(8)]
        kept = await _event(session, tenant, account, processed_at=NOW - timedelta(days=1))

    worker = RetentionWorker(
        database=database, settings=small_batches, storage=LocalMediaStorage(tmp_path)
    )
    assert await worker.redact_webhook_payloads(now=NOW) == 8
    assert await worker.redact_webhook_payloads(now=NOW) == 0

    async with database.session() as session:
        rows = await session.execute(
            select(WhatsAppEvent.id, WhatsAppEvent.payload.is_(None)).where(
                WhatsAppEvent.tenant_id == tenant.id
            )
        )
        # id -> whether the payload is SQL NULL
        cleared = dict(rows.tuples().all())
    assert all(cleared[event.id] for event in old)
    assert not cleared[kept.id]
    retention = counters.hashes[f"{COUNTER_PREFIX}:wasla_webhook_payload_retention_total"]
    assert retention["outcome=redacted"] == 8
    assert "outcome=pending" not in retention
