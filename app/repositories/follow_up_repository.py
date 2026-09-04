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

    async def get_pending_for_conversation(self, conversation_id: uuid.UUID) -> FollowUp | None:
        """The conversation's waiting nudge, if it has one.

        The partial unique index makes at most one row possible, so this cannot
        quietly pick between several.
        """
        return await self._first(
            self._select().where(
                FollowUp.conversation_id == conversation_id,
                FollowUp.status == FollowUpStatus.PENDING,
            )
        )

    async def list_pending_for_conversation(self, conversation_id: uuid.UUID) -> list[FollowUp]:
        """Every pending nudge on this conversation.

        Should be at most one, and the index says so. Written as a list anyway
        because cancellation runs on the inbound path, where quietly leaving a
        second row behind would keep nudging a customer who has already replied.
        """
        return await self._all(
            self._select().where(
                FollowUp.conversation_id == conversation_id,
                FollowUp.status == FollowUpStatus.PENDING,
            )
        )

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
        """Lock and return the follow-ups whose time has come.

        ``FOR UPDATE SKIP LOCKED`` is what makes more than one worker safe. Two
        replicas sweeping at the same instant would otherwise both read the same
        pending rows and send the customer the same nudge twice; with the lock,
        the second replica steps over what the first has taken and picks up the
        rows behind it instead of blocking on them.

        `lease_until` pushes each claimed row's due time out and is committed
        with the claim, which is what keeps the guarantee once the lock is gone
        (ADR-093). The lock ends with the transaction, and the send now commits
        part-way through — so the lock alone would stop protecting the rows at
        the first send. A lease is a fact on the row; a worker that dies leaves
        rows that become due again when it elapses.
        """
        statement = (
            select(FollowUp)
            .where(
                FollowUp.status == FollowUpStatus.PENDING,
                FollowUp.scheduled_at <= now,
            )
            .order_by(FollowUp.scheduled_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(statement)
        claimed = list(result.scalars().all())
        if lease_until is not None:
            for row in claimed:
                row.scheduled_at = lease_until
            await self.session.flush()
        return claimed

    async def claim_by_id(self, follow_up_id: uuid.UUID) -> FollowUp | None:
        """Re-take one claimed row in a transaction of its own.

        The batch claim above hands back identifiers rather than rows to work
        with, because its transaction has committed by the time any of them is
        sent and an object from it is a snapshot. This reads the row again under
        its own lock, so a follow-up an inbound message cancelled in between is
        seen as cancelled rather than sent.

        `SKIP LOCKED` rather than a wait: if somebody else holds this row, the
        answer is to move on, not to queue behind them.
        """
        statement = (
            select(FollowUp).where(FollowUp.id == follow_up_id).with_for_update(skip_locked=True)
        )
        return (await self.session.execute(statement)).scalars().one_or_none()
