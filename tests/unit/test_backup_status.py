"""How the API can say anything about a backup it never takes.

The backup runs in its own container and exits. Nothing of it is in this
process, so an in-memory counter would vanish with the process that
incremented it and `/metrics` would publish zero for ever while backups quietly
stopped. The durable answer is a small file the backup writes after its
artifact is verified off-host, and this is the reader.

The distinction being defended throughout: **a dump is not a backup**. A
deployment whose `pg_dump` succeeds nightly and whose upload has failed for a
week has no recovery point, and a signal derived from "the newest file in the
backup directory" would call it healthy.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.metrics import MetricsRegistry
from app.services.backup_status import BackupStatus, read_backup_status
from app.services.metrics_service import MetricsService

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def write(path: Path, **overrides: Any) -> Path:
    payload = {
        "outcome": "success",
        "written_at": "2026-09-02T02:17:00Z",
        "last_success_at": "2026-09-02T02:17:00Z",
        "last_success_artifact": "wasla-20260902T021700Z.dump",
        "last_success_bytes": 149668,
        "destination": "s3",
        "failures_total": 0,
        "failed_stage": "",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------- reading


def test_a_written_status_is_read_back(tmp_path: Path) -> None:
    status = read_backup_status(write(tmp_path / "status.json"))

    assert status is not None
    assert status.outcome == "success"
    assert status.destination == "s3"
    assert status.last_success_bytes == 149668
    assert status.last_success_at == datetime(2026, 9, 2, 2, 17, tzinfo=UTC)


def test_the_age_is_measured_from_the_last_durable_success(tmp_path: Path) -> None:
    status = read_backup_status(write(tmp_path / "status.json"))

    assert status is not None
    assert status.age_seconds(now=NOW) == pytest.approx((12 - 2) * 3600 - 17 * 60)


def test_a_deployment_that_has_never_succeeded_has_no_age(tmp_path: Path) -> None:
    """Different from stale, and a different alert.

    Never having backed up is a misconfiguration; having backed up three days
    ago is a breakage. Reporting the first as an enormous age would send
    somebody looking for the wrong thing.
    """
    status = read_backup_status(write(tmp_path / "status.json", last_success_at=""))

    assert status is not None
    assert status.age_seconds(now=NOW) is None


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not json at all", id="not-json"),
        pytest.param("[1, 2, 3]", id="not-an-object"),
        pytest.param("", id="empty"),
    ],
)
def test_an_unusable_status_file_reads_as_absent(tmp_path: Path, content: str) -> None:
    """Read on the scrape path, so it must not raise there.

    A metrics endpoint that fell over because a backup container had written
    half a file would take the signal away at the exact moment it mattered.
    """
    path = tmp_path / "status.json"
    path.write_text(content, encoding="utf-8")

    assert read_backup_status(path) is None


def test_a_missing_status_file_reads_as_absent(tmp_path: Path) -> None:
    assert read_backup_status(tmp_path / "nothing.json") is None


def test_nonsense_field_types_do_not_crash_the_reader(tmp_path: Path) -> None:
    status = read_backup_status(
        write(
            tmp_path / "status.json",
            last_success_bytes="lots",
            failures_total=None,
            last_success_at="the day before yesterday",
        )
    )

    assert status is not None
    assert status.last_success_bytes == 0
    assert status.failures_total == 0
    assert status.last_success_at is None


# ---------------------------------------------------------------- publishing


async def sample(path: Path, *, now: datetime = NOW) -> str:
    service = MetricsService(None, registry=MetricsRegistry(), backup_status_path=str(path))
    return await service.render(now=now)


async def test_the_exposition_carries_the_age_of_the_last_durable_backup(tmp_path: Path) -> None:
    body = await sample(write(tmp_path / "status.json"))

    assert "wasla_backup_last_success_timestamp_seconds " in body
    assert "wasla_backup_age_seconds " in body
    assert "wasla_backup_failures_total" in body


async def test_a_stale_backup_is_visible_as_a_large_age(tmp_path: Path) -> None:
    """No sleeping: the timestamps are fixed and the age is arithmetic."""
    write(tmp_path / "status.json", last_success_at="2026-08-29T02:17:00Z")

    body = await sample(tmp_path / "status.json")

    line = next(
        entry for entry in body.splitlines() if entry.startswith("wasla_backup_age_seconds ")
    )
    age = float(line.rsplit(" ", 1)[1])
    assert age > 4 * 24 * 3600, "four days without a durable backup must read as four days"


async def test_a_deployment_with_no_status_path_publishes_nothing(tmp_path: Path) -> None:
    """An absent series says "cannot tell you", which is truer than a zero."""
    service = MetricsService(None, registry=MetricsRegistry(), backup_status_path=None)

    body = await service.render(now=NOW)

    assert "wasla_backup_" not in body


async def test_a_deployment_whose_status_file_is_missing_publishes_nothing(tmp_path: Path) -> None:
    body = await sample(tmp_path / "never-written.json")

    assert "wasla_backup_" not in body


async def test_failures_are_published_by_the_stage_that_failed(tmp_path: Path) -> None:
    """`stage` is a bounded set the script chooses, never a message."""
    write(tmp_path / "status.json", outcome="failure", failures_total=3, failed_stage="upload")

    body = await sample(tmp_path / "status.json")

    assert 'wasla_backup_failures_total{stage="upload"} 3' in body


async def test_a_failed_run_still_reports_the_last_good_backup(tmp_path: Path) -> None:
    """The whole reason the status file carries the previous success forward.

    Last night's upload failed. The deployment still has yesterday's backup,
    and the age must say so rather than resetting to nothing.
    """
    write(
        tmp_path / "status.json",
        outcome="failure",
        failed_stage="upload",
        failures_total=1,
        last_success_at="2026-09-01T02:17:00Z",
    )

    body = await sample(tmp_path / "status.json")

    assert "wasla_backup_age_seconds " in body
    assert 'wasla_backup_failures_total{stage="upload"} 1' in body


async def test_nothing_in_the_exposition_names_a_bucket_or_a_credential(tmp_path: Path) -> None:
    """The status file is mounted into the API and ends up in support tickets."""
    write(tmp_path / "status.json", destination="s3")

    body = await sample(tmp_path / "status.json")

    for forbidden in ("AKIA", "secret", "bucket", "endpoint", "amazonaws", "password"):
        assert forbidden not in body.lower()


def test_the_status_dataclass_carries_no_credential_field() -> None:
    """Asserted structurally, so adding one is a deliberate act somebody sees."""
    assert set(BackupStatus.__slots__) == {
        "destination",
        "failed_stage",
        "failures_total",
        "last_success_at",
        "last_success_bytes",
        "outcome",
    }
