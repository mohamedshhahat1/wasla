"""Data access for scheduled follow-ups.

Two classes, and the split is the point. `FollowUpRepository` is tenant-scoped
like everything else a request touches. `DueFollowUpClaim` is not, because the
worker sweeps every workspace at once and has no tenant to be scoped to — it is
kept in a separate class with a name that says so, rather than as a method on
the scoped repository that quietly ignores the scope.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import Cursor
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind
from app.repositories.base import BaseRepository, TenantScopedRepository

# How many due follow-ups one sweep claims. Bounded so a backlog is worked
# through in batches rather than loaded into memory whole, and so a worker that
# dies mid-sweep has locked only a handful of rows.
DEFAULT_CLAIM_LIMIT = 20


def _after_scheduled(after: Cursor) -> ColumnElement[bool]:
    """Rows following `after` under ``ORDER BY scheduled_at DESC, id DESC``."""
    if after.sort_value is None:
        return FollowUp.id < after.id
    return or_(
        FollowUp.scheduled_at < after.sort_value,
        and_(FollowUp.scheduled_at == after.sort_value, FollowUp.id < after.id),
    )


class FollowUpRepository(TenantScopedRepository[FollowUp]):
    """Follow-ups of one workspace."""

    model = FollowUp

    def _tenant_filter(self) -> ColumnElement[bool]:
        return FollowUp.tenant_id == self.tenant_id

    async def get_by_id(self, follow_up_id: uuid.UUID) -> FollowUp | None:
        return await self._first(self._select().where(FollowUp.id == follow_up_id))

    async def require_by_id(self, follow_up_id: uuid.UUID) -> FollowUp:
        return await self._require(self._select().where(FollowUp.id == follow_up_id))

    async def lock_by_id(self, follow_up_id: uuid.UUID) -> FollowUp:
        """This workspace's follow-up as committed now, locked until the transaction ends.

        Waits rather than skipping. The holder is either another person's
        request or the sweep's per-row re-take, and both finish in database
        time: the re-take releases the row when the send intent commits, which
        is before Meta is asked (ADR-093). Waiting is what lets a cancel see the
        truth - "a send is now committed" - instead of guessing (CRM-10).
        """
        # Staged changes first: sessions here do not autoflush, and the
        # re-read below would otherwise overwrite them with the database's copy.
        await self.session.flush()
        return await self._require(
            self._select()
            .where(FollowUp.id == follow_up_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )

    async def get_pending_for_conversation(
        self,
        conversation_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> FollowUp | None:
        """The conversation's waiting nudge, if it has one.

        The partial unique index makes at most one row possible, so this cannot
        quietly pick between several. `for_update` locks and re-reads it, for a
        reschedule that must not land on a row the sweep is sending (CRM-09).
        """
        # Staged changes first: sessions here do not autoflush, and the
        # re-read below would otherwise overwrite them with the database's copy.
        await self.session.flush()
        statement = self._select().where(
            FollowUp.conversation_id == conversation_id,
            FollowUp.status == FollowUpStatus.PENDING,
        )
        if for_update:
            statement = statement.with_for_update(key_share=True).execution_options(
                populate_existing=True
            )
        return await self._first(statement)

    async def list_pending_for_conversation(self, conversation_id: uuid.UUID) -> list[FollowUp]:
        """Every pending nudge on this conversation that is free to lock, locked.

        Should be at most one, and the index says so. Written as a list anyway
        because cancellation runs on the inbound path, where quietly leaving a
        second row behind would keep nudging a customer who has already replied.

        `SKIP LOCKED`, because the callers - an inbound message, a takeover -
        already hold the conversation row, while the sweep's re-take holds the
        follow-up and then touches the conversation: waiting here would be a
        lock-order inversion. A row the sweep holds is mid-dispatch, and its own
        dispatch-time checks and send intent decide it - the same outcome that
        race always had.
        """
        # Staged changes first: sessions here do not autoflush, and the
        # re-read below would otherwise overwrite them with the database's copy.
        await self.session.flush()
        return await self._all(
            self._select()
            .where(
                FollowUp.conversation_id == conversation_id,
                FollowUp.status == FollowUpStatus.PENDING,
            )
            .with_for_update(skip_locked=True, key_share=True)
            .execution_options(populate_existing=True)
        )

    async def list_pending_by_actor(self, kind: ActorKind) -> list[FollowUp]:
        """Every pending nudge in this workspace that `kind` scheduled, locked.

        Written for the lifecycle transitions that have to stop automated
        messages without touching what a colleague arranged (PD-TOOLS-06). The
        workspace filter is the repository's; `kind` is what separates an AI
        decision from a person's.
        """
        # Staged changes first: sessions here do not autoflush, and the
        # re-read below would otherwise overwrite them with the database's copy.
        await self.session.flush()
        return await self._all(
            self._select()
            .where(
                FollowUp.status == FollowUpStatus.PENDING,
                FollowUp.created_by_kind == kind,
            )
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )

    async def lock_pending_created_by(self, user_id: uuid.UUID) -> list[FollowUp]:
        """Every pending nudge a colleague scheduled here, locked, as committed now.

        For that colleague's removal from the workspace (PD-CRM-8).
        """
        # Staged changes first: sessions here do not autoflush, and the
        # re-read below would otherwise overwrite them with the database's copy.
        await self.session.flush()
        return await self._all(
            self._select()
            .where(
                FollowUp.status == FollowUpStatus.PENDING,
                FollowUp.created_by_kind == ActorKind.USER,
                FollowUp.created_by_id == user_id,
            )
            .order_by(FollowUp.id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )

    async def reacquire(
        self,
        follow_up_id: uuid.UUID,
        claim_token: uuid.UUID | None,
    ) -> FollowUp | None:
        """The row again, locked, if it is still pending under this claim - after a send.

        The send commits its intent part-way through, which ends the re-take's
        lock, so the outcome is written in a later transaction. Before writing
        it the worker proves the row is still the one it claimed: a lease that
        ran out while Meta was slow can have handed it to another worker.
        """
        # Staged changes first: sessions here do not autoflush, and the
        # re-read below would otherwise overwrite them with the database's copy.
        await self.session.flush()
        statement = (
            self._select()
            .where(
                FollowUp.id == follow_up_id,
                FollowUp.status == FollowUpStatus.PENDING,
                FollowUp.claim_token.is_not_distinct_from(claim_token),
            )
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        return await self._first(statement)

    async def list_follow_ups(
        self,
        *,
        statuses: tuple[FollowUpStatus, ...] = (),
        conversation_id: uuid.UUID | None = None,
        lead_id: uuid.UUID | None = None,
        limit: int = 50,
        after: Cursor | None = None,
    ) -> list[FollowUp]:
        """Soonest-scheduled first, paged by keyset."""
        query = self._select()
        if statuses:
            query = query.where(FollowUp.status.in_(statuses))
        if conversation_id is not None:
            query = query.where(FollowUp.conversation_id == conversation_id)
        if lead_id is not None:
            query = query.where(FollowUp.lead_id == lead_id)
        if after is not None:
            query = query.where(_after_scheduled(after))
        return await self._all(
            query.order_by(FollowUp.scheduled_at.desc(), FollowUp.id.desc()).limit(limit)
        )

    def create(
        self,
        *,
        conversation_id: uuid.UUID,
        scheduled_at: datetime,
        body: str | None = None,
        template_name: str | None = None,
        template_language: str | None = None,
        template_components: list[dict[str, Any]] | None = None,
        reason: str | None = None,
        lead_id: uuid.UUID | None = None,
        created_by_id: uuid.UUID | None = None,
        created_by_kind: ActorKind = ActorKind.USER,
    ) -> FollowUp:
        """Stage a follow-up. The tenant comes from this repository, never the caller."""
        return self.add(
            FollowUp(
                tenant_id=self.tenant_id,
                conversation_id=conversation_id,
                lead_id=lead_id,
                scheduled_at=scheduled_at,
                status=FollowUpStatus.PENDING,
                body=body,
                template_name=template_name,
                template_language=template_language,
                template_components=template_components,
                reason=reason,
                created_by_id=created_by_id,
                created_by_kind=created_by_kind,
                attempts=0,
            )
        )


class DueFollowUpClaim(BaseRepository[FollowUp]):
    """Claims follow-ups that are due, across every workspace.

    **Deliberately not tenant-scoped**, and the only repository in the codebase
    that is not. The worker is a platform process sweeping all workspaces on a
    timer; there is no authenticated tenant for it to be confined to. It is a
    separate class rather than a method on the scoped repository so that
    "unscoped" is a thing you have to reach for by name, and shows up in a
    review as one.

    Nothing here is reachable from a request: no route constructs it, and the
    rows it returns are handed straight back to a tenant-scoped service keyed on
    each row's own `tenant_id`.
    """

    model = FollowUp

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int = DEFAULT_CLAIM_LIMIT,
        lease_until: datetime | None = None,
    ) -> list[FollowUp]:
        """Lock, claim and return the follow-ups whose time has come.

        ``FOR UPDATE SKIP LOCKED`` is what makes more than one worker safe. Two
        replicas sweeping at the same instant would otherwise both read the same
        pending rows and send the customer the same nudge twice; with the lock,
        the second replica steps over what the first has taken and picks up the
        rows behind it instead of blocking on them.

        `lease_until` stamps each claimed row with a fresh `claim_token` and a
        `claimed_until`, committed with the claim, which is what keeps the
        guarantee once the lock is gone (ADR-093). The lock ends with the
        transaction, and the send commits part-way through - so the lock alone
        would stop protecting the rows at the first send. A worker that dies
        leaves rows that become claimable again when the lease elapses.

        The claim is its own columns, not `scheduled_at` pushed forward
        (CRM-09). A colleague's reschedule is then a different fact from the
        lease, and clearing the token is how it tells a worker holding a stale
        claim that the row is no longer its to send.
        """
        statement = (
            select(FollowUp)
            .where(
                FollowUp.status == FollowUpStatus.PENDING,
                FollowUp.scheduled_at <= now,
                or_(FollowUp.claimed_until.is_(None), FollowUp.claimed_until <= now),
            )
            .order_by(FollowUp.scheduled_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(statement)
        claimed = list(result.scalars().all())
        if lease_until is not None:
            for row in claimed:
                row.claim_token = uuid.uuid4()
                row.claimed_until = lease_until
            await self.session.flush()
        return claimed

    async def claim_by_id(
        self,
        follow_up_id: uuid.UUID,
        claim_token: uuid.UUID | None = None,
    ) -> FollowUp | None:
        """Re-take one claimed row in a transaction of its own, if it is still this claim's.

        The batch claim above hands back identifiers rather than rows to work
        with, because its transaction has committed by the time any of them is
        sent and an object from it is a snapshot. This reads the row again under
        its own lock, and only if it is still pending and still carries
        `claim_token` - so a follow-up cancelled or rescheduled since
        the claim is not found here, and nothing is sent (CRM-09, CRM-10).

        `SKIP LOCKED` rather than a wait: if somebody else holds this row, the
        answer is to move on, not to queue behind them.
        """
        statement = (
            select(FollowUp)
            .where(
                FollowUp.id == follow_up_id,
                FollowUp.status == FollowUpStatus.PENDING,
                FollowUp.claim_token.is_not_distinct_from(claim_token),
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(statement)).scalars().one_or_none()
