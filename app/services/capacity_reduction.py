"""Bringing a workspace down to a smaller channel capacity (ENT-14, ENT-15).

One flow for every cause - a downgrade taking effect, a channel top-up or grant
ending, a refund withdrawn, a migration adopted:

1. **Boundary** (`boundary`): the capacity in force no longer holds the active
   connections, or allows fewer channel types than they use. An owner's
   pre-selection that still fits is applied at once; otherwise a reduction is
   opened with a grace of ``CHANNEL_CAPACITY_GRACE_DAYS``. Nothing else is
   disabled, and every connection keeps working through the grace.
2. **Owner selection** (`select`): during the grace an owner names the
   connections to keep - they must fit the capacity in force and its channel
   types - and the others are disabled, in one transaction under the
   workspace's capacity lock. Before a boundary the same request stores a
   pre-selection instead.
3. **Automatic fallback** (`resolve_automatically`, the billing worker at the
   grace end): connections of a type the plan no longer allows are disabled
   first, then the newest, keeping the oldest by ``ownership_started_at``.
4. **Capacity back** (`capacity_returned`): a top-up, a grant or an upgrade
   that makes everything fit again closes the reduction; nothing is disabled.

Disabling goes through `ChannelConnectionService.disable`, the one disable path
a person's own disable takes: claim, credential and history kept, pending
follow-ups cancelled, the reduction named on the audit entry. Never a release,
never a delete. Suspension, cancellation and expiry open nothing (ENT-16): the
flow runs only for a serving subscription.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import LimitKey, Subscription
from app.db.models.channel import Channel, ChannelConnection, ConnectionDisabledReason
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.db.models.user import User
from app.repositories.billing_repository import SubscriptionRepository
from app.repositories.channel_capacity_repository import ChannelCapacityReductionRepository
from app.services.audit_service import AuditTrail
from app.services.channel_connection_service import ChannelConnectionService
from app.services.channel_fit import ChannelCapacity
from app.services.entitlement_service import EntitlementService, hold_limit_lock
from app.services.entitlement_terms import ordered

logger = get_logger(__name__)


class CapacitySelectionError(ValidationError):
    """A selection that the capacity in force could not hold (ENT-14).

    422: the request is well formed and names this workspace's connections,
    but keeping exactly those would not fit - too many, or of a channel type
    the plan does not include.
    """

    error_code = "channel_capacity_selection_invalid"
    message = "Those connections do not fit the channel capacity in force."


@dataclass(frozen=True, slots=True)
class FallbackChoice:
    """What the automatic fallback would keep and disable, oldest first in each."""

    keep: tuple[ChannelConnection, ...]
    disable: tuple[ChannelConnection, ...]


def needs_reduction(capacity: ChannelCapacity, active: Mapping[Channel, int]) -> bool:
    """Whether these active connections exceed the slots or use a type not allowed."""
    if any(count and channel not in capacity.allowed for channel, count in active.items()):
        return True
    return not capacity.fits(active)


def automatic_choice(
    connections: Sequence[ChannelConnection], capacity: ChannelCapacity
) -> FallbackChoice:
    """Keep the oldest connections that fit; disable disallowed types and the newest (ENT-14).

    Oldest by ``ownership_started_at``, the moment the workspace's claim began,
    then by id so the choice is the same on every run. Each connection is kept
    if the kept set still fits with it - so a typed slot is filled by its own
    channel whatever that channel's place in the order - and disabled
    otherwise. A channel type the plan does not allow is never kept.
    """
    kept: list[ChannelConnection] = []
    disabled: list[ChannelConnection] = []
    counts: Counter[Channel] = Counter()
    for connection in sorted(connections, key=lambda row: (row.ownership_started_at, row.id)):
        if connection.channel not in capacity.allowed:
            disabled.append(connection)
            continue
        trial = counts.copy()
        trial[connection.channel] += 1
        if capacity.fits(trial):
            counts = trial
            kept.append(connection)
        else:
            disabled.append(connection)
    return FallbackChoice(keep=tuple(kept), disable=tuple(disabled))


def selection_problem(
    connections: Sequence[ChannelConnection], capacity: ChannelCapacity
) -> str | None:
    """Why keeping exactly `connections` would not fit `capacity`, or None if it would."""
    counts = Counter(connection.channel for connection in connections)
    outside = [channel.value for channel in ordered(counts) if channel not in capacity.allowed]
    if outside:
        return (
            "The plan does not include "
            + ", ".join(outside)
            + "; a selection keeps only channels the plan allows."
        )
    if not capacity.fits(counts):
        return (
            f"Keeping {len(connections)} connections does not fit the capacity in force "
            f"({capacity.total} slots)."
        )
    return None


@dataclass(frozen=True, slots=True)
class SelectionOutcome:
    """What an owner's selection did: resolved the open reduction, or saved for the boundary."""

    applied: bool
    kept: tuple[uuid.UUID, ...]
    disabled: tuple[uuid.UUID, ...]
    reduction: ChannelCapacityReduction | None


class ChannelCapacityReductions:
    """The capacity-reduction lifecycle of one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        default_plan_code: str | None = None,
        grace: timedelta | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._default_plan_code = default_plan_code
        self._grace = (
            grace
            if grace is not None
            else timedelta(days=get_settings().channel_capacity_grace_days)
        )
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._reductions = ChannelCapacityReductionRepository(session, tenant_id=tenant_id)
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._audit = AuditTrail(session, tenant_id=tenant_id)

    # ----------------------------------------------------------------- reads

    async def open_reduction(self) -> ChannelCapacityReduction | None:
        return await self._reductions.open()

    async def latest(self) -> ChannelCapacityReduction | None:
        return await self._reductions.latest()

    async def preselected(self) -> list[uuid.UUID]:
        return [row.connection_id for row in await self._reductions.preselection()]

    async def fallback_preview(self, *, now: datetime | None = None) -> FallbackChoice | None:
        """What the automatic fallback would do now, if the workspace has anything to resolve.

        Against the capacity in force while a reduction is open, and against
        the capacity at the end of the term while one is ahead; None when the
        connections fit either way.
        """
        moment = now if now is not None else self._clock()
        active = await self._reductions.active_connections()
        counts = Counter(connection.channel for connection in active)
        entitlements = self._entitlements(moment)
        if await self._reductions.open() is not None:
            capacity = await entitlements.channel_capacity(at=moment)
        else:
            ahead = await entitlements.next_term_channel_capacity()
            if ahead is None:
                return None
            capacity = ahead
        if not needs_reduction(capacity, counts):
            return None
        return automatic_choice(active, capacity)

    # --------------------------------------------------------------- boundary

    async def boundary(
        self, *, cause: CapacityReductionCause, now: datetime | None = None
    ) -> ChannelCapacityReduction | None:
        """A capacity boundary passed: fit, apply the pre-selection, or open the flow.

        Called in the transaction that moved the capacity: the plan change
        applied, the top-up or grant expired, the refund withdrawn, the
        migration adopted. Every boundary consumes the pre-selection - applied
        if it still fits, discarded otherwise - so one made for a change that
        never happened cannot surprise anybody later.
        """
        moment = now if now is not None else self._clock()
        await hold_limit_lock(
            self._session, tenant_id=self._tenant_id, key=LimitKey.CHANNEL_CONNECTIONS
        )
        if not await self._serving():
            # ENT-16: a workspace not served reads over its limit and keeps
            # every connection; nothing is disabled for that.
            await self._reductions.clear_preselection()
            return None
        capacity = await self._entitlements(moment).channel_capacity(at=moment)
        active = await self._reductions.active_connections()
        counts = Counter(connection.channel for connection in active)
        open_ = await self._reductions.open(lock=True)

        if not needs_reduction(capacity, counts):
            await self._reductions.clear_preselection()
            if open_ is not None:
                await self._close(open_, CapacityReductionStatus.NO_LONGER_NEEDED, moment=moment)
            return open_

        if open_ is not None:
            # A newer cause adjusts the target of the reduction already open;
            # the grace already running stands.
            self._snapshot(open_, capacity)
            await self._session.flush()
            logger.info(
                "billing.channel_capacity_reduction_adjusted",
                extra={
                    "event": "billing.channel_capacity_reduction_adjusted",
                    "tenant_id": str(self._tenant_id),
                    "cause": cause.value,
                },
            )
            await self._reductions.clear_preselection()
            return open_

        applied = await self._apply_preselection(active, capacity, cause=cause, moment=moment)
        await self._reductions.clear_preselection()
        if applied is not None:
            return applied

        reduction = ChannelCapacityReduction(
            tenant_id=self._tenant_id,
            cause=cause,
            status=CapacityReductionStatus.PENDING_SELECTION,
            effective_at=moment,
            grace_ends_at=moment + self._grace,
            target_general=0,
            target_typed={},
            target_allowed_types=[],
        )
        self._snapshot(reduction, capacity)
        self._reductions.add(reduction)
        await self._session.flush()
        self._audit.record(
            AuditAction.CHANNEL_CAPACITY_REDUCTION_OPENED,
            actor=None,
            actor_kind=AuditActorKind.SYSTEM,
            target_type="channel_capacity_reduction",
            target_id=reduction.id,
            target_label=cause.value,
            meta={
                "cause": cause.value,
                "active": len(active),
                "target_general": reduction.target_general,
                "target_typed": dict(reduction.target_typed),
                "target_allowed_types": list(reduction.target_allowed_types),
                "grace_ends_at": reduction.grace_ends_at.isoformat(),
            },
        )
        logger.warning(
            "billing.channel_capacity_reduction_opened",
            extra={
                "event": "billing.channel_capacity_reduction_opened",
                "tenant_id": str(self._tenant_id),
                "cause": cause.value,
                "active": len(active),
            },
        )
        return reduction

    async def capacity_returned(
        self, *, now: datetime | None = None
    ) -> ChannelCapacityReduction | None:
        """Close the open reduction if the connections fit again; returns it if closed."""
        moment = now if now is not None else self._clock()
        if await self._reductions.open() is None:
            return None
        await hold_limit_lock(
            self._session, tenant_id=self._tenant_id, key=LimitKey.CHANNEL_CONNECTIONS
        )
        open_ = await self._reductions.open(lock=True)
        if open_ is None or not await self._serving():
            return None
        capacity = await self._entitlements(moment).channel_capacity(at=moment)
        active = await self._reductions.active_connections()
        if needs_reduction(capacity, Counter(row.channel for row in active)):
            return None
        await self._close(open_, CapacityReductionStatus.NO_LONGER_NEEDED, moment=moment)
        return open_

    # -------------------------------------------------------------- selection

    async def select(
        self,
        keep: Sequence[uuid.UUID],
        *,
        expected_revision: int,
        actor: User,
        now: datetime | None = None,
    ) -> SelectionOutcome:
        """An owner names the connections to keep (ENT-14).

        With a reduction open: re-checked under the workspace's capacity lock
        against the capacity in force, and the other active connections are
        disabled now. With none open but the end of the term unable to hold
        every connection: saved as the pre-selection the boundary applies.

        Refused: another workspace's connection, or one released (404, like
        any other resource); a connection not active, or the same one twice
        (422); a selection that does not fit or keeps a channel the plan does
        not allow (422); a stale `expected_revision` (409); nothing to choose
        (409); a workspace not served (409 - ENT-16 disables nothing).
        """
        moment = now if now is not None else self._clock()
        if len(set(keep)) != len(keep):
            raise ValidationError("Each connection is named once.")
        await hold_limit_lock(
            self._session, tenant_id=self._tenant_id, key=LimitKey.CHANNEL_CONNECTIONS
        )
        subscription = await self._subscriptions.get()
        if subscription is None or not subscription.is_serving:
            raise ConflictError(
                "This workspace's subscription is not active. Settle it first; nothing is "
                "disabled meanwhile."
            )
        found = await self._reductions.live_connections(keep)
        if len(found) != len(set(keep)):
            raise NotFoundError("No such connection.")
        chosen = [found[connection_id] for connection_id in keep]
        if any(connection.counts_toward_capacity is False for connection in chosen):
            raise ValidationError("Only active connections can be kept.")

        open_ = await self._reductions.open(lock=True)
        entitlements = self._entitlements(moment)
        if open_ is not None:
            if open_.revision != expected_revision:
                raise ConflictError(
                    f"The reduction has changed (revision {open_.revision}); reload it and retry."
                )
            capacity = await entitlements.channel_capacity(at=moment)
            problem = selection_problem(chosen, capacity)
            if problem is not None:
                raise CapacitySelectionError(problem)
            active = await self._reductions.active_connections()
            kept_ids = {connection.id for connection in chosen}
            others = [connection for connection in active if connection.id not in kept_ids]
            disabled = await self._disable(
                others,
                reason=ConnectionDisabledReason.CAPACITY_REDUCTION,
                actor=actor,
                reduction=open_,
            )
            self._snapshot(open_, capacity)
            await self._resolve(
                open_,
                CapacityReductionStatus.RESOLVED_BY_OWNER,
                moment=moment,
                actor=actor,
                kept=[connection.id for connection in chosen],
                disabled=disabled,
            )
            return SelectionOutcome(
                applied=True,
                kept=tuple(connection.id for connection in chosen),
                disabled=tuple(disabled),
                reduction=open_,
            )

        if subscription.revision != expected_revision:
            raise ConflictError(
                f"The subscription has changed (revision {subscription.revision}); "
                "reload it and retry."
            )
        ahead = await entitlements.next_term_channel_capacity()
        active = await self._reductions.active_connections()
        if ahead is None or not needs_reduction(ahead, Counter(row.channel for row in active)):
            raise ConflictError("Every connection fits; there is nothing to choose.")
        problem = selection_problem(chosen, ahead)
        if problem is not None:
            raise CapacitySelectionError(problem)
        await self._reductions.replace_preselection(
            [connection.id for connection in chosen], by=actor.id, at=moment
        )
        self._audit.record(
            AuditAction.CHANNEL_CAPACITY_SELECTION_SAVED,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="subscription",
            target_id=subscription.id,
            meta={
                "keep": [str(connection.id) for connection in chosen],
                "effective_at": subscription.current_period_end.isoformat(),
            },
        )
        return SelectionOutcome(
            applied=False,
            kept=tuple(connection.id for connection in chosen),
            disabled=(),
            reduction=None,
        )

    # --------------------------------------------------------- automatic fallback

    async def resolve_automatically(
        self, reduction_id: uuid.UUID, *, now: datetime | None = None
    ) -> ChannelCapacityReduction | None:
        """The grace ended with no choice: disable by the fallback rule, once.

        The workspace's capacity lock first, then the reduction's row, claimed
        `SKIP LOCKED` and only while still open and due - so two workers at the
        grace end produce one set of disables. Re-judged against the capacity
        in force: if it fits again the reduction closes with nothing disabled.
        A workspace not served is left until it is (ENT-16).
        """
        moment = now if now is not None else self._clock()
        await hold_limit_lock(
            self._session, tenant_id=self._tenant_id, key=LimitKey.CHANNEL_CONNECTIONS
        )
        reduction = await self._reductions.claim_due(reduction_id, now=moment)
        if reduction is None or not await self._serving():
            return None
        capacity = await self._entitlements(moment).channel_capacity(at=moment)
        active = await self._reductions.active_connections()
        if not needs_reduction(capacity, Counter(row.channel for row in active)):
            await self._close(reduction, CapacityReductionStatus.NO_LONGER_NEEDED, moment=moment)
            return reduction
        choice = automatic_choice(active, capacity)
        disabled = await self._disable(
            choice.disable,
            reason=ConnectionDisabledReason.CAPACITY_REDUCTION_AUTOMATIC,
            actor=None,
            reduction=reduction,
        )
        self._snapshot(reduction, capacity)
        await self._resolve(
            reduction,
            CapacityReductionStatus.RESOLVED_AUTOMATICALLY,
            moment=moment,
            actor=None,
            kept=[connection.id for connection in choice.keep],
            disabled=disabled,
        )
        return reduction

    # ----------------------------------------------------------------- helpers

    def _entitlements(self, moment: datetime) -> EntitlementService:
        return EntitlementService(
            self._session,
            tenant_id=self._tenant_id,
            default_plan_code=self._default_plan_code,
            clock=lambda: moment,
        )

    async def _serving(self) -> bool:
        subscription: Subscription | None = await self._subscriptions.get()
        return subscription is not None and subscription.is_serving

    async def _apply_preselection(
        self,
        active: Sequence[ChannelConnection],
        capacity: ChannelCapacity,
        *,
        cause: CapacityReductionCause,
        moment: datetime,
    ) -> ChannelCapacityReduction | None:
        """Apply an owner's pre-selection at the boundary, if it still fits."""
        rows = await self._reductions.preselection()
        if not rows:
            return None
        by_id = {connection.id: connection for connection in active}
        chosen = [by_id[row.connection_id] for row in rows if row.connection_id in by_id]
        if len(chosen) != len(rows) or selection_problem(chosen, capacity) is not None:
            logger.info(
                "billing.channel_capacity_preselection_discarded",
                extra={
                    "event": "billing.channel_capacity_preselection_discarded",
                    "tenant_id": str(self._tenant_id),
                },
            )
            return None
        selector_id = rows[0].selected_by
        actor = await self._session.get(User, selector_id) if selector_id is not None else None
        reduction = ChannelCapacityReduction(
            tenant_id=self._tenant_id,
            cause=cause,
            status=CapacityReductionStatus.PENDING_SELECTION,
            effective_at=moment,
            grace_ends_at=moment,
            target_general=0,
            target_typed={},
            target_allowed_types=[],
        )
        self._snapshot(reduction, capacity)
        self._reductions.add(reduction)
        await self._session.flush()
        kept_ids = {connection.id for connection in chosen}
        disabled = await self._disable(
            [connection for connection in active if connection.id not in kept_ids],
            reason=ConnectionDisabledReason.CAPACITY_REDUCTION,
            actor=actor,
            reduction=reduction,
        )
        await self._resolve(
            reduction,
            CapacityReductionStatus.RESOLVED_BY_OWNER,
            moment=moment,
            actor=actor,
            kept=sorted(kept_ids),
            disabled=disabled,
            preselected=True,
        )
        return reduction

    async def _disable(
        self,
        connections: Sequence[ChannelConnection],
        *,
        reason: ConnectionDisabledReason,
        actor: User | None,
        reduction: ChannelCapacityReduction,
    ) -> list[uuid.UUID]:
        """Disable through the one disable path, naming the reduction on each audit entry."""
        service = ChannelConnectionService(
            self._session,
            tenant_id=self._tenant_id,
            default_plan_code=self._default_plan_code,
        )
        disabled: list[uuid.UUID] = []
        for connection in connections:
            await service.disable(
                connection.id,
                reason=reason,
                actor=actor,
                actor_kind=AuditActorKind.USER if actor is not None else AuditActorKind.SYSTEM,
                meta={"reduction_id": str(reduction.id), "cause": reduction.cause.value},
            )
            disabled.append(connection.id)
        return disabled

    @staticmethod
    def _snapshot(reduction: ChannelCapacityReduction, capacity: ChannelCapacity) -> None:
        """Record the capacity the workspace must fit, as last judged."""
        reduction.target_general = capacity.general if capacity.general is not None else 0
        reduction.target_typed = {
            channel.value: count for channel, count in sorted(capacity.typed.items()) if count
        }
        reduction.target_allowed_types = [channel.value for channel in ordered(capacity.allowed)]

    async def _close(
        self,
        reduction: ChannelCapacityReduction,
        status: CapacityReductionStatus,
        *,
        moment: datetime,
    ) -> None:
        await self._resolve(reduction, status, moment=moment, actor=None, kept=None, disabled=[])

    async def _resolve(
        self,
        reduction: ChannelCapacityReduction,
        status: CapacityReductionStatus,
        *,
        moment: datetime,
        actor: User | None,
        kept: Sequence[uuid.UUID] | None,
        disabled: Sequence[uuid.UUID],
        preselected: bool = False,
    ) -> None:
        reduction.status = status
        reduction.resolved_at = moment
        reduction.resolved_by = actor.id if actor is not None else None
        reduction.kept_connection_ids = list(kept) if kept is not None else None
        reduction.disabled_connection_ids = list(disabled)
        await self._session.flush()
        self._audit.record(
            AuditAction.CHANNEL_CAPACITY_REDUCTION_RESOLVED,
            actor=actor,
            actor_kind=AuditActorKind.USER if actor is not None else AuditActorKind.SYSTEM,
            target_type="channel_capacity_reduction",
            target_id=reduction.id,
            target_label=status.value,
            meta={
                "cause": reduction.cause.value,
                "resolution": status.value,
                "kept": [str(item) for item in kept] if kept is not None else None,
                "disabled": [str(item) for item in disabled],
                "preselected": preselected,
            },
        )
        logger.info(
            "billing.channel_capacity_reduction_resolved",
            extra={
                "event": "billing.channel_capacity_reduction_resolved",
                "tenant_id": str(self._tenant_id),
                "cause": reduction.cause.value,
                "resolution": status.value,
                "disabled": len(disabled),
            },
        )


__all__ = [
    "CapacitySelectionError",
    "ChannelCapacityReductions",
    "FallbackChoice",
    "SelectionOutcome",
    "automatic_choice",
    "needs_reduction",
    "selection_problem",
]
