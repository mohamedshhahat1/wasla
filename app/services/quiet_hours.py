"""When a campaign may not write, in the workspace's own time (TZ-03, ADR-145).

Pure functions over one workspace's settings. Nothing here reads the database
or the clock: the worker and the delivery plan both pass the moment they are
asking about, so a test can stand anywhere in the year - on either side of a
daylight-saving transition - and the plan and the send agree.

**Local, never a fixed offset.** A window is a pair of wall-clock times in the
workspace's IANA zone. Its end is found by building the local date-time it
ends at and converting that to UTC with the zone's rules for that date, so a
window ending at 09:00 in Cairo ends at 07:00 UTC in winter and 06:00 UTC in
summer, and a window that spans a transition is as long as it really is.

A window whose start is later than its end crosses midnight - 22:00 to 09:00,
the default, is the normal case. A boundary that falls in a daylight-saving
gap (a local time that never happens) resolves to an instant after the gap
(PEP 495's `fold=0`); Cairo's transitions are at midnight, away from the
default window's ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from app.core.timezones import zone

if TYPE_CHECKING:
    from app.db.models.tenant import Tenant


@dataclass(frozen=True, slots=True)
class QuietHours:
    """One workspace's quiet window: on or off, its local start and end, its zone."""

    enabled: bool
    start: time
    end: time
    timezone: str

    @property
    def crosses_midnight(self) -> bool:
        return self.start > self.end

    def _local_time(self, at: datetime) -> time:
        return at.astimezone(zone(self.timezone)).time().replace(tzinfo=None)

    def contains(self, at: datetime) -> bool:
        """Whether `at` falls inside the window. Never, when quiet hours are off."""
        if not self.enabled:
            return False
        now = self._local_time(at)
        if self.crosses_midnight:
            return now >= self.start or now < self.end
        return self.start <= now < self.end

    def _instant(self, day: date, wall: time) -> datetime:
        """The UTC instant of local `wall` on `day`, by the zone's rules for that date.

        `fold=0` (PEP 495): a wall time repeated by a fall-back transition is
        its first occurrence, and one skipped by a spring-forward gap is read
        with the offset before the gap - an instant after the gap.
        """
        return datetime.combine(day, wall, tzinfo=zone(self.timezone)).astimezone(UTC)

    def ends_after(self, at: datetime) -> datetime:
        """When the window `at` is in ends; `at` itself when it is not in one."""
        if not self.contains(at):
            return at
        local = at.astimezone(zone(self.timezone))
        day = local.date()
        if self.crosses_midnight and local.time().replace(tzinfo=None) >= self.start:
            day = day + timedelta(days=1)
        return self._instant(day, self.end)

    def next_start(self, at: datetime) -> datetime | None:
        """The next moment at or after `at` the window opens; None when quiet hours are off."""
        if not self.enabled:
            return None
        if self.contains(at):
            return at
        local = at.astimezone(zone(self.timezone))
        day = local.date()
        candidate = self._instant(day, self.start)
        if candidate <= at:
            candidate = self._instant(day + timedelta(days=1), self.start)
        return candidate


def from_tenant(tenant: Tenant) -> QuietHours:
    """A workspace's quiet hours as its row records them."""
    return QuietHours(
        enabled=tenant.campaign_quiet_hours_enabled,
        start=tenant.campaign_quiet_hours_start,
        end=tenant.campaign_quiet_hours_end,
        timezone=tenant.timezone,
    )


__all__ = ["QuietHours", "from_tenant"]
