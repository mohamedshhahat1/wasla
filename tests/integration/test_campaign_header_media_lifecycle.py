"""A campaign header file's life after upload: kept while needed, then retention, purge (HM-07).

Through the real sweeps on committed rows: `MediaRetentionService` (the
retention worker's pass), `MediaUploadReconciler` (the upload worker's) and
`WorkspacePurgeService` + `PurgeWorker` (the purge ledger, MEDIA-07). A header
file is a writer of the same protocol as an attachment, and these are the
same sweeps, not copies of them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.core.storage import LocalMediaStorage, MediaStorage, build_key
from app.db.models.campaign import Campaign, CampaignStatus, CampaignStopReason
from app.db.models.campaign_media import CampaignHeaderMedia
from app.db.models.media import MediaStorageState
from app.db.models.media_purge import MediaPurgeObject
from app.db.models.tenant import Tenant
from app.db.session import Database
from app.services.campaign_service import HEADER_MEDIA_NOT_STORED_CODE, CampaignService
from app.services.media_retention_service import MediaRetentionService
from app.services.media_upload_service import MediaUploadReconciler, Verdict
from app.services.workspace_purge_service import WorkspacePurgeService
from app.workers.campaign_worker import CampaignWorker
from app.workers.purge_worker import PurgeWorker
from tests.integration.campaign_harness import (
    HEADER_PNG,
    FakeMeta,
    compose,
    erase,
    quiesce,
    seed,
    store_file,
    worker,
)

pytestmark = pytest.mark.integration

IMAGE: list[dict[str, Any]] = [
    {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["4::aGFuZGxl"]}},
    {"type": "BODY", "text": "Spring sale."},
]
RETENTION_DAYS = 30


@pytest.fixture
def storage(tmp_path: Path) -> MediaStorage:
    return LocalMediaStorage(tmp_path)


@pytest_asyncio.fixture
async def world(
    prepared_database: str, storage: MediaStorage
) -> AsyncIterator[tuple[FakeMeta, CampaignWorker, Database]]:
    meta = FakeMeta()
    each, database = worker(prepared_database, meta, name="lifecycle", storage=storage)
    await quiesce(database)
    try:
        yield meta, each, database
    finally:
        await quiesce(database)
        await erase(database)
        await database.dispose()


async def _age(database: Database, asset: uuid.UUID, days: int) -> None:
    async with database.session_factory() as session:
        await session.execute(
            update(CampaignHeaderMedia)
            .where(CampaignHeaderMedia.id == asset)
            .values(created_at=datetime.now(UTC) - timedelta(days=days))
        )
        await session.commit()


async def _sweep(database: Database, storage: MediaStorage) -> None:
    async with database.session_factory() as session:
        await MediaRetentionService(session=session, storage=storage).sweep(
            retention_days=RETENTION_DAYS, limit=500
        )


async def _asset(database: Database, asset: uuid.UUID) -> CampaignHeaderMedia:
    async with database.session_factory() as session:
        row = await session.get(CampaignHeaderMedia, asset)
        assert row is not None
        return row


async def _set_status(database: Database, campaign_id: uuid.UUID, status: CampaignStatus) -> None:
    async with database.session_factory() as session:
        # A scheduled campaign is due from `scheduled_at`, as `schedule` sets it.
        await session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(status=status, scheduled_at=datetime.now(UTC), next_send_at=None)
        )
        await session.commit()


@pytest.mark.parametrize(
    "status",
    [CampaignStatus.DRAFT, CampaignStatus.SCHEDULED, CampaignStatus.RUNNING, CampaignStatus.PAUSED],
)
async def test_retention_never_removes_a_file_a_live_campaign_names(
    world: tuple[FakeMeta, CampaignWorker, Database],
    storage: MediaStorage,
    status: CampaignStatus,
) -> None:
    _, _, database = world
    workspace = await seed(database, recipients=1, components=IMAGE)
    asset = await store_file(database, storage, workspace)
    campaign_id = await compose(database, workspace, header_media_id=asset, schedule=False)
    await _set_status(database, campaign_id, status)
    await _age(database, asset, RETENTION_DAYS + 10)

    await _sweep(database, storage)

    row = await _asset(database, asset)
    assert row.storage_state is MediaStorageState.STORED
    assert row.storage_key is not None and await storage.get(row.storage_key) == HEADER_PNG


@pytest.mark.parametrize(
    "status", [CampaignStatus.COMPLETED, CampaignStatus.CANCELLED, CampaignStatus.FAILED, None]
)
async def test_a_file_no_live_campaign_names_follows_ordinary_retention(
    world: tuple[FakeMeta, CampaignWorker, Database],
    storage: MediaStorage,
    status: CampaignStatus | None,
) -> None:
    _, _, database = world
    workspace = await seed(database, recipients=1, components=IMAGE)
    asset = await store_file(database, storage, workspace)
    if status is not None:
        campaign_id = await compose(database, workspace, header_media_id=asset, schedule=False)
        await _set_status(database, campaign_id, status)
    key = (await _asset(database, asset)).storage_key
    assert key is not None

    await _sweep(database, storage)
    assert (await _asset(database, asset)).storage_state is MediaStorageState.STORED, "too young"

    await _age(database, asset, RETENTION_DAYS + 10)
    await _sweep(database, storage)

    row = await _asset(database, asset)
    assert row.storage_state is MediaStorageState.PURGED
    assert row.storage_key is None and not await storage.exists(key)


async def test_a_purged_file_stops_a_resumed_campaign_and_refuses_a_schedule(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world
    workspace = await seed(database, recipients=3, components=IMAGE)
    asset = await store_file(database, storage, workspace)
    campaign_id = await compose(database, workspace, header_media_id=asset, schedule=False)
    second = await compose(database, workspace, header_media_id=asset, schedule=False)
    for each_campaign in (campaign_id, second):
        await _set_status(database, each_campaign, CampaignStatus.COMPLETED)
    await _age(database, asset, RETENTION_DAYS + 10)
    await _sweep(database, storage)
    assert (await _asset(database, asset)).storage_state is MediaStorageState.PURGED

    # Composing on it again is refused at once; so is scheduling a campaign
    # that was composed before the file went.
    with pytest.raises(ValidationError) as composed:
        await compose(database, workspace, header_media_id=asset, schedule=False)
    assert composed.value.error_code == HEADER_MEDIA_NOT_STORED_CODE
    await _set_status(database, second, CampaignStatus.DRAFT)
    with pytest.raises(ValidationError) as refused:
        async with database.session_factory() as session:
            await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
                campaign_id=second
            )
    assert refused.value.error_code == HEADER_MEDIA_NOT_STORED_CODE

    # A campaign already scheduled when its file went - the sweep raced it.
    await _set_status(database, second, CampaignStatus.SCHEDULED)
    await each.run_once()

    assert meta.uploads == [] and meta.requests == []
    async with database.session_factory() as session:
        stopped = await session.get(Campaign, second)
        assert stopped is not None
        assert stopped.status is CampaignStatus.PAUSED
        assert stopped.status_reason == CampaignStopReason.HEADER_MEDIA_UNAVAILABLE


async def test_an_interrupted_upload_is_settled_by_reconciliation(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    _, _, database = world
    workspace = await seed(database, recipients=1, components=IMAGE)
    landed_key = build_key(tenant_id=workspace.tenant_id, mime_type="image/png")
    lost_key = build_key(tenant_id=workspace.tenant_id, mime_type="image/png")
    await storage.put_at(key=landed_key, data=HEADER_PNG, mime_type="image/png")
    import hashlib

    ids = []
    async with database.session_factory() as session:
        for key in (landed_key, lost_key):
            row = CampaignHeaderMedia(
                tenant_id=workspace.tenant_id,
                kind="image",
                mime_type="image/png",
                byte_size=len(HEADER_PNG),
                content_hash=hashlib.sha256(HEADER_PNG).hexdigest(),
                storage_key=key,
                storage_state=MediaStorageState.PENDING,
                upload_started_at=datetime.now(UTC) - timedelta(hours=1),
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
        await session.commit()

    verdicts = []
    for _ in range(2):
        async with database.session_factory() as session:
            outcome = await MediaUploadReconciler(session=session, storage=storage).run(
                grace_seconds=60, limit=10
            )
            verdicts.append(outcome)

    landed, lost = (await _asset(database, ids[0])), (await _asset(database, ids[1]))
    assert landed.storage_state is MediaStorageState.STORED
    assert lost.storage_state is MediaStorageState.ABSENT and lost.storage_key is None
    assert sum(v.finalized for v in verdicts) >= 1 and sum(v.missing for v in verdicts) >= 1
    assert Verdict.FINALIZED.value == "finalized"


async def test_a_workspace_purge_deletes_header_files_through_the_ledger(
    world: tuple[FakeMeta, CampaignWorker, Database],
    storage: MediaStorage,
    prepared_database: str,
) -> None:
    _, _, database = world
    workspace = await seed(database, recipients=1, components=IMAGE)
    asset = await store_file(database, storage, workspace)
    await compose(database, workspace, header_media_id=asset, schedule=False)
    key = (await _asset(database, asset)).storage_key
    assert key is not None
    now = datetime.now(UTC)
    async with database.session_factory() as session:
        tenant = await session.get(Tenant, workspace.tenant_id)
        assert tenant is not None
        tenant.deleted_at = now - timedelta(days=31)
        tenant.purge_due_at = now - timedelta(days=1)
        await session.commit()

    async with database.session_factory() as session:
        tenant = await session.get(Tenant, workspace.tenant_id)
        assert tenant is not None
        outcome = await WorkspacePurgeService(session).purge(tenant, now=now)
        await session.commit()
    assert outcome.objects_recorded >= 1
    async with database.session_factory() as session:
        assert await session.get(CampaignHeaderMedia, asset) is None
        owed = await session.scalar(
            select(func.count())
            .select_from(MediaPurgeObject)
            .where(MediaPurgeObject.storage_key == key)
        )
        assert owed == 1
    assert await storage.exists(key)

    settings = Settings(_env_file=None, environment="test", database_url=prepared_database)
    finished = await PurgeWorker(database=database, settings=settings, storage=storage).run_once(
        now=now + timedelta(minutes=5)
    )

    assert finished.objects_deleted >= 1
    assert not await storage.exists(key)
    async with database.session_factory() as session:
        owed = await session.scalar(
            select(func.count())
            .select_from(MediaPurgeObject)
            .where(MediaPurgeObject.storage_key == key)
        )
        assert owed == 0
