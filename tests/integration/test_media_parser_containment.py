"""Customer PDFs through the real worker: bounded, contained, and never stranding
the conversation (MEDIA-02, MEDIA-03).

Driven through `MediaWorker._handle` with the production `MediaReader`, so the
bytes travel the same route a customer's file does - download, store, read,
release - and the only stand-in is Meta handing the file over.

The audit's two findings are two sides of one boundary. A PDF parsed on the
worker's own event loop could stall every worker in the process for minutes
(MEDIA-02), and a parser exception outside a narrow catch list escaped the job,
left the row `downloading` for ever and silenced every later attachment in the
conversation (MEDIA-03). The parse now happens in the bounded child the
knowledge base already uses, and whatever it does costs that one file.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.conversation import MessageKind
from app.db.models.media import MediaStatus
from app.services.knowledge_limits import EXTRACTION_TIMEOUT_SECONDS
from app.services.media_reader import DocumentBeyondLimitsError, ReadResult
from app.services.media_service import READER_FAILED
from tests import media_harness as h
from tests.pdf_fixtures import POISON_PDF_CLASSES, amplifying_pdf, poison_pdf

pytestmark = pytest.mark.integration


def _resident_bytes() -> int | None:
    """This process's resident set, where the platform reports one."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


@pytest.mark.parametrize("escape_class", POISON_PDF_CLASSES)
async def test_a_poison_pdf_is_skipped_and_the_conversation_is_answered(
    db_session: AsyncSession, tmp_path: Path, settings: Settings, escape_class: str
) -> None:
    """P2-01, reversed, for each class that escaped. Before: `downloading` for
    ever, dead-lettered, zero turns. After: skipped, one turn."""
    where = await h.scene(db_session)
    media = await h.attachment(
        db_session, where, mime_type="application/pdf", kind=MessageKind.DOCUMENT
    )
    whatsapp = h.StubWhatsApp(content=poison_pdf(escape_class), mime_type="application/pdf")
    worker = h.worker(db_session, tmp_path, settings, whatsapp=whatsapp)

    job = await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.SKIPPED
    assert media.last_error == "This document could not be read."
    assert escape_class not in (media.last_error or "")
    assert job is not None
    assert len(h.released(worker)) == 1


async def test_a_later_attachment_still_gets_its_turn_after_a_poison_pdf(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """The conversation-wide half of MEDIA-03: one bad file used to block every
    later attachment in the conversation, because release waits for none to be
    unresolved."""
    where = await h.scene(db_session)
    poison = await h.attachment(
        db_session, where, mime_type="application/pdf", kind=MessageKind.DOCUMENT
    )
    await h.run(
        h.worker(
            db_session,
            tmp_path,
            settings,
            whatsapp=h.StubWhatsApp(content=poison_pdf("KeyError"), mime_type="application/pdf"),
        ),
        poison,
    )

    readable = await h.attachment(
        db_session, where, mime_type="text/plain", kind=MessageKind.DOCUMENT
    )
    worker = h.worker(
        db_session,
        tmp_path,
        settings,
        whatsapp=h.StubWhatsApp(content=h.TEXT, mime_type="text/plain"),
    )
    job = await h.run(worker, readable)

    await db_session.refresh(readable)
    assert readable.status is MediaStatus.READY
    assert job is not None
    assert job.trigger_message_id == readable.message_id


async def test_an_unexpected_reader_exception_costs_the_file_not_the_conversation(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """The broad boundary above the parser (M25). A reader raising something
    nobody listed - here a `KeyError` from a provider client, not a PDF - is a
    failed read, and the turn is still released."""

    class Exploding:
        async def read(self, *, content: bytes, mime_type: str | None) -> ReadResult:
            raise KeyError("choices")

    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    worker = h.worker(db_session, tmp_path, settings, reader=Exploding())

    job = await h.run(worker, media)

    await db_session.refresh(media)
    assert media.status is MediaStatus.FAILED
    assert media.last_error == READER_FAILED
    assert "choices" not in (media.last_error or "")
    assert job is not None


async def test_cancellation_is_not_mistaken_for_an_unreadable_file(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """A worker being stopped is not a malformed attachment. The boundary
    catches `Exception`, and cancellation is not one."""

    class Cancelled:
        async def read(self, *, content: bytes, mime_type: str | None) -> ReadResult:
            raise asyncio.CancelledError

    where = await h.scene(db_session)
    media = await h.attachment(db_session, where)
    worker = h.worker(db_session, tmp_path, settings, reader=Cancelled())

    with pytest.raises(asyncio.CancelledError):
        await h.run(worker, media)

    assert h.released(worker) == []


async def test_an_amplifying_pdf_is_killed_on_time_while_the_loop_keeps_running(
    db_session: AsyncSession, tmp_path: Path, settings: Settings
) -> None:
    """P5, reversed, through the worker. The 16 MB-inflating PDF is 25 KB.

    Parsed in-process it froze the loop for 170 s and reached 763 MB. Here a
    ticker on the same loop records its longest gap and the process's resident
    set while the worker handles the file: the child is killed at the
    extraction deadline, the loop never stalls, the parent does not grow, the
    row is terminal, and the conversation is released.
    """
    pdf = amplifying_pdf(16)
    assert len(pdf) < 64 * 1024

    where = await h.scene(db_session)
    media = await h.attachment(
        db_session, where, mime_type="application/pdf", kind=MessageKind.DOCUMENT
    )
    worker = h.worker(
        db_session,
        tmp_path,
        settings,
        whatsapp=h.StubWhatsApp(content=pdf, mime_type="application/pdf"),
    )

    gaps: list[float] = []
    resident: list[int] = []
    baseline = _resident_bytes()
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.05)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
            sample = _resident_bytes()
            if sample is not None:
                resident.append(sample)

    beating = asyncio.create_task(ticker())
    started = time.perf_counter()
    job = await h.run(worker, media)
    elapsed = time.perf_counter() - started
    stop.set()
    await beating

    await db_session.refresh(media)
    assert media.status is MediaStatus.SKIPPED
    assert media.last_error == DocumentBeyondLimitsError.message
    assert job is not None

    # The clock, not the text, stopped it - and stopped it near the deadline.
    assert EXTRACTION_TIMEOUT_SECONDS <= elapsed < EXTRACTION_TIMEOUT_SECONDS + 10
    # The loop kept ticking every ~50 ms throughout: no stall anywhere near the
    # 170 s the in-process parse caused, or even one second.
    assert len(gaps) > EXTRACTION_TIMEOUT_SECONDS * 5
    assert max(gaps) < 1.0
    # The parse's memory lived and died in the child.
    if baseline is not None and resident:
        assert max(resident) - baseline < 150 * 1024 * 1024
