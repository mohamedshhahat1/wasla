"""A workspace's automatic follow-up settings (FUS-01, ADR-151).

Any member reads them; an owner or an administrator changes them, naming the
version they edited (`If-Match`), and the change is audited, old and new. The
card that serves them is "إعدادات المتابعة": a toggle, the platforms, and up to
five reminders of five minutes to 23 hours each.

"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.registry import ChannelRegistry, default_registry
from app.core.config import get_settings
from app.core.exceptions import ConflictError, ValidationError
from app.db.models.audit import AuditAction
from app.db.models.channel import Channel
from app.db.models.follow_up_settings import (
    DEFAULT_CHANNEL_TYPES,
    DEFAULT_ENABLED,
    DEFAULT_STEPS,
    MAX_GUIDANCE_LENGTH,
    MAX_STEP_DELAY_MINUTES,
    MAX_STEPS,
    MIN_STEP_DELAY_MINUTES,
    FollowUpSettings,
)
from app.db.models.user import User
from app.services.audit_service import AuditTrail
from app.services.personalisation import normalise

STEPS_REQUIRED_CODE: Final = "follow_up_steps_required"
CHANNELS_REQUIRED_CODE: Final = "follow_up_channels_required"
CHANNEL_UNAVAILABLE_CODE: Final = "channel_type_unavailable"
DELAY_OUT_OF_RANGE_CODE: Final = "follow_up_delay_out_of_range"
TOO_MANY_STEPS_CODE: Final = "follow_up_too_many_steps"
GUIDANCE_TOO_LONG_CODE: Final = "follow_up_guidance_too_long"
SETTINGS_CHANGED_CODE: Final = "follow_up_settings_changed"


@dataclass(frozen=True, slots=True)
class Step:
    """One reminder: how long after the anchor, and what the AI is told for it."""

    delay_minutes: int
    guidance: str | None

    def as_json(self) -> dict[str, Any]:
        return {"delay_minutes": self.delay_minutes, "guidance": self.guidance}


@dataclass(frozen=True, slots=True)
class SequenceSettings:
    """What a workspace's sequences are, as read now. `version` 0: never configured."""

    enabled: bool
    channel_types: tuple[str, ...]
    steps: tuple[Step, ...]
    version: int
    updated_at: datetime | None
    updated_by_id: uuid.UUID | None

    def covers(self, channel: Channel) -> bool:
        """Whether a conversation on `channel` runs sequences now."""
        return self.enabled and bool(self.steps) and channel.value in self.channel_types

    def as_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "channel_types": list(self.channel_types),
            "steps": [step.as_json() for step in self.steps],
        }


#: A workspace with no row: not configured, so nothing runs (see the model).
UNCONFIGURED: Final = SequenceSettings(
    enabled=False,
    channel_types=DEFAULT_CHANNEL_TYPES,
    steps=tuple(Step(**step) for step in DEFAULT_STEPS),
    version=0,
    updated_at=None,
    updated_by_id=None,
)


def _settings_of(row: FollowUpSettings) -> SequenceSettings:
    return SequenceSettings(
        enabled=row.enabled,
        channel_types=tuple(row.channel_types),
        steps=tuple(
            Step(delay_minutes=int(step["delay_minutes"]), guidance=step.get("guidance"))
            for step in row.steps
        ),
        version=row.version,
        updated_at=row.updated_at,
        updated_by_id=row.updated_by_id,
    )


def available_channel_types(registry: ChannelRegistry | None = None) -> tuple[str, ...]:
    """The channel types this deployment can run reminders on: those with an adapter."""
    channels = (registry or default_registry()).channels
    return tuple(channel.value for channel in Channel if channel in channels)


def default_settings_row(tenant_id: uuid.UUID) -> FollowUpSettings:
    """The owner's default for a new workspace: on, WhatsApp, one reminder after five minutes.

    WhatsApp is the default platform when it is available; otherwise the first
    available one (FUS-01).
    """
    available = available_channel_types()
    channels = list(DEFAULT_CHANNEL_TYPES) if "whatsapp" in available else list(available[:1])
    return FollowUpSettings(
        tenant_id=tenant_id,
        enabled=DEFAULT_ENABLED and bool(channels),
        channel_types=channels,
        steps=[dict(step) for step in DEFAULT_STEPS],
        version=1,
    )


async def read_settings(session: AsyncSession, tenant_id: uuid.UUID) -> SequenceSettings:
    """The workspace's settings as the database holds them now; `UNCONFIGURED` without a row."""
    row = await session.scalar(
        select(FollowUpSettings)
        .where(FollowUpSettings.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    return _settings_of(row) if row is not None else UNCONFIGURED


class FollowUpSettingsService:
    """One workspace's reminders."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        registry: ChannelRegistry | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._registry = registry or default_registry()

    def available_channel_types(self) -> tuple[str, ...]:
        return available_channel_types(self._registry)

    async def read(self) -> SequenceSettings:
        return await read_settings(self._session, self._tenant_id)

    async def update(
        self,
        *,
        actor: User,
        enabled: bool,
        channel_types: list[str],
        steps: list[Step],
        version: int,
    ) -> SequenceSettings:
        """Replace the settings, from the version the caller read; audited, old and new.

        Validated as the card needs it (FUS-01); an edit made from an older
        version is `409 follow_up_settings_changed`.
        """
        chosen = self._validated(enabled=enabled, channel_types=channel_types, steps=steps)
        row = await self._session.scalar(
            select(FollowUpSettings)
            .where(FollowUpSettings.tenant_id == self._tenant_id)
            .with_for_update()
        )
        current = row.version if row is not None else 0
        if version != current:
            raise ConflictError(
                "Somebody changed the follow-up settings since you opened them.",
                error_code=SETTINGS_CHANGED_CODE,
                details={"version": current},
            )
        before = _settings_of(row) if row is not None else UNCONFIGURED
        if row is None:
            row = FollowUpSettings(tenant_id=self._tenant_id, version=0)
            self._session.add(row)
        row.enabled = enabled
        row.channel_types = list(chosen)
        row.steps = [step.as_json() for step in steps]
        row.version = current + 1
        row.updated_by_id = actor.id
        await self._session.flush()
        await self._session.refresh(row)
        after = _settings_of(row)

        # What the change does to steps already waiting (FUS-06), in this
        # transaction. Imported here: the sequence service reads these settings.
        from app.services.follow_up_sequences import SequenceService

        await SequenceService(self._session, tenant_id=self._tenant_id).apply_settings(after)
        AuditTrail(self._session, tenant_id=self._tenant_id).record(
            AuditAction.FOLLOW_UP_SETTINGS_UPDATED,
            actor=actor,
            target_type="tenant",
            target_id=self._tenant_id,
            meta={"previous": before.as_json(), "current": after.as_json()},
        )
        return after

    def _validated(
        self, *, enabled: bool, channel_types: list[str], steps: list[Step]
    ) -> tuple[str, ...]:
        available = self.available_channel_types()
        chosen = tuple(dict.fromkeys(channel_types))
        unavailable = [name for name in chosen if name not in available]
        if unavailable:
            raise ValidationError(
                "Follow-ups cannot run on that platform in this deployment.",
                error_code=CHANNEL_UNAVAILABLE_CODE,
                details={"channel_types": unavailable, "available": list(available)},
            )
        most = min(MAX_STEPS, get_settings().follow_up_sequence_max_steps)
        if len(steps) > most:
            raise ValidationError(f"At most {most} reminders.", error_code=TOO_MANY_STEPS_CODE)
        for step in steps:
            if not MIN_STEP_DELAY_MINUTES <= step.delay_minutes <= MAX_STEP_DELAY_MINUTES:
                raise ValidationError(
                    "A reminder waits between 5 minutes and 23 hours.",
                    error_code=DELAY_OUT_OF_RANGE_CODE,
                    details={
                        "min_minutes": MIN_STEP_DELAY_MINUTES,
                        "max_minutes": MAX_STEP_DELAY_MINUTES,
                    },
                )
            if step.guidance is not None and len(step.guidance) > MAX_GUIDANCE_LENGTH:
                raise ValidationError(
                    f"A reminder's instruction is at most {MAX_GUIDANCE_LENGTH} characters.",
                    error_code=GUIDANCE_TOO_LONG_CODE,
                )
        if enabled and not steps:
            raise ValidationError(
                "Add at least one reminder, or turn follow-ups off.",
                error_code=STEPS_REQUIRED_CODE,
            )
        if enabled and not chosen:
            raise ValidationError(
                "Choose at least one platform, or turn follow-ups off.",
                error_code=CHANNELS_REQUIRED_CODE,
            )
        return chosen


def clean_guidance(value: str | None) -> str | None:
    """A reminder's instruction as stored: one line, no control characters (PER-05)."""
    if value is None:
        return None
    text = normalise(value)
    return text or None


__all__ = [
    "UNCONFIGURED",
    "FollowUpSettingsService",
    "SequenceSettings",
    "Step",
    "available_channel_types",
    "clean_guidance",
    "default_settings_row",
    "read_settings",
]
