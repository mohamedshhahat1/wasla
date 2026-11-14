"""An upload takes no lock and holds no connection while a PDF is parsed (RV-10, RQ-09, P-R08).

The parse is slow for real - a real extraction child started late through a
test seam (`slow_pdf_child.py`), longer than the session's 5 s `lock_timeout`.
The plan's lock is the same advisory lock `DocumentSlotDep` took before the
handler ran; the uploads go through `KnowledgeService.submit` exactly as the
route calls it.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

import app.services.extraction as extraction
from app.db.models.billing import LimitKey
from app.db.models.knowledge import DocumentSource
from app.services.entitlement_service import _lock_id
from app.services.knowledge_service import KnowledgeService
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration

PDFS = Path(__file__).resolve().parents[1] / "fixtures" / "rag" / "pdf"
DELAY = 6.0


@pytest.fixture
def slow_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extraction, "CHILD_SCRIPT", Path(__file__).with_name("slow_pdf_child.py"))
    monkeypatch.setenv("WASLA_TEST_PDF_DELAY", str(DELAY))
    monkeypatch.setattr(
        extraction, "_child_environment", lambda: {**_env(), "WASLA_TEST_PDF_DELAY": str(DELAY)}
    )
    monkeypatch.setattr(extraction, "EXTRACTION_TIMEOUT_SECONDS", 30)


def _env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if k in extraction.CHILD_ENVIRONMENT}


async def _upload(
    indexing: Indexing, tenant_id: uuid.UUID, raw: str, source: DocumentSource
) -> Any:
    async with indexing.database.session() as session:
        knowledge = KnowledgeService(session=session, tenant_id=tenant_id, queue=indexing.queue())
        base = await knowledge.ensure_default_knowledge_base()

        async def reserve_slot() -> object:
            # What `DocumentSlotDep` did, before the handler, until RQ-09.
            return await session.execute(
                select(
                    func.pg_advisory_xact_lock(_lock_id(tenant_id, LimitKey.KNOWLEDGE_DOCUMENTS))
                )
            )

        view, created = await knowledge.submit(
            knowledge_base_id=base.id,
            title=f"Upload {uuid.uuid4().hex[:6]}",
            raw=raw,
            source=source,
            reserve_slot=reserve_slot,
            release_for_extraction=True,
        )
        return view, created


async def test_two_uploads_to_one_workspace_succeed_while_a_slow_pdf_parses(
    indexing: Indexing, slow_child: None
) -> None:
    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        await KnowledgeService(session=session, tenant_id=tenant_id).ensure_default_knowledge_base()
    pdf = base64.b64encode((PDFS / "word-EN-1.pdf").read_bytes()).decode()
    checked_out: list[int] = []

    async def watch() -> None:
        await asyncio.sleep(DELAY / 2)
        checked_out.append(indexing.database.engine.pool.checkedout())  # type: ignore[attr-defined]

    slow = asyncio.create_task(_upload(indexing, tenant_id, pdf, DocumentSource.PDF))
    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0.5)
    # Another upload to the same workspace, mid-parse: it needs the plan's lock.
    quick = await asyncio.wait_for(
        _upload(indexing, tenant_id, "Opening hours are nine to five.", DocumentSource.TEXT), DELAY
    )
    await watcher
    (_, created_slow), (_, created_quick) = await slow, quick
    assert created_slow and created_quick
    # Mid-parse, the slow upload held no pooled connection.
    assert checked_out == [0]


class _ParseWindow:
    """When the burst is inside extraction: every upload in, and the first one out.

    Counted at `extraction.extract_pdf_pages_bounded`, which an upload reaches
    only after `released` has handed its connection back, so the window holds
    the uploads' parse and nothing else (FEB-33).
    """

    def __init__(self, expected: int) -> None:
        self._expected = expected
        self._entered = 0
        self.all_entered = asyncio.Event()
        self.first_left = asyncio.Event()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bounded = extraction.extract_pdf_pages_bounded

        async def counted(*args: Any, **kwargs: Any) -> Any:
            self._entered += 1
            if self._entered >= self._expected:
                self.all_entered.set()
            try:
                return await bounded(*args, **kwargs)
            finally:
                self.first_left.set()

        monkeypatch.setattr(extraction, "extract_pdf_pages_bounded", counted)


async def test_the_parse_window_opens_when_the_last_upload_is_in_and_closes_with_the_first_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window's own rule (FEB-33): an upload still in setup never opens it."""
    release = asyncio.Event()

    async def parse(*args: Any, **kwargs: Any) -> list[str]:
        await release.wait()
        return []

    monkeypatch.setattr(extraction, "extract_pdf_pages_bounded", parse)
    window = _ParseWindow(3)
    window.install(monkeypatch)

    async def settle() -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    parses = [asyncio.create_task(extraction.extract_pdf_pages_bounded(b"")) for _ in range(2)]
    await settle()
    assert not window.all_entered.is_set(), "two of three in: the window must stay shut"
    parses.append(asyncio.create_task(extraction.extract_pdf_pages_bounded(b"")))
    await settle()
    assert window.all_entered.is_set()
    assert not window.first_left.is_set()
    release.set()
    await asyncio.gather(*parses)
    assert window.first_left.is_set()


async def test_a_burst_of_slow_uploads_does_not_exhaust_the_pool(
    indexing: Indexing, slow_child: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await indexing.workspace()
    async with indexing.database.session() as session:
        await KnowledgeService(session=session, tenant_id=tenant_id).ensure_default_knowledge_base()
    pool = indexing.database.engine.pool
    burst = pool.size() + 3  # type: ignore[attr-defined]
    pdfs = [
        base64.b64encode((PDFS / name).read_bytes()).decode()
        for name in sorted(p.name for p in PDFS.glob("*.pdf"))[:burst]
    ]
    peak: list[int] = []
    window = _ParseWindow(burst)
    window.install(monkeypatch)

    async def watch() -> None:
        # While every upload is parsing or waiting for a parse slot, and until
        # the first parse ends - opened by that condition, never by a clock.
        # Before it each upload briefly holds a connection for its own reads,
        # which is not what this test is about; a fixed one-second start
        # counted them whenever the burst started slowly (7 of 5 once in the
        # migration lane, FEB-33).
        await asyncio.wait_for(window.all_entered.wait(), timeout=DELAY * 4)
        while not window.first_left.is_set():
            peak.append(pool.checkedout())  # type: ignore[attr-defined]
            await asyncio.sleep(0.25)

    watcher = asyncio.create_task(watch())
    results = await asyncio.gather(
        *(_upload(indexing, tenant_id, pdf, DocumentSource.PDF) for pdf in pdfs),
        return_exceptions=True,
    )
    await watcher
    refused = [r for r in results if isinstance(r, BaseException)]
    assert not refused, refused
    assert peak, "the parse window never opened"
    # Extraction is bounded to two at a time per process; queued uploads wait
    # for a slot holding no connection, so the pool never fills.
    assert max(peak) < pool.size()  # type: ignore[attr-defined]
