"""Each campaign recipient can see the photo meant for them (HMR-01 .. HMR-03).

Real `CampaignWorker`s with their own databases and commits; Meta at the
transport counts every upload and every send, with the header id each send
carried. Three files - A for leads interested in X, B for Y, C for everybody
else - and thirty people: ten of each. One upload per file and connection; each
person's own file; nobody twice, with one, two or four workers; a file Meta
refuses waits its own people, never the campaign.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from app.core.storage import LocalMediaStorage, MediaStorage
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.db.models.campaign_media import CampaignHeaderMedia
from app.db.models.lead import Lead, LeadSource, LeadStatus
from app.db.session import Database
from app.services.campaign_service import CampaignService
from app.services.media_retention_service import MediaRetentionService
from app.services.template_header_media import TemplateHeaderMediaService
from tests.integration.campaign_harness import (
    FakeMeta,
    Workspace,
    compose,
    erase,
    meta_error,
    quiesce,
    seed,
    store_file,
    worker,
)

pytestmark = pytest.mark.integration

IMAGE_HEADER: list[dict[str, Any]] = [
    {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["4::aGFuZGxl"]}},
    {"type": "BODY", "text": "Spring offer."},
]


def png(marker: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + f"photograph {marker} ".encode() * 40


async def _leads(database: Database, workspace: Workspace) -> None:
    """Ten interested in X, ten in Y, ten with no lead."""
    async with database.session_factory() as session:
        for index, contact_id in enumerate(workspace.contact_ids[:20]):
            session.add(
                Lead(
                    tenant_id=workspace.tenant_id,
                    contact_id=contact_id,
                    status=LeadStatus.NEW,
                    source=LeadSource.MANUAL,
                    interest="X" if index < 10 else "y ",
                )
            )
        await session.commit()


async def _world(
    prepared_database: str, tmp_path: Path, name: str
) -> tuple[FakeMeta, Database, MediaStorage, Workspace, dict[str, uuid.UUID], uuid.UUID]:
    meta = FakeMeta()
    storage = LocalMediaStorage(tmp_path)
    _, database = worker(prepared_database, meta, name=name, storage=storage)
    await quiesce(database)
    workspace = await seed(database, recipients=30, components=IMAGE_HEADER)
    await _leads(database, workspace)
    files = {
        marker: await store_file(database, storage, workspace, content=png(marker))
        for marker in ("A", "B", "C")
    }
    campaign_id = await compose(
        database,
        workspace,
        header_media_id=files["C"],
        header_media_mode="per_recipient",
        header_media_rules=[
            {"when": {"field": "lead.interest", "equals": "x"}, "header_media_id": str(files["A"])},
            {"when": {"field": "lead.interest", "equals": "Y"}, "header_media_id": str(files["B"])},
        ],
    )
    return meta, database, storage, workspace, files, campaign_id


async def _drain(replicas: list[Any], rounds: int = 30) -> None:
    for _ in range(rounds):
        handled = await asyncio.gather(*(replica.run_once() for replica in replicas))
        if not any(handled):
            return


async def _numbers(database: Database, workspace: Workspace) -> dict[uuid.UUID, str]:
    from app.db.models.conversation import Contact

    async with database.session_factory() as session:
        rows = await session.execute(
            select(Contact.id, Contact.wa_id).where(Contact.tenant_id == workspace.tenant_id)
        )
        return {row[0]: str(row[1]) for row in rows.all()}


def _ids_by_number(meta: FakeMeta) -> dict[str, str | None]:
    return {
        str(entry["to"]): header
        for entry, header in zip(meta.requests, meta.header_ids(), strict=True)
    }


@pytest.mark.parametrize("workers", [1, 2, 4])
async def test_ten_ten_ten_with_three_uploads_and_nobody_twice(
    prepared_database: str, tmp_path: Path, workers: int
) -> None:
    meta, database, storage, workspace, files, campaign_id = await _world(
        prepared_database, tmp_path, f"hmr-{workers}"
    )
    replicas = [
        worker(prepared_database, meta, name=f"hmr-{workers}-{n}", storage=storage)[0]
        for n in range(workers)
    ]
    try:
        await _drain(replicas)

        assert meta.received_twice() == 0
        assert len(meta.requests) == 30
        assert len(meta.uploads) == 3, "one upload per file and connection"
        numbers = await _numbers(database, workspace)
        sent = _ids_by_number(meta)
        by_group = [
            {sent[numbers[contact_id]] for contact_id in group}
            for group in (
                workspace.contact_ids[:10],
                workspace.contact_ids[10:20],
                workspace.contact_ids[20:],
            )
        ]
        assert all(len(ids) == 1 for ids in by_group), "one file per group"
        assert len(set.union(*by_group)) == 3, "three different files"
        async with database.session_factory() as session:
            row = await session.get(Campaign, campaign_id)
            assert row is not None and row.status is CampaignStatus.COMPLETED
            chosen = Counter(
                (
                    await session.execute(
                        select(CampaignRecipient.header_media_id).where(
                            CampaignRecipient.campaign_id == campaign_id
                        )
                    )
                ).scalars()
            )
        assert chosen == Counter({files["A"]: 10, files["B"]: 10, files["C"]: 10})
    finally:
        await erase(database)
        await database.dispose()


async def test_a_refused_file_waits_its_people_and_skips_them_after_three(
    prepared_database: str, tmp_path: Path
) -> None:
    meta, database, storage, workspace, files, campaign_id = await _world(
        prepared_database, tmp_path, "hmr-refused"
    )
    replica, _ = worker(prepared_database, meta, name="hmr-refused-w", storage=storage)

    async def refuse_b(n: int) -> Any:
        import httpx

        if b"photograph B" in meta.uploads[-1]["body"]:
            return meta_error(400, 100)
        return httpx.Response(200, json={"id": f"hm{n:04d}{uuid.uuid4().hex[:6]}"})

    meta.upload_behaviour = refuse_b
    try:
        for _ in range(6):
            await _drain([replica])
            async with database.session_factory() as session:
                await session.execute(
                    update(CampaignRecipient)
                    .where(CampaignRecipient.campaign_id == campaign_id)
                    .values(not_before=None)
                )
                await session.execute(
                    update(Campaign).where(Campaign.id == campaign_id).values(next_send_at=None)
                )
                await session.commit()

        assert len(meta.requests) == 20, "A's and C's people were sent"
        assert meta.received_twice() == 0
        async with database.session_factory() as session:
            row = await session.get(Campaign, campaign_id)
            assert row is not None and row.status is CampaignStatus.COMPLETED
            assert (row.header_media_refusals or {}).get(str(files["B"])) == 3
            skipped = (
                await session.execute(
                    select(CampaignRecipient.status, CampaignRecipient.reason_code).where(
                        CampaignRecipient.campaign_id == campaign_id,
                        CampaignRecipient.header_media_id == files["B"],
                    )
                )
            ).all()
            assert {tuple(row) for row in skipped} == {
                (RecipientStatus.SKIPPED, "header_media_refused")
            }
            attempts = (
                await session.execute(
                    select(CampaignRecipient.attempts).where(
                        CampaignRecipient.campaign_id == campaign_id
                    )
                )
            ).scalars()
            assert set(attempts) <= {0, 1}
    finally:
        await erase(database)
        await database.dispose()


async def test_the_frozen_file_is_sent_after_the_lead_changes(
    prepared_database: str, tmp_path: Path
) -> None:
    meta, database, storage, workspace, files, campaign_id = await _world(
        prepared_database, tmp_path, "hmr-frozen"
    )
    replica, _ = worker(prepared_database, meta, name="hmr-frozen-w", storage=storage)
    try:
        async with database.session_factory() as session:
            await session.execute(
                update(Lead).where(Lead.contact_id == workspace.contact_ids[0]).values(interest="Y")
            )
            await session.commit()
            preview = await CampaignService(
                session=session, tenant_id=workspace.tenant_id
            ).preview_messages(campaign_id, [workspace.contact_ids[0], workspace.contact_ids[25]])
        assert [item.header_media_id for item in preview] == [files["A"], files["C"]]
        await _drain([replica])
        numbers = await _numbers(database, workspace)
        sent = _ids_by_number(meta)
        first = sent[numbers[workspace.contact_ids[0]]]
        assert first == sent[numbers[workspace.contact_ids[1]]], "still file A"
    finally:
        await erase(database)
        await database.dispose()


async def test_composition_refuses_foreign_wrong_kind_and_too_many_files(
    prepared_database: str, tmp_path: Path
) -> None:
    from app.core.exceptions import NotFoundError, ValidationError

    meta = FakeMeta()
    storage = LocalMediaStorage(tmp_path)
    _, database = worker(prepared_database, meta, name="hmr-compose", storage=storage)
    try:
        workspace = await seed(database, recipients=1, components=IMAGE_HEADER)
        other = await seed(database, recipients=1, components=IMAGE_HEADER)
        default = await store_file(database, storage, workspace, content=png("D"))
        theirs = await store_file(database, storage, other, content=png("T"))
        pdf = await store_file(
            database,
            storage,
            workspace,
            content=b"%PDF-1.4 list" * 30,
            kind="document",
            mime_type="application/pdf",
            filename="a.pdf",
        )
        many = [
            await store_file(database, storage, workspace, content=png(f"M{n}")) for n in range(20)
        ]

        async def create(rules: list[dict[str, Any]]) -> None:
            async with database.session_factory() as session:
                await CampaignService(session=session, tenant_id=workspace.tenant_id).create(
                    account_id=workspace.account_id,
                    template_id=workspace.template_id,
                    name="Rules",
                    header_media_id=default,
                    header_media_mode="per_recipient",
                    header_media_rules=rules,
                )

        def rule(file_id: uuid.UUID, value: str = "x") -> dict[str, Any]:
            return {
                "when": {"field": "lead.interest", "equals": value},
                "header_media_id": str(file_id),
            }

        with pytest.raises(NotFoundError):
            await create([rule(theirs)])
        with pytest.raises(ValidationError) as kind:
            await create([rule(pdf)])
        assert kind.value.error_code == "header_media_kind_mismatch"
        with pytest.raises(ValidationError) as too_many:
            await create([rule(file_id, str(n)) for n, file_id in enumerate(many)])
        assert too_many.value.error_code == "header_media_too_many_files"
    finally:
        await erase(database)
        await database.dispose()


async def test_a_file_named_only_by_a_running_campaign_s_recipient_is_kept(
    prepared_database: str, tmp_path: Path
) -> None:
    meta, database, storage, workspace, files, campaign_id = await _world(
        prepared_database, tmp_path, "hmr-retention"
    )
    try:
        async with database.session_factory() as session:
            await session.execute(
                update(Campaign)
                .where(Campaign.id == campaign_id)
                .values(status=CampaignStatus.RUNNING)
            )
            await session.execute(
                update(CampaignHeaderMedia)
                .where(CampaignHeaderMedia.tenant_id == workspace.tenant_id)
                .values(created_at=CampaignHeaderMedia.created_at - timedelta(days=400))
            )
            await session.commit()
        async with database.session_factory() as session:
            claimed = await MediaRetentionService(session=session, storage=storage).claim(
                retention_days=30, limit=100
            )
        assert not {row.id for row in claimed} & set(files.values())
        async with database.session_factory() as session:
            assert await TemplateHeaderMediaService(
                session,
                tenant_id=workspace.tenant_id,
                refresh=timedelta(days=25),
            ).in_use(files["A"])
    finally:
        await erase(database)
        await database.dispose()
