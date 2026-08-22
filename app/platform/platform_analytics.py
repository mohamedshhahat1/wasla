"""The platform owner's view of the whole estate.

Read-only, and narrower than the eventual dashboard on purpose. Everything here
is a figure this system can actually compute from rows it holds:

- how many workspaces exist and what state they are in
- how many WhatsApp numbers are connected and live
- what the platform consumed, and what each workspace consumed

What is deliberately absent is revenue. MRR, ARR, subscription revenue and
churn are questions about subscriptions, and there are no subscriptions until
Phase 13; a dashboard that displayed a plausible zero would be worse than one
that displays nothing, because somebody would eventually believe it. The same
goes for estimated AI cost: token counts are real and per-model prices are not
stored anywhere, so any figure here would be a number this system invented.

Tenant *administration* - creating, suspending, deleting a workspace - is not
here either. This module answers questions; it changes nothing, which is what
lets it be read without an audit trail that does not exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import TenantStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.platform.platform_usage import PlatformUsageService
from app.services.usage_service import UsageSummary, UsageWindow, resolve_window

# A page of workspaces on a dashboard. Offset paging is used here rather than
# the keyset cursors the tenant API uses, and the reason is that this list is
# sorted by name and searched by hand: an operator typing into a search box
# wants page three of forty results, not a stable feed. The population is
# thousands, not millions.
DEFAULT_PAGE = 25
MAX_PAGE = 100


@dataclass(frozen=True, slots=True)
class PlatformOverview:
    """The estate, at a glance, for one window."""

    window: UsageWindow
    # Required rather than optional: an overview without usage is not an
    # overview, and a nullable field here would push a "what if there is none"
    # branch into every consumer for a case the service cannot produce.
    usage: UsageSummary
    tenants_total: int = 0
    tenants_active: int = 0
    tenants_suspended: int = 0
    whatsapp_numbers: int = 0
    whatsapp_numbers_active: int = 0


@dataclass(frozen=True, slots=True)
class WorkspaceRow:
    """One workspace as a platform dashboard lists it."""

    tenant: Tenant
    usage: UsageSummary


@dataclass(frozen=True, slots=True)
class WorkspacePage:
    """A page of workspaces, with the total so an operator can page through it."""

    window: UsageWindow
    total: int
    rows: list[WorkspaceRow] = field(default_factory=list)


class PlatformAnalyticsService:
    """Cross-tenant reporting for platform staff."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._usage = PlatformUsageService(session)

    async def overview(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> PlatformOverview:
        window = resolve_window(since=since, until=until)

        by_status = await self._session.execute(
            select(Tenant.status, func.count())
            # Soft-deleted workspaces are excluded from every count here. They
            # are gone as far as an operator is concerned, and including them
            # would make "total" disagree with the list below it.
            .where(Tenant.deleted_at.is_(None)).group_by(Tenant.status)
        )
        counts = {status: int(count) for status, count in by_status.all()}

        numbers = await self._session.execute(
            select(WhatsAppAccount.status, func.count()).group_by(WhatsAppAccount.status)
        )
        by_account_status = {status: int(count) for status, count in numbers.all()}

        return PlatformOverview(
            window=window,
            tenants_total=sum(counts.values()),
            tenants_active=counts.get(TenantStatus.ACTIVE, 0),
            tenants_suspended=counts.get(TenantStatus.SUSPENDED, 0),
            whatsapp_numbers=sum(by_account_status.values()),
            whatsapp_numbers_active=by_account_status.get(WhatsAppAccountStatus.ACTIVE, 0),
            usage=await self._usage.totals(since=window.since, until=window.until),
        )

    async def workspaces(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        search: str | None = None,
        status: TenantStatus | None = None,
        limit: int = DEFAULT_PAGE,
        offset: int = 0,
    ) -> WorkspacePage:
        """A page of workspaces with what each consumed in the window.

        Two queries and then one aggregate over the page's ids, rather than a
        join: usage is grouped per meter, so joining it to the tenant row would
        multiply each workspace by its number of meters and leave the caller to
        undo that.
        """
        window = resolve_window(since=since, until=until)
        bounded = max(1, min(limit, MAX_PAGE))

        total = await self._session.scalar(
            self._filtered(select(func.count()).select_from(Tenant), search=search, status=status)
        )
        result = await self._session.execute(
            self._filtered(select(Tenant), search=search, status=status)
            .order_by(Tenant.name, Tenant.id)
            .limit(bounded)
            .offset(max(0, offset))
        )
        tenants = list(result.scalars().all())

        usage = await self._usage.by_tenant([tenant.id for tenant in tenants], window=window)
        return WorkspacePage(
            window=window,
            total=int(total or 0),
            rows=[WorkspaceRow(tenant=tenant, usage=usage[tenant.id]) for tenant in tenants],
        )

    def _filtered[StatementT: Select[Any]](
        self,
        statement: StatementT,
        *,
        search: str | None,
        status: TenantStatus | None,
    ) -> StatementT:
        """The same predicate for the page and its count, so they cannot disagree."""
        statement = statement.where(Tenant.deleted_at.is_(None))
        if status is not None:
            statement = statement.where(Tenant.status == status)
        if search:
            # Escaped before it reaches a LIKE pattern: a search for "100%"
            # must not become a wildcard.
            pattern = f"%{_escape_like(search.strip())}%"
            statement = statement.where(
                or_(
                    Tenant.name.ilike(pattern, escape="\\"),
                    Tenant.slug.ilike(pattern, escape="\\"),
                )
            )
        return statement


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
