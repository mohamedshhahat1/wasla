"""A workspace's campaign settings: its quiet hours (TZ-03, ADR-145).

On by default - 22:00 to 09:00 in the workspace's own zone - for every
workspace, new and existing. Any member reads them; an owner or an
administrator changes them, and the change is audited, old and new.

**A change takes effect at the next batch.** A campaign already waiting for
the old window's end is made due now (`release_quiet_waits`), so the next
sweep computes its wait again against the new window - or, with quiet hours
off, sends. Moving the workspace's zone does the same.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Final

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.campaign import Campaign, CampaignStatus, CampaignWaitReason
from app.db.models.enums import TenantRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.audit_service import AuditTrail
from app.services.follow_up_service import release_follow_up_quiet_waits

logger = get_logger(__name__)

#: The stable error code a window whose start equals its end is refused with.
EMPTY_WINDOW_CODE: Final = "quiet_hours_empty_window"


@dataclass(frozen=True, slots=True)
class CampaignSettings:
    quiet_hours_enabled: bool
    quiet_hours_start: time
    quiet_hours_end: time
    timezone: str


def settings_of(tenant: Tenant) -> CampaignSettings:
    return CampaignSettings(
        quiet_hours_enabled=tenant.campaign_quiet_hours_enabled,
        quiet_hours_start=tenant.campaign_quiet_hours_start,
        quiet_hours_end=tenant.campaign_quiet_hours_end,
        timezone=tenant.timezone,
    )


async def release_quiet_waits(
    session: AsyncSession, tenant_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """Make every campaign of this workspace waiting for quiet hours due now.

    The next sweep asks the window again: it computes a new end, or sends. The
    wait itself is left for that sweep to clear or renew, so nothing here
    decides whether it is night.
    """
    moment = now or datetime.now(UTC)
    result = await session.execute(
        update(Campaign)
        .where(
            Campaign.tenant_id == tenant_id,
            Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING]),
            Campaign.waiting_reason == CampaignWaitReason.QUIET_HOURS.value,
        )
        .values(next_send_at=moment)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


class CampaignSettingsService:
    """One workspace's campaign settings."""

    def __init__(self, session: AsyncSession, *, tenant: Tenant) -> None:
        self._session = session
        self._tenant = tenant

    def read(self) -> CampaignSettings:
        return settings_of(self._tenant)

    async def update(
        self,
        *,
        actor: User,
        actor_role: TenantRole,
        quiet_hours_enabled: bool,
        quiet_hours_start: time,
        quiet_hours_end: time,
    ) -> CampaignSettings:
        """Replace the quiet hours. Owner or administrator; audited, old and new."""
        if actor_role not in (TenantRole.TENANT_OWNER, TenantRole.TENANT_ADMIN):
            raise PermissionDeniedError("This action requires a different role in this workspace.")
        if quiet_hours_start == quiet_hours_end:
            raise ValidationError(
                "Quiet hours need a window: the start and the end cannot be the same time.",
                error_code=EMPTY_WINDOW_CODE,
            )
        if quiet_hours_start.second or quiet_hours_end.second:
            raise ValidationError("Quiet hours are set to the minute.")

        before = settings_of(self._tenant)
        tenant = self._tenant
        tenant.campaign_quiet_hours_enabled = quiet_hours_enabled
        tenant.campaign_quiet_hours_start = quiet_hours_start
        tenant.campaign_quiet_hours_end = quiet_hours_end
        after = settings_of(tenant)
        await self._session.flush()
        if after == before:
            return after

        AuditTrail(self._session, tenant_id=tenant.id).record(
            AuditAction.CAMPAIGN_SETTINGS_UPDATED,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="tenant",
            target_id=tenant.id,
            target_label=tenant.slug,
            meta={
                "previous": _summary(before),
                "current": _summary(after),
            },
        )
        released = await release_quiet_waits(self._session, tenant.id)
        # Template follow-ups waiting for the night ask the new window at the
        # next sweep too (FQH-03).
        released_follow_ups = await release_follow_up_quiet_waits(self._session, tenant.id)
        logger.info(
            "workspace.campaign_settings_updated",
            extra={
                "event": "workspace.campaign_settings_updated",
                "tenant_id": str(tenant.id),
                "released_waits": released,
                "released_follow_ups": released_follow_ups,
            },
        )
        return after


def _summary(settings: CampaignSettings) -> dict[str, object]:
    return {
        "quiet_hours_enabled": settings.quiet_hours_enabled,
        "quiet_hours_start": settings.quiet_hours_start.strftime("%H:%M"),
        "quiet_hours_end": settings.quiet_hours_end.strftime("%H:%M"),
    }


__all__ = [
    "EMPTY_WINDOW_CODE",
    "CampaignSettings",
    "CampaignSettingsService",
    "release_quiet_waits",
    "settings_of",
]
