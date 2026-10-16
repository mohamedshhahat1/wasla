"""A campaign's activity log, read from the audit trail (PG-05, ADR-149).

The trail already records who composed, targeted, scheduled, paused and
cancelled a campaign, and the system's own stops and waits. The page shows the
same entries with a closed vocabulary of what happened and only the metadata
that is safe to show any member: field names, counts, reasons and times -
never a contact's identity or a parameter's value. An entry the trail gains
later that this module does not know is left out, not shown raw.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from app.db.models.audit import AuditAction, AuditActorKind, AuditLog


class ActivityKind(StrEnum):
    """What happened to a campaign. Closed."""

    CREATED = "created"
    UPDATED = "updated"
    DUPLICATED_FROM = "duplicated_from"
    DUPLICATED_TO = "duplicated_to"
    AUDIENCE_SET = "audience_set"
    SCHEDULED = "scheduled"
    RESUMED = "resumed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    DELETED = "deleted"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    TEST_SENT = "test_sent"
    RECIPIENTS_EXPORTED = "recipients_exported"


#: The metadata each action may show, and nothing else.
SAFE_KEYS: Final[dict[AuditAction, frozenset[str]]] = {
    AuditAction.CAMPAIGN_CREATED: frozenset({"messages_per_minute"}),
    AuditAction.CAMPAIGN_UPDATED: frozenset({"fields", "audience_cleared"}),
    AuditAction.CAMPAIGN_DUPLICATED: frozenset({"source_id"}),
    AuditAction.CAMPAIGN_AUDIENCE_SET: frozenset({"recipients"}),
    AuditAction.CAMPAIGN_SCHEDULED: frozenset({"recipients", "scheduled_at", "send_until"}),
    AuditAction.CAMPAIGN_PAUSED: frozenset({"reason"}),
    AuditAction.CAMPAIGN_CANCELLED: frozenset({"reason"}),
    AuditAction.CAMPAIGN_DELETED: frozenset({"recipients"}),
    AuditAction.CAMPAIGN_WAITING: frozenset({"reason", "resumes_at"}),
    AuditAction.CAMPAIGN_COMPLETED: frozenset(),
    AuditAction.CAMPAIGN_FAILED: frozenset({"reason"}),
    # Which contact a test went to is not shown: the admin's own, by rule.
    AuditAction.CAMPAIGN_TEST_SENT: frozenset(),
    AuditAction.CAMPAIGN_RECIPIENTS_EXPORTED: frozenset({"rows"}),
}
# Filters are shown by name - an audience's and an export's - and hand-picked
# people only as a count: `contact_ids` is already a count in the trail (CMP-14).
_FILTER_KEYS: Final = frozenset(
    {"last_inbound_within_days", "lead_statuses", "contact_ids", "status", "reason_code"}
)

_KINDS: Final[dict[AuditAction, ActivityKind]] = {
    AuditAction.CAMPAIGN_CREATED: ActivityKind.CREATED,
    AuditAction.CAMPAIGN_UPDATED: ActivityKind.UPDATED,
    AuditAction.CAMPAIGN_AUDIENCE_SET: ActivityKind.AUDIENCE_SET,
    AuditAction.CAMPAIGN_PAUSED: ActivityKind.PAUSED,
    AuditAction.CAMPAIGN_CANCELLED: ActivityKind.CANCELLED,
    AuditAction.CAMPAIGN_DELETED: ActivityKind.DELETED,
    AuditAction.CAMPAIGN_WAITING: ActivityKind.WAITING,
    AuditAction.CAMPAIGN_COMPLETED: ActivityKind.COMPLETED,
    AuditAction.CAMPAIGN_FAILED: ActivityKind.FAILED,
    AuditAction.CAMPAIGN_TEST_SENT: ActivityKind.TEST_SENT,
    AuditAction.CAMPAIGN_RECIPIENTS_EXPORTED: ActivityKind.RECIPIENTS_EXPORTED,
}


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    """One line of a campaign's activity."""

    id: uuid.UUID
    kind: ActivityKind
    occurred_at: datetime
    actor_type: str
    actor_id: uuid.UUID | None
    actor_name: str | None
    metadata: dict[str, Any]


def _kind(entry: AuditLog, campaign_id: uuid.UUID) -> ActivityKind | None:
    if entry.action is AuditAction.CAMPAIGN_DUPLICATED:
        # The copy's own first line, or "a copy was made" on the original.
        return (
            ActivityKind.DUPLICATED_FROM
            if entry.target_id == campaign_id
            else ActivityKind.DUPLICATED_TO
        )
    if entry.action is AuditAction.CAMPAIGN_SCHEDULED:
        resumed = (entry.meta or {}).get("resumed")
        return ActivityKind.RESUMED if resumed else ActivityKind.SCHEDULED
    return _KINDS.get(entry.action)


def _metadata(entry: AuditLog, kind: ActivityKind) -> dict[str, Any]:
    meta = entry.meta or {}
    if kind is ActivityKind.DUPLICATED_TO:
        return {"copy_id": str(entry.target_id)}
    allowed = SAFE_KEYS.get(entry.action, frozenset())
    shown = {key: meta[key] for key in allowed if key in meta}
    filters = meta.get("filters")
    if kind in (ActivityKind.AUDIENCE_SET, ActivityKind.RECIPIENTS_EXPORTED) and isinstance(
        filters, dict
    ):
        shown["filters"] = {key: value for key, value in filters.items() if key in _FILTER_KEYS}
    return shown


def activity_entry(
    entry: AuditLog, actor_name: str | None, campaign_id: uuid.UUID
) -> ActivityEntry | None:
    """The page's view of one audit entry; None for one it does not show."""
    kind = _kind(entry, campaign_id)
    if kind is None:
        return None
    is_user = entry.actor_kind is not AuditActorKind.SYSTEM and entry.actor_id is not None
    return ActivityEntry(
        id=entry.id,
        kind=kind,
        occurred_at=entry.occurred_at,
        actor_type="user" if is_user else "system",
        actor_id=entry.actor_id if is_user else None,
        actor_name=actor_name if is_user else None,
        metadata=_metadata(entry, kind),
    )


__all__ = ["SAFE_KEYS", "ActivityEntry", "ActivityKind", "activity_entry"]
