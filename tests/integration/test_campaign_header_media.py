"""A media-header campaign uploads its file once and sends everyone the same id (HM-04 .. HM-07).

Real `CampaignWorker`s - each with its own `Database`, its own commits - the
real `CampaignService`, `MessagingService`, `WhatsAppClient` and Meta error
classifier, and a fake Meta at the HTTP transport that counts uploads
(`POST /{phone-number-id}/media`) and sends (`POST /{phone-number-id}/messages`)
separately (`campaign_harness.FakeMeta`). **"Uploaded once" and "sent once" are
counted at the transport, never as rows.**

Meta's shapes and codes (read 2026-10-15): the upload answers `{"id": ...}`
("Media", developers.facebook.com/documentation/business-messaging/whatsapp/
business-phone-numbers/media.md); the header parameter is `{"type": "image",
"image": {"id": ...}}` and its video and document twins, a document with its
`filename` ("Template media", "Document messages"); "Media IDs returned by the
API expire after 30 days"; 131053 "Unable to upload the media used in the
message" is the only code Meta documents for a send that could not use its
media (Error codes).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.core.storage import LocalMediaStorage, MediaStorage
from app.db.models import Tenant, TenantStatus
from app.db.models.campaign import Campaign, CampaignStatus, CampaignStopReason
from app.db.models.campaign_media import CampaignHeaderMedia
from app.db.models.conversation import Contact
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateStatus, WhatsAppTemplate
from app.db.session import Database
from app.schemas.campaign import CampaignRead
from app.services.campaign_service import CampaignService
from app.workers.campaign_worker import CampaignWorker
from tests.integration.campaign_harness import (
    FakeMeta,
    Workspace,
    accepted,
    compose,
    erase,
    ledger,
    meta_error,
    quiesce,
    seed,
    store_file,
    worker,
)

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"a spring sale photograph" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 256
PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def _template(format_: str) -> list[dict[str, Any]]:
    return [
        {"type": "HEADER", "format": format_, "example": {"header_handle": ["4::aGFuZGxl"]}},
        {"type": "BODY", "text": "Spring sale."},
    ]


IMAGE = _template("IMAGE")


@pytest.fixture
def storage(tmp_path: Path) -> MediaStorage:
    return LocalMediaStorage(tmp_path)


@pytest_asyncio.fixture
async def world(
    prepared_database: str, storage: MediaStorage
) -> AsyncIterator[tuple[FakeMeta, CampaignWorker, Database]]:
    meta = FakeMeta()
    each, database = worker(prepared_database, meta, name="media", storage=storage)
    await quiesce(database)
    try:
        yield meta, each, database
    finally:
        await quiesce(database)
        await erase(database)
        await database.dispose()


async def _campaign(database: Database, campaign_id: uuid.UUID) -> Campaign:
    async with database.session_factory() as session:
        row = await session.get(Campaign, campaign_id)
        assert row is not None
        return row


async def _media_campaign(
    database: Database,
    storage: MediaStorage,
    *,
    recipients: int,
    rate: int = 600,
    components: list[dict[str, Any]] = IMAGE,
    **file: Any,
) -> tuple[Workspace, uuid.UUID, uuid.UUID]:
    workspace = await seed(database, recipients=recipients, components=components)
    asset = await store_file(database, storage, workspace, **file)
    campaign_id = await compose(database, workspace, rate=rate, header_media_id=asset)
    return workspace, asset, campaign_id


def _raising(error: type[httpx.TransportError]) -> Any:
    async def behave(n: int) -> httpx.Response:
        raise error("harness", request=httpx.Request("POST", "https://graph.facebook.com"))

    return behave


def _answering(response: httpx.Response) -> Any:
    async def behave(n: int) -> httpx.Response:
        return response

    return behave


async def _drain(*workers_: CampaignWorker, rounds: int = 40) -> None:
    for _ in range(rounds):
        handled = 0
        for each in workers_:
            handled += await each.run_once()
        if handled == 0:
            return
        await asyncio.sleep(0.05)


# ------------------------------------------------------- the decisive numbers


async def test_thirty_people_one_upload_thirty_sends_one_id(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world
    workspace, _, campaign_id = await _media_campaign(database, storage, recipients=30)

    await each.run_once()

    assert len(meta.uploads) == 1
    assert len(meta.requests) == 30
    assert meta.received_twice() == 0
    ids = meta.header_ids()
    assert set(ids) == {ids[0]} and ids[0] is not None and ids[0].startswith("hm0001")
    header = meta.requests[0]["payload"]["template"]["components"][0]
    assert header == {"type": "header", "parameters": [{"type": "image", "image": {"id": ids[0]}}]}
    # The bytes Meta received are the stored file's.
    assert PNG in meta.uploads[0]["body"]
    async with database.session_factory() as session:
        account = await session.get(WhatsAppAccount, workspace.account_id)
        assert account is not None
        assert meta.uploads[0]["phone_number_id"] == account.phone_number_id
    row = await ledger(database, campaign_id)
    assert row.status is CampaignStatus.COMPLETED and row.sent == 30 and row.meters == 30
    stored = await _campaign(database, campaign_id)
    assert stored.header_provider_media_id == ids[0]
    assert stored.header_provider_account_id == workspace.account_id


@pytest.mark.parametrize("replicas", [2, 4])
async def test_many_workers_upload_once_and_send_everyone_once(
    prepared_database: str, storage: MediaStorage, replicas: int
) -> None:
    """2 and 4 workers contend for every sweep of a three-batch campaign: one upload in all.

    Each round, every worker sweeps at once; whichever claims the campaign
    sends a batch of 10 and the rest find it leased. Later batches - possibly
    on other workers - reuse the id the first recorded. The lease outlasts an
    upload, as in production (300 s): a lease that runs out *during* an upload
    is the takeover case, which may upload twice and sends one id
    (`test_a_lease_taken_over_during_the_upload...`). A 0.2 s lease here made
    this test see that case whenever the machine was loaded (CMV-01).
    """
    meta = FakeMeta(latency=0.005)
    pool = [
        worker(
            prepared_database,
            meta,
            name=f"w{index}",
            lease_seconds=5.0,
            claim_limit=2,
            storage=storage,
        )
        for index in range(replicas)
    ]
    database = pool[0][1]
    try:
        await quiesce(database)
        _, _, campaign_id = await _media_campaign(database, storage, recipients=30, rate=10)
        start = datetime.now(UTC)

        for round_ in range(4):
            moment = start + timedelta(minutes=2 * round_)
            await asyncio.gather(*(each.run_once(now=moment) for each, _ in pool))

        assert len(meta.uploads) == 1
        assert len(meta.requests) == 30
        assert meta.received_twice() == 0
        assert len(set(meta.header_ids())) == 1
        row = await ledger(database, campaign_id)
        assert row.status is CampaignStatus.COMPLETED
        assert row.sent == 30 and row.meters == 30 and row.conversations_with_two == 0
    finally:
        await quiesce(database)
        await erase(database)
        for _, each_db in pool:
            await each_db.dispose()


async def test_a_lease_taken_over_during_the_upload_sends_one_id_to_everyone_once(
    prepared_database: str, storage: MediaStorage
) -> None:
    """A stalls in its upload past its lease; B takes over, uploads, sends; A writes nothing.

    At most two uploads, one id on every send, nobody twice, nothing sent by A.
    """
    meta = FakeMeta(latency=0.005, upload_stall={1: 1.5})
    a, db_a = worker(prepared_database, meta, name="a", lease_seconds=0.5, storage=storage)
    b, db_b = worker(prepared_database, meta, name="b", lease_seconds=0.5, storage=storage)
    try:
        await quiesce(db_a)
        _, _, campaign_id = await _media_campaign(db_a, storage, recipients=30)
        started = asyncio.Event()

        async def on_upload(n: int) -> None:
            if n == 1:
                started.set()

        meta.on_upload = on_upload
        running = asyncio.create_task(a.run_once())
        await started.wait()
        await asyncio.sleep(0.8)
        assert await b.run_once() == 1, "B should have taken the expired lease over"
        await running
        await _drain(a, b)

        assert len(meta.uploads) == 2
        assert meta.uploads[0]["worker"] == "a" and meta.uploads[1]["worker"] == "b"
        assert len(meta.requests) == 30
        assert meta.received_twice() == 0
        assert meta.by_worker()["a"] == 0
        assert len(set(meta.header_ids())) == 1
        stored = await _campaign(db_a, campaign_id)
        assert stored.header_provider_media_id == meta.header_ids()[0]
    finally:
        await quiesce(db_a)
        await erase(db_a)
        await db_a.dispose()
        await db_b.dispose()


async def test_two_campaigns_sharing_a_file_upload_it_once_each(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world
    workspace = await seed(database, recipients=5, components=IMAGE)
    asset = await store_file(database, storage, workspace)
    first = await compose(database, workspace, header_media_id=asset)
    await each.run_once()
    second = await compose(database, workspace, header_media_id=asset, schedule=False)
    async with database.session_factory() as session:
        # The second campaign's audience is the same five people - a day later,
        # since a person hears from a workspace's campaigns once a day (FREQ-01).
        await session.execute(
            update(Contact)
            .where(Contact.id.in_(workspace.contact_ids))
            .values(last_marketing_sent_at=Contact.last_marketing_sent_at - timedelta(days=2))
        )
        await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
            campaign_id=second
        )
        await session.commit()
    await each.run_once()

    assert len(meta.uploads) == 2
    ids = meta.header_ids()
    assert len(ids) == 10 and set(ids[:5]) != set(ids[5:])
    assert {(await _campaign(database, first)).header_provider_media_id} == set(ids[:5])
    assert {(await _campaign(database, second)).header_provider_media_id} == set(ids[5:])


@pytest.mark.parametrize(
    ("format_", "content", "kind", "mime_type", "filename", "expected"),
    [
        ("VIDEO", MP4, "video", "video/mp4", None, None),
        (
            "DOCUMENT",
            PDF,
            "document",
            "application/pdf",
            "Spring catalogue.pdf",
            "Spring catalogue.pdf",
        ),
        ("DOCUMENT", PDF, "document", "application/pdf", "كتالوج.pdf", "attachment.pdf"),
    ],
)
async def test_video_and_document_headers_are_sent_as_meta_documents_them(
    world: tuple[FakeMeta, CampaignWorker, Database],
    storage: MediaStorage,
    format_: str,
    content: bytes,
    kind: str,
    mime_type: str,
    filename: str | None,
    expected: str | None,
) -> None:
    meta, each, database = world
    await _media_campaign(
        database,
        storage,
        recipients=2,
        components=_template(format_),
        content=content,
        kind=kind,
        mime_type=mime_type,
        filename=filename,
    )

    await each.run_once()

    assert len(meta.uploads) == 1 and len(meta.requests) == 2
    media: dict[str, Any] = {"id": meta.header_ids()[0]}
    if expected is not None:
        media["filename"] = expected
    assert meta.requests[0]["payload"]["template"]["components"][0] == {
        "type": "header",
        "parameters": [{"type": kind, kind: media}],
    }
    if expected is not None:
        # The upload carried the same Meta-safe name (MEDIA-06).
        assert expected.encode() in meta.uploads[0]["body"]


# ------------------------------------------------------------------ refresh


async def test_an_id_older_than_the_refresh_age_is_uploaded_again_before_the_batch(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world
    _, _, campaign_id = await _media_campaign(database, storage, recipients=15, rate=5)
    start = datetime.now(UTC)

    await each.run_once(now=start)
    assert len(meta.uploads) == 1 and len(meta.requests) == 5
    await each.run_once(now=start + timedelta(minutes=2))
    assert len(meta.uploads) == 1 and len(meta.requests) == 10, "a fresh id is reused"

    await each.run_once(now=start + timedelta(days=26))

    assert len(meta.uploads) == 2
    ids = meta.header_ids()
    assert len(ids) == 15
    assert set(ids[:10]) == {ids[0]} and set(ids[10:]) == {ids[10]} and ids[0] != ids[10]
    assert meta.received_twice() == 0
    assert (await ledger(database, campaign_id)).status is CampaignStatus.COMPLETED


async def test_a_campaign_paused_past_metas_lifetime_uploads_again_before_its_first_send(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world
    workspace, _, campaign_id = await _media_campaign(database, storage, recipients=10, rate=5)
    start = datetime.now(UTC)
    await each.run_once(now=start)
    async with database.session_factory() as session:
        await CampaignService(session=session, tenant_id=workspace.tenant_id).pause(campaign_id)
        await session.commit()
    async with database.session_factory() as session:
        # Uploaded 31 days ago: Meta has forgotten it.
        await session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(header_provider_uploaded_at=datetime.now(UTC) - timedelta(days=31))
        )
        await CampaignService(session=session, tenant_id=workspace.tenant_id).schedule(
            campaign_id=campaign_id
        )
        await session.commit()

    await each.run_once()

    assert len(meta.uploads) == 2
    ids = meta.header_ids()
    assert len(ids) == 10 and set(ids[5:]) == {meta.header_ids()[-1]} and ids[0] != ids[5]
    assert meta.received_twice() == 0


# ----------------------------------------------------------------- failures


@pytest.mark.parametrize("damage", ["deleted", "altered"])
async def test_a_stored_file_gone_or_altered_pauses_the_campaign_before_anything(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage, damage: str
) -> None:
    meta, each, database = world
    _, asset, campaign_id = await _media_campaign(database, storage, recipients=3)
    async with database.session_factory() as session:
        row = await session.get(CampaignHeaderMedia, asset)
        assert row is not None and row.storage_key is not None
        key = row.storage_key
    if damage == "deleted":
        await storage.delete(key)
    else:
        await storage.put_at(key=key, data=PNG + b"tampered", mime_type="image/png")

    await each.run_once()

    assert meta.uploads == [] and meta.requests == []
    paused = await _campaign(database, campaign_id)
    assert paused.status is CampaignStatus.PAUSED
    assert paused.status_reason == CampaignStopReason.HEADER_MEDIA_UNAVAILABLE
    assert dict((await ledger(database, campaign_id)).recipients) == {"pending/attempts=0": 3}


async def test_meta_refusing_the_upload_fails_the_campaign_once_and_keeps_the_template(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world
    meta.upload_behaviour = _answering(meta_error(400, 100))
    workspace, _, campaign_id = await _media_campaign(database, storage, recipients=3)

    await each.run_once()

    assert len(meta.uploads) == 1 and meta.requests == []
    failed = await _campaign(database, campaign_id)
    assert failed.status is CampaignStatus.FAILED
    assert failed.status_reason == CampaignStopReason.HEADER_MEDIA_REFUSED
    assert failed.last_error is not None and "code 100" in failed.last_error
    assert failed.header_provider_media_id is None
    assert dict((await ledger(database, campaign_id)).recipients) == {"pending/attempts=0": 3}
    async with database.session_factory() as session:
        template = await session.get(WhatsAppTemplate, workspace.template_id)
        assert template is not None and template.status is TemplateStatus.APPROVED


async def test_an_unreachable_meta_costs_time_and_pauses_after_the_bound(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world
    meta.upload_behaviour = _raising(httpx.ConnectError)
    _, _, campaign_id = await _media_campaign(database, storage, recipients=3)
    start = datetime.now(UTC)

    await each.run_once(now=start)
    await each.run_once(now=start + timedelta(minutes=2))

    waiting = await _campaign(database, campaign_id)
    assert waiting.status is CampaignStatus.RUNNING
    assert waiting.next_send_at is not None and waiting.next_send_at > start
    assert meta.requests == []
    assert dict((await ledger(database, campaign_id)).recipients) == {"pending/attempts=0": 3}

    await each.run_once(now=start + timedelta(hours=7))

    paused = await _campaign(database, campaign_id)
    assert paused.status is CampaignStatus.PAUSED
    assert paused.status_reason == CampaignStopReason.PROVIDER_UNREACHABLE
    assert meta.requests == []


@pytest.mark.parametrize("answer", ["read_timeout", "500", "200_no_id"])
async def test_an_uncertain_upload_records_no_id_and_the_next_batch_uploads_again(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage, answer: str
) -> None:
    meta, each, database = world
    first: Any = {
        "read_timeout": _raising(httpx.ReadTimeout),
        "500": _answering(meta_error(500, None)),
        "200_no_id": _answering(httpx.Response(200, json={})),
    }[answer]

    async def behave(n: int) -> httpx.Response:
        if n == 1:
            response: httpx.Response = await first(n)
            return response
        return httpx.Response(200, json={"id": f"hm-good-{n}"})

    meta.upload_behaviour = behave
    _, _, campaign_id = await _media_campaign(database, storage, recipients=3)
    start = datetime.now(UTC)

    await each.run_once(now=start)

    after_first = await _campaign(database, campaign_id)
    assert after_first.header_provider_media_id is None
    assert meta.requests == [] and len(meta.uploads) == 1

    await each.run_once(now=start + timedelta(minutes=2))

    assert len(meta.uploads) == 2 and len(meta.requests) == 3
    assert set(meta.header_ids()) == {"hm-good-2"}
    assert (await ledger(database, campaign_id)).status is CampaignStatus.COMPLETED


async def test_media_unknown_on_a_send_uploads_once_more_and_the_campaign_goes_on(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world

    async def behaviour(payload: dict[str, Any], n: int) -> httpx.Response:
        if n == 1:
            return meta_error(400, 131053)
        return accepted(payload, n)

    meta.behaviour = behaviour
    _, _, campaign_id = await _media_campaign(database, storage, recipients=5)

    await each.run_once()

    assert len(meta.uploads) == 2
    assert len(meta.requests) == 6
    assert meta.per_recipient()[meta.requests[0]["to"]] == 2  # refused, then sent once
    ids = meta.header_ids()
    assert ids[0] != ids[1] and set(ids[1:]) == {ids[1]}
    row = await ledger(database, campaign_id)
    assert row.status is CampaignStatus.COMPLETED
    assert dict(row.recipients) == {"sent/attempts=0": 5}
    assert row.sent == row.meters == 5


async def test_media_unknown_twice_fails_the_campaign_after_two_sends(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world

    async def behaviour(payload: dict[str, Any], n: int) -> httpx.Response:
        return meta_error(400, 131053)

    meta.behaviour = behaviour
    _, _, campaign_id = await _media_campaign(database, storage, recipients=5)

    await each.run_once()

    assert len(meta.requests) == 2
    assert len(meta.uploads) == 2
    failed = await _campaign(database, campaign_id)
    assert failed.status is CampaignStatus.FAILED
    assert failed.status_reason == CampaignStopReason.HEADER_MEDIA_REFUSED
    assert failed.header_provider_media_id is None
    assert dict((await ledger(database, campaign_id)).recipients) == {"pending/attempts=0": 5}


async def test_a_refused_credential_on_upload_fails_the_campaign(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world
    meta.upload_behaviour = _answering(meta_error(401, 190))
    _, _, campaign_id = await _media_campaign(database, storage, recipients=3)

    await each.run_once()

    assert len(meta.uploads) == 1 and meta.requests == []
    failed = await _campaign(database, campaign_id)
    assert failed.status is CampaignStatus.FAILED
    assert failed.status_reason == CampaignStopReason.CREDENTIAL_REFUSED


async def test_an_unreadable_workspace_token_is_never_replaced_by_the_platforms(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    """ADR-034: a number's own credential this process cannot read is refused, not downgraded."""
    meta, each, database = world
    workspace, _, campaign_id = await _media_campaign(database, storage, recipients=3)
    async with database.session_factory() as session:
        await session.execute(
            update(WhatsAppAccount)
            .where(WhatsAppAccount.id == workspace.account_id)
            .values(access_token_encrypted="v2:sealed-by-another-key")
        )
        await session.commit()

    await each.run_once()

    assert meta.uploads == [] and meta.requests == []
    failed = await _campaign(database, campaign_id)
    assert failed.status is CampaignStatus.FAILED
    assert failed.status_reason == CampaignStopReason.CREDENTIAL_REFUSED


# ---------------------------------------------------------------- lifecycle


async def test_a_suspended_workspace_uploads_nothing_and_sends_nothing(
    world: tuple[FakeMeta, CampaignWorker, Database], storage: MediaStorage
) -> None:
    meta, each, database = world
    workspace, _, campaign_id = await _media_campaign(database, storage, recipients=3)
    async with database.session_factory() as session:
        await session.execute(
            update(Tenant)
            .where(Tenant.id == workspace.tenant_id)
            .values(status=TenantStatus.SUSPENDED)
        )
        await session.commit()

    await each.run_once()

    assert meta.uploads == [] and meta.requests == []
    paused = await _campaign(database, campaign_id)
    assert paused.status_reason == CampaignStopReason.WORKSPACE_SUSPENDED


# ----------------------------------------------------------------- leakage


async def test_metas_id_is_in_no_response_and_no_log_line(
    world: tuple[FakeMeta, CampaignWorker, Database],
    storage: MediaStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    meta, each, database = world
    caplog.set_level(logging.DEBUG)
    workspace, asset, campaign_id = await _media_campaign(database, storage, recipients=2)

    await each.run_once()

    provider_id = (await _campaign(database, campaign_id)).header_provider_media_id
    assert provider_id is not None
    async with database.session_factory() as session:
        service = CampaignService(session=session, tenant_id=workspace.tenant_id)
        campaign = await service.get(campaign_id)
        files = await service.header_media_for([campaign])
        read = CampaignRead.from_model(campaign, header_media=files.get(campaign.id))
        key = (await session.get(CampaignHeaderMedia, asset)).storage_key  # type: ignore[union-attr]
    body = read.model_dump_json()
    assert provider_id not in body and str(key) not in body
    assert read.header_media is not None and read.header_media.id == asset
    assert provider_id not in caplog.text
    async with database.session_factory() as session:
        from app.db.models.audit import AuditLog

        trail = (
            await session.execute(
                select(AuditLog.meta).where(AuditLog.tenant_id == workspace.tenant_id)
            )
        ).scalars()
        assert all(provider_id not in str(meta_) for meta_ in trail)


async def test_a_worker_held_before_its_upload_past_its_lease_uploads_once(
    prepared_database: str, storage: MediaStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker held between its claim and its upload, past its lease: one upload in all (HM-04).

    A is held after claiming its batch, before preparing the header, for longer
    than its lease. The campaign row stays locked from `claim_by_id` until the
    upload's first commit, so B cannot take it over in that window, and A's
    `hold()` before the upload still finds its own token. Either way exactly
    one worker uploads, once, and everybody is sent that id once. (Without the
    row lock and without `hold()`, B takes over and A uploads a second copy:
    the combined mutant that proves `hold()` an independent guard.)
    """
    from app.repositories.campaign_repository import CampaignRecipientRepository

    meta = FakeMeta(latency=0.005)
    a, db_a = worker(prepared_database, meta, name="a", lease_seconds=0.5, storage=storage)
    b, db_b = worker(prepared_database, meta, name="b", lease_seconds=0.5, storage=storage)
    original = CampaignRecipientRepository.claim_pending
    calls = 0
    held = asyncio.Event()

    async def slow_first(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        claimed = await original(self, *args, **kwargs)
        if calls == 1:
            held.set()
            await asyncio.sleep(1.2)
        return claimed

    monkeypatch.setattr(CampaignRecipientRepository, "claim_pending", slow_first)
    try:
        await quiesce(db_a)
        _, _, campaign_id = await _media_campaign(db_a, storage, recipients=10)
        running = asyncio.create_task(a.run_once())
        await held.wait()
        await asyncio.sleep(0.7)
        await b.run_once()
        await running
        await _drain(a, b)

        assert len(meta.uploads) == 1
        uploader = meta.uploads[0]["worker"]
        assert len(meta.requests) == 10 and meta.by_worker()[uploader] == 10
        assert set(meta.header_ids()) == {meta.header_ids()[0]}
        assert meta.received_twice() == 0
        assert (await ledger(db_a, campaign_id)).sent == 10
    finally:
        await quiesce(db_a)
        await erase(db_a)
        await db_a.dispose()
        await db_b.dispose()
