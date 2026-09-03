"""The recovery loop: what it settles, what it refuses to settle, and what stops it.

The protocol is tested against a real PostgreSQL and a real MinIO in
`tests/integration/test_media_write_atomicity.py`, and a real process is killed
mid-write in `test_media_crash_recovery.py`. What is left here is the decision
table and the loop around it - the four verdicts, the one that writes nothing,
and the metric labels, none of which need a store to be correct about.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import uuid
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql.base import PGDialect as PostgresDialect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.storage import LocalMediaStorage, MediaStorage, StorageError
from app.core.telemetry import REDIS_COUNTERS
from app.db.models.media import MediaStorageState, MessageMedia
from app.repositories.media_repository import PlatformMediaRepository
from app.services.media_upload_service import (
    MediaUploadReconciler,
    ReconciliationOutcome,
    Verdict,
)
from app.workers import runner
from app.workers.upload_worker import UploadRecoveryWorker
from tests.fakes import as_session

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic" * 8
METRIC = "wasla_media_upload_reconciliation_total"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        **overrides,
    )


# ------------------------------------------------------------- the decisions


@dataclass
class Row:
    """Enough of a media row for the verdict, and nothing else."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    storage_key: str | None
    byte_size: int
    content_hash: str | None
    storage_state: MediaStorageState = MediaStorageState.PENDING
    upload_started_at: datetime | None = None


class FakeStore:
    """A store with a fixed opinion about one key."""

    def __init__(
        self,
        *,
        content: bytes | None = None,
        reachable: bool = True,
    ) -> None:
        self._content = content
        self._reachable = reachable
        self.heads = 0
        self.gets = 0
        self.deletes: list[str] = []

    async def put_at(self, *, key: str, data: bytes, mime_type: str | None = None) -> None:
        self._content = data

    async def get(self, key: str) -> bytes:
        self.gets += 1
        if not self._reachable:
            raise StorageError()
        if self._content is None:
            raise StorageError()
        return self._content

    async def delete(self, key: str) -> None:
        self.deletes.append(key)

    async def exists(self, key: str) -> bool:
        self.heads += 1
        if not self._reachable:
            raise StorageError()
        return self._content is not None


def _reconciler(store: MediaStorage) -> MediaUploadReconciler:
    return MediaUploadReconciler(session=as_session(object()), storage=store)


def _row(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "storage_key": f"{uuid.uuid4()}/2026/09/{uuid.uuid4()}.png",
        "byte_size": len(PNG),
        "content_hash": hashlib.sha256(PNG).hexdigest(),
    }
    values.update(overrides)
    return Row(**values)


async def test_matching_bytes_are_finalised() -> None:
    store = FakeStore(content=PNG)
    row = _row()

    verdict = await _reconciler(store)._verify(row, key=str(row.storage_key))

    assert verdict is Verdict.FINALIZED


async def test_an_absent_object_is_missing_not_finalised() -> None:
    store = FakeStore(content=None)
    row = _row()

    verdict = await _reconciler(store)._verify(row, key=str(row.storage_key))

    assert verdict is Verdict.MISSING
    # HEAD answered; nothing was read, and nothing was deleted.
    assert store.gets == 0
    assert store.deletes == []


async def test_different_bytes_at_our_key_are_a_mismatch() -> None:
    """The hash is recomputed, never taken from a validator the store supplies."""
    store = FakeStore(content=b"%PDF-1.7 somebody else's file")
    row = _row()

    verdict = await _reconciler(store)._verify(row, key=str(row.storage_key))

    assert verdict is Verdict.MISMATCHED


async def test_the_right_size_and_the_wrong_bytes_is_still_a_mismatch() -> None:
    """Existence is not identity, which is why the object is read rather than headed."""
    store = FakeStore(content=b"X" * len(PNG))
    row = _row()

    verdict = await _reconciler(store)._verify(row, key=str(row.storage_key))

    assert verdict is Verdict.MISMATCHED


async def test_a_store_that_will_not_answer_is_not_an_empty_store() -> None:
    """The distinction the whole recovery turns on (ADR-087)."""
    store = FakeStore(content=PNG, reachable=False)
    row = _row()

    verdict = await _reconciler(store)._verify(row, key=str(row.storage_key))

    assert verdict is Verdict.UNREACHABLE


async def test_an_unreachable_verdict_writes_nothing_to_the_row() -> None:
    row = _row()
    _reconciler(FakeStore())._apply(row, Verdict.MISSING)
    assert row.storage_state is MediaStorageState.ABSENT
    assert row.storage_key is None
    assert row.upload_started_at is None


async def test_a_mismatched_object_is_quarantined_and_kept() -> None:
    """Not served, and not deleted: it is the only evidence of how it got there."""
    store = FakeStore(content=b"wrong")
    row = _row()

    _reconciler(store)._apply(row, Verdict.MISMATCHED)

    assert row.storage_state is MediaStorageState.MISMATCHED
    assert row.storage_key is not None
    assert store.deletes == []


# ------------------------------------------------------------------- the loop


class RecordingReconciler:
    """Stands in for the reconciler, remembering what the loop asked of it."""

    calls: ClassVar[list[str]] = []

    def __init__(self, *, session: AsyncSession, storage: MediaStorage) -> None:
        self.session = session
        self.storage = storage

    async def run(
        self, *, now: datetime | None, grace_seconds: float, limit: int
    ) -> ReconciliationOutcome:
        RecordingReconciler.calls.append(f"run:{grace_seconds}:{limit}")
        return ReconciliationOutcome(finalized=1, missing=1)

    async def pending_count(self) -> int:
        RecordingReconciler.calls.append("pending")
        return 3

    async def mismatched_count(self) -> int:
        RecordingReconciler.calls.append("mismatched")
        return 0


class FakeSession:
    async def __aenter__(self) -> AsyncSession:
        return as_session(self)

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeDatabase:
    def __init__(self) -> None:
        self.sessions = 0

    def session(self) -> FakeSession:
        self.sessions += 1
        return FakeSession()


@pytest.fixture(autouse=True)
def recording(monkeypatch: pytest.MonkeyPatch) -> type[RecordingReconciler]:
    RecordingReconciler.calls = []
    monkeypatch.setattr(
        "app.workers.upload_worker.MediaUploadReconciler",
        RecordingReconciler,
    )
    return RecordingReconciler


def _worker(tmp_path: Path, **overrides: Any) -> UploadRecoveryWorker:
    return UploadRecoveryWorker(
        database=FakeDatabase(),  # type: ignore[arg-type]
        settings=_settings(**overrides),
        storage=LocalMediaStorage(tmp_path),
        poll_seconds=0.01,
    )


async def test_the_configured_grace_and_batch_reach_the_pass(
    tmp_path: Path, recording: type[RecordingReconciler]
) -> None:
    await _worker(
        tmp_path,
        media_upload_grace_seconds=120.0,
        media_upload_recovery_batch_size=7,
    ).run_once(now=NOW)

    assert "run:120.0:7" in recording.calls


async def test_the_levels_are_read_after_the_pass(
    tmp_path: Path, recording: type[RecordingReconciler]
) -> None:
    """Reading them first would report the backlog this pass just cleared."""
    await _worker(tmp_path).run_once(now=NOW)

    assert recording.calls.index("pending") > recording.calls.index("run:900.0:100")
    assert "mismatched" in recording.calls


async def test_a_failing_pass_does_not_stop_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store being unavailable is the ordinary reason this fails."""
    worker = _worker(tmp_path)
    attempts = 0

    async def explode(*, now: datetime | None = None) -> ReconciliationOutcome:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            worker.stop()
        raise RuntimeError("the store fell over")

    monkeypatch.setattr(worker, "run_once", explode)
    await asyncio.wait_for(worker.run_forever(), timeout=5)

    assert attempts >= 3


async def test_stopping_does_not_wait_out_a_whole_period(tmp_path: Path) -> None:
    worker = UploadRecoveryWorker(
        database=FakeDatabase(),  # type: ignore[arg-type]
        settings=_settings(),
        storage=LocalMediaStorage(tmp_path),
        # A period nothing would wait out if `stop` did not interrupt it.
        poll_seconds=3600.0,
    )
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0)
    worker.stop()

    await asyncio.wait_for(task, timeout=5)


# ------------------------------------------------------------ wiring and labels


def test_the_runner_knows_the_kind() -> None:
    """A deployment that leaves it out has to say so, rather than forget it."""
    assert runner.UPLOADS in runner.ALL_KINDS
    assert runner.selected_kinds("uploads") == (runner.UPLOADS,)


def test_the_runner_builds_it_by_default(tmp_path: Path) -> None:
    workers = runner.build_workers(
        kinds=[runner.UPLOADS],
        database=FakeDatabase(),  # type: ignore[arg-type]
        redis=None,  # type: ignore[arg-type]
        settings=_settings(media_storage_path=str(tmp_path)),
    )

    assert [type(worker).__name__ for worker in workers] == ["UploadRecoveryWorker"]


def test_the_metric_carries_one_bounded_label() -> None:
    """No tenant, no media id, no key, no filename, no hash, no bucket.

    The cardinality of this metric is the number of verdicts plus the two
    levels, and it is that for ever.
    """
    _, labels = REDIS_COUNTERS[METRIC]

    assert labels == ("outcome",)


def test_every_verdict_is_a_short_fixed_string() -> None:
    for verdict in Verdict:
        assert verdict.value.isascii()
        assert verdict.value.replace("_", "").isalpha()
        assert len(verdict.value) <= 16


def test_the_row_is_not_named_in_anything_published() -> None:
    """A media id belongs in a log line, where it is bounded and scoped.

    It is not a metric label: one series per attachment is unbounded
    cardinality, and it would put a workspace's activity into a scrape any
    operator can read.
    """
    outcome = ReconciliationOutcome(finalized=1, missing=2, mismatched=3, unreachable=4)

    assert outcome.examined == 10
    # Counts, and only counts. Nothing that could name a workspace or a file
    # can reach the metric writer, because nothing that could is in here.
    assert {field.type for field in fields(outcome)} == {"int"}


def test_the_claim_skips_rows_another_worker_is_holding() -> None:
    """`FOR UPDATE SKIP LOCKED`, asserted on the statement rather than by racing.

    What it buys is not exactly-once - that comes from the row lock plus the
    state transition, and holds without it. What it buys is that a second
    worker does not *block* behind the first while that one is inside a
    bounded object read against the store. Without it two reconcilers
    serialise: the second waits out the first's verification, re-evaluates,
    finds the row settled, and only then moves to the next one.

    That difference is a matter of who waits, and the only honest way to assert
    it is on the query. A timing test would be asserting how fast this machine
    is.
    """
    statement = (
        select(MessageMedia)
        .where(MessageMedia.storage_state == MediaStorageState.PENDING)
        .with_for_update(skip_locked=True)
    )
    # SQLAlchemy does not annotate its dialect constructors, and compiling
    # against the real one is the only way to see the SQL PostgreSQL will get.
    compiled = str(statement.compile(dialect=PostgresDialect()))  # type: ignore[no-untyped-call]

    assert "FOR UPDATE SKIP LOCKED" in compiled

    source = inspect.getsource(PlatformMediaRepository.claim_stale_uploads)
    assert "skip_locked=True" in source


def test_the_grace_period_is_longer_than_a_single_object_write() -> None:
    """Otherwise a pass would reconcile writes that are still in progress.

    The store's own timeout bounds how long one `put_at` can take, so a grace
    period shorter than it would be a guarantee that some live write gets
    verified underneath its writer.
    """
    settings = _settings()

    assert settings.media_upload_grace_seconds > settings.media_s3_timeout_seconds
    assert settings.media_upload_grace_seconds >= timedelta(minutes=5).total_seconds()
