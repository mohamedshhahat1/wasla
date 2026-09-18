"""A workspace the platform no longer serves gets no media spend (MEDIA-08).

The audit's P2-04/P2-05: a suspended or soft-deleted workspace still had its
customers' files downloaded from Meta, stored, and read by a paid model, and a
released number's file was queued and read though its text was only recorded.
The agent worker refused the eventual reply (AI-06), so nobody saw anything -
which is what made the spend invisible.

PD-MEDIA-05 as implemented: suspended, soft-deleted and released each end the
file `SKIPPED` with a fixed reason and cost nothing - no descriptor, no
download, no object, no vision, transcription or PDF parse - and the row is
never left waiting. The lifecycle is read fresh before each costly step, so a
suspension that lands mid-download stops the write and the read that follow.
A closed conversation or a disabled agent is not a reason to drop a customer's
file; the reply is the agent worker's business.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.storage import LocalMediaStorage
from app.db.models.conversation import ConversationStatus
from app.db.models.enums import TenantStatus
from app.db.models.media import MediaStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.repositories.media_repository import MediaRepository
from app.services.media_outcomes import MediaReason, text_for
from app.services.media_reader import ReadResult
from app.services.media_service import MediaService
from app.services.whatsapp_service import WhatsAppIngestionService
from tests import media_harness as h
from tests.fakes import as_agent_queue, as_media_queue, as_media_reader, as_whatsapp

pytestmark = pytest.mark.integration


class CountingReader:
    def __init__(self) -> None:
        self.reads = 0

    async def read(self, *, content: bytes, mime_type: str | None) -> ReadResult:
        self.reads += 1
        return ReadResult(transcript="A blue sofa.", method="vision")


def _objects(tmp_path: Path) -> list[Path]:
    return [path for path in tmp_path.rglob("*") if path.is_file()]


async def _suspend(session: AsyncSession, where: h.Scene) -> None:
    where.tenant.status = TenantStatus.SUSPENDED
    await session.flush()


async def _delete(session: AsyncSession, where: h.Scene) -> None:
    where.tenant.deleted_at = datetime.now(UTC)
    await session.flush()


async def _release(session: AsyncSession, where: h.Scene) -> None:
    where.account.status = WhatsAppAccountStatus.RELEASED
    where.account.released_at = datetime.now(UTC)
    await session.flush()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (_suspend, MediaReason.WORKSPACE_SUSPENDED),
        (_delete, MediaReason.WORKSPACE_DELETED),
        (_release, MediaReason.CHANNEL_UNAVAILABLE),
    ],
)
async def test_a_file_queued_before_the_change_costs_nothing_after_it(
    db_session: AsyncSession,
    tmp_path: Path,
    settings: Settings,
    change: object,
    reason: MediaReason,
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    await change(db_session, where)  # type: ignore[operator]
    whatsapp = h.StubWhatsApp()
    reader = CountingReader()
    worker = h.worker(db_session, tmp_path, settings, whatsapp=whatsapp, reader=reader)

    job = await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.SKIPPED
    assert media.last_error == text_for(reason)
    assert (whatsapp.probes, whatsapp.fetches, reader.reads) == (0, 0, 0)
    assert media.storage_key is None
    assert _objects(tmp_path) == []
    # Resolved, so nothing strands. The turn this releases is the agent
    # worker's to refuse, under its own lifecycle check (AI-06).
    assert media.claim_id is None
    assert job is not None


async def test_a_suspension_during_the_download_stops_the_write_and_the_read(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)

    async def suspended_meanwhile() -> None:
        await _suspend(db_session, where)

    whatsapp = h.StubWhatsApp(before_fetch=suspended_meanwhile)
    reader = CountingReader()
    worker = h.worker(db_session, tmp_path, settings, whatsapp=whatsapp, reader=reader)

    await h.run(worker, media)

    await db_session.refresh(media)
    assert whatsapp.fetches == 1
    assert media.status is MediaStatus.SKIPPED
    assert media.last_error == text_for(MediaReason.WORKSPACE_SUSPENDED)
    assert _objects(tmp_path) == []
    assert reader.reads == 0


async def test_a_suspension_after_the_file_is_stored_stops_the_paid_read(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """The object already stored is kept - historical media, under ordinary
    retention and purge - and no provider is paid to read it."""
    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    storage = LocalMediaStorage(tmp_path)
    service = MediaService(
        session=db_session,
        tenant_id=where.tenant.id,
        settings=settings,
        storage=storage,
        whatsapp=as_whatsapp(h.StubWhatsApp()),
    )
    stored = await service.download(media)
    assert stored.status is MediaStatus.STORED

    await _suspend(db_session, where)
    reader = CountingReader()
    outcome = await service.understand(media, reader=as_media_reader(reader))

    assert outcome.status is MediaStatus.SKIPPED
    assert outcome.reason is MediaReason.WORKSPACE_SUSPENDED
    assert reader.reads == 0
    assert len(_objects(tmp_path)) == 1


async def test_a_closed_conversation_or_disabled_agent_still_has_its_file_read(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    where = await h.scene(db_session)
    where.conversation.status = ConversationStatus.CLOSED
    media = await h.attachment(db_session, where)
    reader = CountingReader()
    worker = h.worker(db_session, tmp_path, settings, reader=reader)

    await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.READY
    assert reader.reads == 1


async def test_the_lifecycle_read_is_fresh_and_scoped_to_the_workspace(
    db_session: AsyncSession,
) -> None:
    """Read as columns, so a suspension written after the conversation was
    loaded is seen; and another workspace's conversation reads as missing."""
    mine = await h.scene(db_session, slug="mine")
    theirs = await h.scene(db_session, slug="theirs")
    repository = MediaRepository(db_session, tenant_id=mine.tenant.id)

    before = await repository.serving(mine.conversation.id)
    assert before is not None and before.workspace_active
    await _suspend(db_session, mine)
    after = await repository.serving(mine.conversation.id)
    assert after is not None and not after.workspace_active

    assert await repository.serving(theirs.conversation.id) is None


# --------------------------------------- a released number, at the webhook


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[object] = []

    async def enqueue(self, job: object) -> None:
        self.jobs.append(job)


async def test_a_released_numbers_file_is_recorded_terminal_and_never_queued(
    db_session: AsyncSession,
) -> None:
    """P2-05, reversed. The message belongs to the tenure; the number has since
    been given up. Text is recorded and not answered (MSG-01), and the file
    beside it is recorded the same way: terminal, with nothing queued."""
    sent_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    tenant = Tenant(name="Former", slug="former")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id="109876543299",
            waba_id="waba-former",
            display_phone_number="+201000000099",
            status=WhatsAppAccountStatus.RELEASED,
            ownership_started_at=sent_at - timedelta(days=30),
            ownership_verified_at=sent_at - timedelta(days=30),
            released_at=sent_at + timedelta(days=1),
        )
    )
    await db_session.flush()

    agents, media_jobs = RecordingQueue(), RecordingQueue()
    outcome = await WhatsAppIngestionService(
        session=db_session,
        queue=as_agent_queue(agents),
        media_queue=as_media_queue(media_jobs),
    ).ingest(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "109876543299"},
                                "contacts": [{"wa_id": "201234567890"}],
                                "messages": [
                                    {
                                        "id": "wamid.history",
                                        "from": "201234567890",
                                        "type": "image",
                                        "timestamp": str(int(sent_at.timestamp())),
                                        "image": {"id": "media-old", "mime_type": "image/jpeg"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    )

    assert outcome.stored == 1
    assert (outcome.media_queued, outcome.queued) == (0, 0)
    assert media_jobs.jobs == [] and agents.jobs == []
    rows = await MediaRepository(db_session, tenant_id=tenant.id).list_for_conversation(
        await _only_conversation(db_session, tenant.id)
    )
    assert len(rows) == 1
    assert rows[0].status is MediaStatus.SKIPPED
    assert rows[0].last_error == text_for(MediaReason.CHANNEL_UNAVAILABLE)


async def _only_conversation(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    from sqlalchemy import select

    from app.db.models.conversation import Conversation

    found: uuid.UUID = (
        await session.execute(select(Conversation.id).where(Conversation.tenant_id == tenant_id))
    ).scalar_one()
    return found
