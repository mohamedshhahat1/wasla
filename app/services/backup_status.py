"""What the API can say about backups it does not take.

The backup runs in its own container, on its own schedule, and exits. Nothing
of it is in this process — so an in-memory counter incremented inside the
backup would vanish with the process that incremented it, and a `/metrics`
endpoint that pretended otherwise would publish zero for ever while backups
quietly stopped.

The durable answer is a small file. `backup_postgres.sh` writes it after the
artifact is verified at its off-host destination, and the API reads it and
publishes the age of the last **complete** backup. That distinction is the
whole value: alerting on "the newest file in the backup directory" would call a
deployment healthy whose dumps have been failing to upload for a week, because
the dumps themselves keep succeeding.

**Read-only, and nothing in it is a secret.** A filename, a byte count, a
destination kind, two timestamps and a failure count. No bucket, no endpoint,
no credential — the file is mounted read-only into the API and is exactly the
sort of thing that gets pasted into a support ticket (ADR-075).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BackupStatus:
    """The last thing the backup process wrote down."""

    last_success_at: datetime | None
    outcome: str
    destination: str
    last_success_bytes: int
    failures_total: int
    failed_stage: str

    def age_seconds(self, *, now: datetime | None = None) -> float | None:
        """How long since a backup last reached its destination.

        None when there has never been one, which is a different alert from a
        stale one: a deployment that has never backed up successfully is
        misconfigured, and one whose last success was three days ago is broken.
        """
        if self.last_success_at is None:
            return None
        return max(0.0, ((now or datetime.now(UTC)) - self.last_success_at).total_seconds())


def read_backup_status(path: str | Path) -> BackupStatus | None:
    """Read the status file, or None if there is not a usable one.

    Every failure answers None rather than raising. This is read on the scrape
    path, and a metrics endpoint that fell over because a backup container had
    written half a file would take away the signal at the exact moment it
    mattered. An absent series is itself alertable, which is the honest way to
    report "this deployment cannot tell you about its backups".
    """
    location = Path(path)
    try:
        raw = location.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning(
            "backup.status_unreadable",
            extra={"event": "backup.status_unreadable"},
        )
        return None
    if not isinstance(payload, dict):
        return None

    return BackupStatus(
        last_success_at=_moment(payload.get("last_success_at")),
        outcome=_text(payload.get("outcome"), "unknown"),
        destination=_text(payload.get("destination"), "unknown"),
        last_success_bytes=_count(payload.get("last_success_bytes")),
        failures_total=_count(payload.get("failures_total")),
        failed_stage=_text(payload.get("failed_stage"), ""),
    )


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _text(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))


__all__ = ["BackupStatus", "read_backup_status"]
