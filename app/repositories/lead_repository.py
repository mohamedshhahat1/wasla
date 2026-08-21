"""Data access for leads, their notes and their activity log.

The filter combinations a CRM list needs are built here rather than in the
service, so every one of them starts from the tenant-scoped select and none can
be assembled without it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, Select, and_, func, or_, select

from app.core.pagination import Cursor
from app.db.models.lead import (
    TERMINAL_STATUSES,
    ActorKind,
    Lead,
    LeadActivity,
    LeadActivityKind,
    LeadNote,
    LeadSource,
    LeadStatus,
)
from app.repositories.base import TenantScopedRepository


@dataclass(frozen=True, slots=True)
class LeadFilters:
    """What a caller may narrow a lead list by.

    A dataclass rather than a pile of keyword arguments because the same set is
    used by the list query and by the count behind the statistics endpoint, and
    two hand-written copies of it would drift.

    Every field defaults to "no restriction", so an empty instance means the
    whole workspace and filters compose by intersection.
    """

    statuses: tuple[LeadStatus, ...] = ()
    sources: tuple[LeadSource, ...] = ()
    assigned_to_id: uuid.UUID | None = None
    # Distinct from `assigned_to_id is None`, which means "do not filter". This
    # asks specifically for the unassigned queue, which is the one a manager
    # actually looks at.
    unassigned_only: bool = False
    tags: tuple[str, ...] = ()
    search: str | None = None
    contact_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class LeadStatistics:
    """Counts for a workspace's pipeline."""

    total: int
    open_leads: int
    unassigned: int
    by_status: dict[LeadStatus, int] = field(default_factory=dict)


def _after_created(after: Cursor) -> ColumnElement[bool]:
    """Rows following `after` under ``ORDER BY created_at DESC, id DESC``.

    `created_at` is never null, so unlike the conversation cursor this needs no
    null block: a cursor without a sort value cannot arise from this ordering,
    and one that arrives anyway falls back to the id alone rather than being
    trusted to mean something.
    """
    if after.sort_value is None:
        return Lead.id < after.id
    return or_(
        Lead.created_at < after.sort_value,
        and_(Lead.created_at == after.sort_value, Lead.id < after.id),
    )


class LeadRepository(TenantScopedRepository[Lead]):
    """Leads of one workspace."""

    model = Lead

    def _tenant_filter(self) -> ColumnElement[bool]:
        return Lead.tenant_id == self.tenant_id

    async def get_by_id(self, lead_id: uuid.UUID) -> Lead | None:
        return await self._first(self._select().where(Lead.id == lead_id))

    async def require_by_id(self, lead_id: uuid.UUID) -> Lead:
        return await self._require(self._select().where(Lead.id == lead_id))

    async def get_active_for_contact(self, contact_id: uuid.UUID) -> Lead | None:
        """The customer's open opportunity, if they have one.

        This is the deterministic match that keeps an agent from opening a new
        lead on every message. It looks for an *open* lead only, so a customer
        who bought or walked away last quarter starts a fresh one rather than
        reanimating a closed record.

        The partial unique index makes at most one row possible, so this cannot
        quietly pick between several.
        """
        return await self._first(
            self._select().where(
                Lead.contact_id == contact_id,
                Lead.status.not_in(tuple(TERMINAL_STATUSES)),
            )
        )

    def _filtered(self, filters: LeadFilters) -> Select[tuple[Lead]]:
        """Apply filters on top of the tenant-scoped select."""
        query = self._select()

        if filters.statuses:
            query = query.where(Lead.status.in_(filters.statuses))
        if filters.sources:
            query = query.where(Lead.source.in_(filters.sources))
        if filters.unassigned_only:
            query = query.where(Lead.assigned_to_id.is_(None))
        elif filters.assigned_to_id is not None:
            query = query.where(Lead.assigned_to_id == filters.assigned_to_id)
        if filters.tags:
            # Containment: the lead must carry every tag asked for.
            query = query.where(Lead.tags.contains(list(filters.tags)))
        if filters.contact_id is not None:
            query = query.where(Lead.contact_id == filters.contact_id)
        if filters.conversation_id is not None:
            query = query.where(Lead.conversation_id == filters.conversation_id)
        if filters.search:
            # `ilike` with both wildcards will not use a b-tree index, which is
            # the right trade at a workspace's scale: a CRM search box is used
            # interactively against thousands of rows, not millions, and full
            # text search would need its own index and its own language config.
            # Escaped so a customer named "100%" is searched for, not matched by
            # everything.
            pattern = f"%{_escape_like(filters.search)}%"
            query = query.where(
                or_(
                    Lead.name.ilike(pattern, escape="\\"),
                    Lead.email.ilike(pattern, escape="\\"),
                    Lead.phone.ilike(pattern, escape="\\"),
                    Lead.interest.ilike(pattern, escape="\\"),
                )
            )
        return query

    async def list_leads(
        self,
        *,
        filters: LeadFilters | None = None,
        limit: int = 50,
        after: Cursor | None = None,
    ) -> list[Lead]:
        """Newest first, paged by keyset."""
        query = self._filtered(filters or LeadFilters())
        if after is not None:
            query = query.where(_after_created(after))
        return await self._all(query.order_by(Lead.created_at.desc(), Lead.id.desc()).limit(limit))

    async def statistics(self, *, filters: LeadFilters | None = None) -> LeadStatistics:
        """Pipeline counts, aggregated in the database.

        Grouped in one query rather than counted per status in a loop: six
        round trips to answer one dashboard panel is six chances to be slow.
        """
        query = self._filtered(filters or LeadFilters())
        subquery = query.subquery()

        result = await self.session.execute(
            select(subquery.c.status, func.count()).group_by(subquery.c.status)
        )
        by_status = {LeadStatus(status): int(count) for status, count in result.all()}

        unassigned_result = await self.session.execute(
            select(func.count()).select_from(subquery).where(subquery.c.assigned_to_id.is_(None))
        )

        total = sum(by_status.values())
        closed = sum(by_status.get(status, 0) for status in TERMINAL_STATUSES)
        return LeadStatistics(
            total=total,
            open_leads=total - closed,
            unassigned=int(unassigned_result.scalar_one()),
            by_status=by_status,
        )

    def create(
        self,
        *,
        source: LeadSource,
        contact_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
        status: LeadStatus = LeadStatus.NEW,
        name: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        interest: str | None = None,
        budget_amount: object | None = None,
        budget_currency: str | None = None,
        score: int = 0,
        assigned_to_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        custom_fields: dict[str, object] | None = None,
        human_verified_fields: list[str] | None = None,
        last_activity_at: datetime | None = None,
    ) -> Lead:
        """Stage a new lead. The tenant comes from this repository, never the caller."""
        return self.add(
            Lead(
                tenant_id=self.tenant_id,
                contact_id=contact_id,
                conversation_id=conversation_id,
                status=status,
                source=source,
                name=name,
                phone=phone,
                email=email,
                interest=interest,
                budget_amount=budget_amount,
                budget_currency=budget_currency,
                score=score,
                assigned_to_id=assigned_to_id,
                tags=tags or [],
                custom_fields=custom_fields or {},
                human_verified_fields=human_verified_fields or [],
                last_activity_at=last_activity_at,
            )
        )


class LeadNoteRepository(TenantScopedRepository[LeadNote]):
    """Notes on the leads of one workspace."""

    model = LeadNote

    def _tenant_filter(self) -> ColumnElement[bool]:
        return LeadNote.tenant_id == self.tenant_id

    async def list_for_lead(
        self,
        *,
        lead_id: uuid.UUID,
        limit: int = 50,
        after: Cursor | None = None,
    ) -> list[LeadNote]:
        query = self._select().where(LeadNote.lead_id == lead_id)
        if after is not None:
            query = query.where(
                or_(
                    LeadNote.created_at < after.sort_value,
                    and_(LeadNote.created_at == after.sort_value, LeadNote.id < after.id),
                )
                if after.sort_value is not None
                else LeadNote.id < after.id
            )
        return await self._all(
            query.order_by(LeadNote.created_at.desc(), LeadNote.id.desc()).limit(limit)
        )

    def create(
        self,
        *,
        lead_id: uuid.UUID,
        body: str,
        author_id: uuid.UUID | None = None,
        author_kind: ActorKind = ActorKind.USER,
    ) -> LeadNote:
        return self.add(
            LeadNote(
                tenant_id=self.tenant_id,
                lead_id=lead_id,
                body=body,
                author_id=author_id,
                author_kind=author_kind,
            )
        )


class LeadActivityRepository(TenantScopedRepository[LeadActivity]):
    """The activity log of one workspace's leads.

    Append-only by design: there is a `record` and a `list_for_lead`, and no
    method that updates or deletes. An audit trail the application can rewrite
    does not answer the question it exists to answer.
    """

    model = LeadActivity

    def _tenant_filter(self) -> ColumnElement[bool]:
        return LeadActivity.tenant_id == self.tenant_id

    async def list_for_lead(
        self,
        *,
        lead_id: uuid.UUID,
        limit: int = 50,
        after: Cursor | None = None,
    ) -> list[LeadActivity]:
        query = self._select().where(LeadActivity.lead_id == lead_id)
        if after is not None:
            query = query.where(
                or_(
                    LeadActivity.created_at < after.sort_value,
                    and_(
                        LeadActivity.created_at == after.sort_value,
                        LeadActivity.id < after.id,
                    ),
                )
                if after.sort_value is not None
                else LeadActivity.id < after.id
            )
        return await self._all(
            query.order_by(LeadActivity.created_at.desc(), LeadActivity.id.desc()).limit(limit)
        )

    def record(
        self,
        *,
        lead_id: uuid.UUID,
        kind: LeadActivityKind,
        summary: str,
        actor_id: uuid.UUID | None = None,
        actor_kind: ActorKind = ActorKind.SYSTEM,
        data: dict[str, object] | None = None,
    ) -> LeadActivity:
        return self.add(
            LeadActivity(
                tenant_id=self.tenant_id,
                lead_id=lead_id,
                kind=kind,
                summary=summary[:300],
                actor_id=actor_id,
                actor_kind=actor_kind,
                data=data,
            )
        )


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards in user-supplied search text.

    Without this a search for `%` matches every lead in the workspace, and one
    for `_` matches every single-character name - which reads as a broken search
    box rather than as an injection, but is the same mistake.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
