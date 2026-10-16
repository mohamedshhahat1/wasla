"""When a campaign will reach its people: one function for the plan and the ETA (LIM-05, PG-13).

The worker decides each send; this predicts them, by replaying the same rules
over the campaign's pending recipients from a moment on:

- **its own rate** - a batch of at most 50, then `batch / messages_per_minute`
  minutes before the next (ADR-026);
- **quiet hours** in the workspace's own zone (TZ-03): nothing inside the window;
- **the portfolio's campaign share** of Meta's limit (LIM-04): a person outside
  their service window takes a place for 24 hours; when none is free the plan
  waits for the first place to be given back, plus the same margin the worker
  waits;
- **per-person deferrals** - Meta's pair limit and the per-contact marketing
  cap (FREQ-01) - and **the deadline** (`send_until`, LIM-06): whoever is still
  pending then is not sent.

Nothing here reads the database or the clock. The caller gathers the facts -
who is pending, whose window is open, who the ledger already counts - and the
same function answers the schedule response, `GET /delivery-plan` and the ETA,
so the three can never disagree about the same campaign at the same moment.

It is a prediction and says so: other senders on the portfolio, a limit Meta
changes and the send rate the worker actually achieves all move it. The page
must never present it as a guaranteed time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final

from app.core.timezones import zone
from app.services.quiet_hours import QuietHours

#: Meta's moving window, and the margin a waiting campaign adds before asking again.
WINDOW: Final = timedelta(hours=24)
RESUME_MARGIN: Final = timedelta(minutes=5)
#: The worker's batch: at most this many recipients per sweep.
BATCH: Final = 50
#: Meta's default time-to-live for a marketing template: 30 days ("Time-to-live",
#: developers.facebook.com, read 2026-10-16). A plan running past it is accepted
#: and says so (`plan_exceeds_ttl`).
MARKETING_TTL: Final = timedelta(days=30)
#: How far a plan is simulated before it gives up: a year of steps is a
#: campaign nobody can usefully plan.
MAX_HORIZON: Final = timedelta(days=366)
MAX_STEPS: Final = 200_000


class PlanNotice(StrEnum):
    """What the page should tell a person about a plan. Closed (LIM-05)."""

    STARTS_AFTER_QUIET_HOURS = "starts_after_quiet_hours"
    SPREADS_OVER_DAYS = "spreads_over_days"
    LIMIT_SHARED_WITH_OTHER_NUMBERS = "limit_shared_with_other_numbers"
    LIMIT_UNKNOWN = "limit_unknown"
    CONTACT_CAP_DEFERS = "contact_cap_defers"
    PLAN_EXCEEDS_TTL = "plan_exceeds_ttl"
    DEADLINE_LEAVES_RECIPIENTS = "deadline_leaves_recipients"


class EtaBasis(StrEnum):
    """What an ETA was computed from (PG-13). Closed."""

    RATE = "rate"
    QUIET_HOURS = "quiet_hours"
    DAILY_LIMIT = "daily_limit"
    CONTACT_CAP = "contact_cap"
    DEADLINE = "deadline"


@dataclass(frozen=True, slots=True)
class PlannedRecipient:
    """One pending person, as the plan needs them."""

    #: Their service window is open: they take no place in the portfolio's 24 hours.
    window_open: bool = False
    #: The ledger already counts them in the window: they take no new place.
    counted: bool = False
    #: Not to be sent before this (Meta's pair limit, CMP-18).
    not_before: datetime | None = None
    #: Not to be sent a marketing message before this (FREQ-01).
    cap_until: datetime | None = None

    def ready_at(self) -> datetime | None:
        moments = [moment for moment in (self.not_before, self.cap_until) if moment is not None]
        return max(moments) if moments else None


@dataclass(frozen=True, slots=True)
class PlanInputs:
    recipients: tuple[PlannedRecipient, ...]
    #: Messages per minute: the campaign's, or what it was observed to achieve.
    rate: float
    quiet: QuietHours
    #: The campaign share of the portfolio's limit; None for unlimited.
    budget: int | None
    #: When each person the ledger counts in the window was last sent - one
    #: moment per distinct person. Their places come back 24 hours later.
    counted_at: tuple[datetime, ...] = ()
    #: The limit is shared with other numbers or workspaces.
    shared: bool = False
    #: The limit was never read: there is no plan.
    limit_known: bool = True
    send_until: datetime | None = None
    #: When the campaign may start: its schedule, or now.
    start: datetime | None = None


@dataclass(frozen=True, slots=True)
class PlanDay:
    local_date: date
    first_send_at: datetime
    last_send_at: datetime
    recipients: int


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    days: tuple[PlanDay, ...]
    estimated_completion_at: datetime | None
    notices: tuple[PlanNotice, ...]
    basis: tuple[EtaBasis, ...]
    planned: int
    left_by_deadline: int
    computed_at: datetime
    timezone: str
    first_send_at: datetime | None = None


@dataclass
class _Ledger:
    """The portfolio's counted people, by when each place is given back."""

    returns: deque[datetime] = field(default_factory=deque)

    def free(self, *, budget: int, at: datetime) -> int:
        while self.returns and self.returns[0] + WINDOW <= at:
            self.returns.popleft()
        return budget - len(self.returns)

    def next_return(self) -> datetime | None:
        return self.returns[0] + WINDOW + RESUME_MARGIN if self.returns else None

    def take(self, at: datetime, count: int) -> None:
        self.returns.extend([at] * count)


def plan(inputs: PlanInputs, *, now: datetime) -> DeliveryPlan:
    """Replay the worker's rules over the pending recipients from `start` (or now)."""
    tz = inputs.quiet.timezone
    if not inputs.limit_known:
        return DeliveryPlan(
            days=(),
            estimated_completion_at=None,
            notices=(PlanNotice.LIMIT_UNKNOWN,),
            basis=(),
            planned=0,
            left_by_deadline=0,
            computed_at=now,
            timezone=tz,
        )
    start = max(inputs.start or now, now)
    rate = max(inputs.rate, 0.001)
    ledger = _Ledger(returns=deque(sorted(inputs.counted_at)))
    waiting = sorted(
        inputs.recipients,
        key=lambda person: (person.ready_at() or start, person.window_open),
    )
    pending = deque(waiting)
    sent_at: list[datetime] = []
    notices: set[PlanNotice] = set()
    basis: set[EtaBasis] = {EtaBasis.RATE}
    at = start
    steps = 0
    deferred: list[PlannedRecipient] = []
    while (pending or deferred) and steps < MAX_STEPS and at - start < MAX_HORIZON:
        steps += 1
        if inputs.send_until is not None and at >= inputs.send_until:
            basis.add(EtaBasis.DEADLINE)
            break
        if inputs.quiet.contains(at):
            if not sent_at:
                notices.add(PlanNotice.STARTS_AFTER_QUIET_HOURS)
            basis.add(EtaBasis.QUIET_HOURS)
            at = inputs.quiet.ends_after(at)
            continue
        # Everybody whose deferral has passed is ready, in the order they were.
        if deferred:
            ready = [person for person in deferred if (person.ready_at() or at) <= at]
            deferred = [person for person in deferred if (person.ready_at() or at) > at]
            pending.extendleft(reversed(ready))
        batch: list[PlannedRecipient] = []
        free = ledger.free(budget=inputs.budget, at=at) if inputs.budget is not None else None
        blocked_by_limit = False
        while pending and len(batch) < BATCH:
            person = pending[0]
            ready_at = person.ready_at()
            if ready_at is not None and ready_at > at:
                if person.cap_until is not None and person.cap_until > at:
                    notices.add(PlanNotice.CONTACT_CAP_DEFERS)
                    basis.add(EtaBasis.CONTACT_CAP)
                deferred.append(pending.popleft())
                continue
            takes_place = not person.window_open and not person.counted
            if takes_place and free is not None:
                if free <= 0:
                    blocked_by_limit = True
                    break
                free -= 1
            batch.append(pending.popleft())
        if batch:
            sent_at.extend([at] * len(batch))
            places = sum(1 for person in batch if not person.window_open and not person.counted)
            if inputs.budget is not None and places:
                ledger.take(at, places)
            at = at + timedelta(minutes=len(batch) / rate)
            continue
        if blocked_by_limit:
            notices.add(PlanNotice.SPREADS_OVER_DAYS)
            basis.add(EtaBasis.DAILY_LIMIT)
            if inputs.shared:
                notices.add(PlanNotice.LIMIT_SHARED_WITH_OTHER_NUMBERS)
            following = ledger.next_return() or at + WINDOW
            at = max(following, at + timedelta(seconds=1))
            continue
        if deferred:
            following = min((person.ready_at() or at) for person in deferred)
            at = max(following, at + timedelta(seconds=1))
            continue
        break

    left = len(pending) + len(deferred)
    if left and inputs.send_until is not None:
        notices.add(PlanNotice.DEADLINE_LEAVES_RECIPIENTS)
    days = _days(sent_at, tz)
    completion = sent_at[-1] if sent_at and not left else None
    if completion is not None and completion - start > MARKETING_TTL:
        notices.add(PlanNotice.PLAN_EXCEEDS_TTL)
    if len(days) > 1 and EtaBasis.DAILY_LIMIT in basis:
        notices.add(PlanNotice.SPREADS_OVER_DAYS)
    return DeliveryPlan(
        days=days,
        estimated_completion_at=completion,
        notices=tuple(sorted(notices, key=lambda notice: notice.value)),
        basis=tuple(sorted(basis, key=lambda item: item.value)),
        planned=len(sent_at),
        left_by_deadline=left if inputs.send_until is not None else 0,
        computed_at=now,
        timezone=tz,
        first_send_at=sent_at[0] if sent_at else None,
    )


def _days(sent_at: list[datetime], tz: str) -> tuple[PlanDay, ...]:
    by_day: dict[date, list[datetime]] = {}
    zone_info = zone(tz)
    for moment in sent_at:
        by_day.setdefault(moment.astimezone(zone_info).date(), []).append(moment)
    return tuple(
        PlanDay(
            local_date=day,
            first_send_at=moments[0],
            last_send_at=moments[-1],
            recipients=len(moments),
        )
        for day, moments in sorted(by_day.items())
    )


def confidence(eta_plan: DeliveryPlan) -> str:
    """How much an ETA should be trusted: high on the rate alone, lower with every wait."""
    basis = set(eta_plan.basis)
    if EtaBasis.DAILY_LIMIT in basis or PlanNotice.LIMIT_SHARED_WITH_OTHER_NUMBERS in set(
        eta_plan.notices
    ):
        return "low"
    if basis - {EtaBasis.RATE}:
        return "medium"
    return "high"


__all__ = [
    "BATCH",
    "MARKETING_TTL",
    "DeliveryPlan",
    "EtaBasis",
    "PlanDay",
    "PlanInputs",
    "PlanNotice",
    "PlannedRecipient",
    "confidence",
    "plan",
]
