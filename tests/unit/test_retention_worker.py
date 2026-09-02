"""The retention loop: what it runs, in what order, and what stops it.

The service is tested against a real database elsewhere. What is left here is
the loop itself - that a deployment which has not configured retention runs it
harmlessly, that reconciliation happens before new claims, that a failing sweep
does not kill the loop, and that a stop request does not wait out a day.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from app.core.config import Settings
from app.core.storage import LocalMediaStorage
from app.services.media_retention_service import RetentionOutcome
from app.workers import runner
from app.workers.retention_worker import RetentionWorker

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        **overrides,
    )


class RecordingService:
    """Stands in for the service, remembering the order it was called in."""

    calls: ClassVar[list[str]] = []

    def __init__(self, *, session, storage) -> None:
        self.session = session
        self.storage = storage

    async def reconcile(self, *, limit: int) -> int:
        RecordingService.calls.append(f"reconcile:{limit}")
        return 1

    async def sweep(self, *, now, retention_days: int, limit: int) -> RetentionOutcome:
        RecordingService.calls.append(f"sweep:{retention_days}:{limit}")
        return RetentionOutcome(claimed=2, purged=2)

    async def pending_count(self) -> int:
        RecordingService.calls.append("pending")
        return 0


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeDatabase:
    def __init__(self) -> None:
        self.sessions = 0

    def session(self) -> FakeSession:
        self.sessions += 1
        return FakeSession()


@pytest.fixture(autouse=True)
def recording(monkeypatch):
    RecordingService.calls = []
    monkeypatch.setattr(
        "app.workers.retention_worker.MediaRetentionService",
        RecordingService,
    )
    return RecordingService


def _worker(tmp_path, **overrides) -> RetentionWorker:
    return RetentionWorker(
        database=FakeDatabase(),  # type: ignore[arg-type]
        settings=_settings(**overrides),
        storage=LocalMediaStorage(tmp_path),
        poll_seconds=0.01,
    )


async def test_reconciliation_runs_before_new_claims(tmp_path, recording) -> None:
    """Work already decided is finished before more is taken on.

    A store refusing deletions would otherwise accumulate an ever-growing set of
    half-done rows behind an ever-growing set of new claims.
    """
    await _worker(tmp_path, media_retention_days=30).run_once(now=NOW)

    assert recording.calls[0].startswith("reconcile")
    assert recording.calls[1].startswith("sweep")


async def test_the_configured_period_and_batch_reach_the_sweep(tmp_path, recording) -> None:
    await _worker(tmp_path, media_retention_days=45, media_retention_batch_size=17).run_once(
        now=NOW
    )

    assert "sweep:45:17" in recording.calls
    assert "reconcile:17" in recording.calls


async def test_a_deployment_with_no_retention_still_runs_the_loop(tmp_path, recording) -> None:
    """Zero disables deleting, not the loop.

    The reconciliation pass still has to run: a deployment that turns retention
    *off* after a failed sweep would otherwise strand every row that sweep had
    claimed, with their objects still in the store and no query looking for them.
    """
    await _worker(tmp_path, media_retention_days=0).run_once(now=NOW)

    assert any(call.startswith("reconcile") for call in recording.calls)
    assert "sweep:0:200" in recording.calls


async def test_a_failing_sweep_does_not_kill_the_loop(tmp_path, monkeypatch) -> None:
    """A store unavailable for a day is not a reason to stop trying."""
    attempts = 0

    async def explode(self, **kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("the store is on fire")

    monkeypatch.setattr(RecordingService, "reconcile", explode)
    worker = _worker(tmp_path, media_retention_days=30)

    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    worker.stop()
    await asyncio.wait_for(task, timeout=2)

    assert attempts > 1, "the loop stopped after the first failure"


async def test_stopping_does_not_wait_out_the_interval(tmp_path) -> None:
    """A container is killed ten seconds after SIGTERM, and this polls daily."""
    worker = RetentionWorker(
        database=FakeDatabase(),  # type: ignore[arg-type]
        settings=_settings(media_retention_days=30),
        storage=LocalMediaStorage(tmp_path),
        poll_seconds=86_400.0,
    )

    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    worker.stop()

    await asyncio.wait_for(task, timeout=2)


# ------------------------------------------------------------------ wiring


def test_retention_is_one_of_the_worker_kinds() -> None:
    assert "retention" in runner.ALL_KINDS


def test_a_deployment_gets_the_retention_loop_by_default() -> None:
    """Nothing in `WORKER_KINDS` means every loop, and this is one of them.

    A deployment that had to name it would be a deployment where forgetting to
    is a media volume that grows for ever with nothing to say so.
    """
    assert "retention" in runner.selected_kinds("")


def test_the_loop_can_be_run_apart_from_the_others() -> None:
    assert runner.selected_kinds("retention") == ("retention",)


def test_building_the_retention_worker_needs_no_redis(tmp_path) -> None:
    """It polls PostgreSQL, like the other time-triggered loops (ADR-022).

    Worth asserting because a queue-driven retention would put the deletion of
    customer data behind the replay command, where an operator could re-run a
    dead-lettered purge weeks later against a row since re-populated.
    """
    workers = runner.build_workers(
        kinds=["retention"],
        database=FakeDatabase(),  # type: ignore[arg-type]
        redis=None,  # type: ignore[arg-type]
        settings=_settings(media_storage_path=str(tmp_path)),
    )

    assert len(workers) == 1
    assert isinstance(workers[0], RetentionWorker)
