"""Counting business-initiated sends against Meta's limit, and the campaigns' 80 % (LIM-03, LIM-04).

The reservation, the decisive guard that a portfolio never messages more unique
people in a moving 24 hours than Meta allows it, in one transaction - the send
intent's (ADR-093 TX1):

    pg_advisory_xact_lock(portfolio)     every sender on the portfolio queues here
    count distinct people, last 24 h     all sources, every number and workspace
    decide                               a campaign within its share; a test within
                                         the limit; anything else always (recorded)
    insert the ledger row                with the message the intent stages
    COMMIT                               the lock ends; Meta is asked after it

**What counts.** A template message to a person whose service window on that
number is closed: their conversation has had no inbound message for 23 hours -
one hour of margin against the 24-hour window closing between this check and
Meta handling the request. An open window costs nothing and is not recorded. A
person already counted in the window is not a new person.

**Who is held back.** Campaigns (by `CAMPAIGN_PORTFOLIO_LIMIT_SHARE` of the
limit, 80 %), test sends (by the whole limit, refused while it is unknown), and
template follow-ups and manual templates (by the whole limit, `DEFER_AT_LIMIT`:
the other 20 % is theirs, and at 100 % a follow-up waits for `resumes_at` and a
person is told when - FLIM-01, FLIM-02). While the limit is unknown those two
are sent and recorded with `limit_at_send` null: one person somebody is waiting
on is not held back by a read nobody has made. Anything else is recorded only.
A free-form message inside the customer's window never reaches the ledger
(FLIM-03).

**Released only when provably undelivered.** `release` deletes the row of a
send Meta refused (the message recorded UNDELIVERED); an uncertain answer keeps
it - counting somebody who may not have been reached is the safe error.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import advisory_locks
from app.db.models.business_initiated import BusinessInitiatedSend, LedgerSource
from app.db.models.messaging_limit import WhatsAppBusinessPortfolio
from app.db.models.whatsapp import WhatsAppAccount
from app.services import messaging_limit_service as portfolio_limits

#: Meta's moving window.
WINDOW: Final = timedelta(hours=24)
#: A conversation with an inbound message more recent than this is inside its
#: customer service window: 23 hours, an hour short of Meta's 24.
SERVICE_WINDOW_WITH_MARGIN: Final = timedelta(hours=23)
#: Added to the moment a counted person leaves the window before a waiting
#: campaign asks again, so it never asks a moment early.
RESUME_MARGIN: Final = timedelta(minutes=5)
#: The stable error codes a refused test send meets.
PORTFOLIO_LIMIT_REACHED_CODE: Final = "portfolio_limit_reached"


class Enforcement(StrEnum):
    """Whose budget a reservation is checked against."""

    #: The campaign share: floor(share x limit).
    CAMPAIGN = "campaign"
    #: The whole limit: a test send is refused only when nothing is left.
    WHOLE_LIMIT = "whole_limit"
    #: The whole limit, but sent while the limit is unknown: a template
    #: follow-up waits at 100 %, a manual template is refused with when
    #: (FLIM-01, FLIM-02).
    DEFER_AT_LIMIT = "defer_at_limit"
    #: Recorded, never refused.
    RECORD_ONLY = "record_only"


#: The enforcements that refuse a send while nobody has read the limit.
_FAIL_CLOSED: Final = frozenset({Enforcement.CAMPAIGN, Enforcement.WHOLE_LIMIT})


class PortfolioLimitReachedError(Exception):
    """This send would put the portfolio over its budget for the moving 24 hours."""

    def __init__(self, *, resumes_at: datetime, used: int, budget: int) -> None:
        super().__init__("The business portfolio's messaging budget is used for now.")
        self.resumes_at = resumes_at
        self.used = used
        self.budget = budget


class MessagingLimitUnknownError(Exception):
    """Nobody has read Meta's limit for this number's portfolio: nothing is sent (LIM-01)."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """A decision taken under the lock; written with the message that is about to be staged."""

    portfolio_id: uuid.UUID
    recipient: str
    sent_at: datetime
    source: LedgerSource
    tenant_id: uuid.UUID
    campaign_id: uuid.UUID | None
    limit_at_send: int | None
    budget_at_send: int | None


@dataclass(frozen=True, slots=True)
class WindowUsage:
    """Distinct people of one portfolio in the 24 hours ending at `at`."""

    used: int
    #: When the first of them leaves the window (plus the margin); None if nobody is in it.
    next_capacity_at: datetime | None


def campaign_budget(limit: int | None, share: float) -> int | None:
    """Unique people campaigns may reach per moving 24 hours; None for unlimited (LIM-04)."""
    if limit is None:
        return None
    return math.floor(share * limit)


def window_open(last_inbound_at: datetime | None, *, at: datetime) -> bool:
    """Whether a conversation's customer service window is open at `at`, with the margin."""
    return last_inbound_at is not None and last_inbound_at > at - SERVICE_WINDOW_WITH_MARGIN


#: How full a portfolio's moving 24 hours is, as a closed set of buckets: the
#: gauge counts portfolios per bucket and never names one (ADR-072).
UTILISATION_BUCKETS: Final = ("<50", "50-80", "80-100", ">=100")


def utilisation_bucket(used: int, limit: int) -> str:
    """Which bucket `used` of `limit` falls in: the share of Meta's whole limit, all sources."""
    share = used * 100 / limit
    if share >= 100:
        return ">=100"
    if share >= 80:
        return "80-100"
    if share >= 50:
        return "50-80"
    return "<50"


async def utilisation_census(session: AsyncSession, *, at: datetime) -> dict[str, int]:
    """How many portfolios with a known, finite limit sit in each bucket now (LIM-07).

    Distinct people of the last 24 hours, every source - the count Meta's
    limit is about - against the whole limit, not the campaigns' share: what
    `PortfolioLimitReached` warns of is follow-ups and manual templates
    running into Meta's own ceiling.
    """
    rows = await session.execute(
        select(
            WhatsAppBusinessPortfolio.messaging_limit,
            select(func.count(func.distinct(BusinessInitiatedSend.recipient)))
            .where(
                BusinessInitiatedSend.portfolio_id == WhatsAppBusinessPortfolio.id,
                BusinessInitiatedSend.sent_at > at - WINDOW,
            )
            .scalar_subquery(),
        ).where(WhatsAppBusinessPortfolio.messaging_limit.is_not(None))
    )
    census = dict.fromkeys(UTILISATION_BUCKETS, 0)
    for limit, used in rows.all():
        census[utilisation_bucket(int(used or 0), int(limit))] += 1
    return census


class BusinessInitiatedLedger:
    """The ledger of one send path. Platform-level: the count crosses workspaces."""

    def __init__(self, session: AsyncSession, *, share: float) -> None:
        self._session = session
        self._share = share

    async def _portfolio(
        self, account_id: uuid.UUID
    ) -> tuple[portfolio_limits.PortfolioLimit, uuid.UUID | None]:
        limit = await portfolio_limits.limit_for_account(self._session, account_id)
        if limit.portfolio_id is not None:
            return limit, limit.portfolio_id
        account = await self._session.get(WhatsAppAccount, account_id)
        if account is None:
            return limit, None
        # A number nothing has linked yet: its portfolio is the WABA's, created
        # unknown if need be, so the send is still counted.
        portfolio = await portfolio_limits.portfolio_for_waba(self._session, account.waba_id)
        account.portfolio_id = portfolio.id
        return limit, portfolio.id

    async def _lock(self, portfolio_id: uuid.UUID) -> None:
        await self._session.execute(
            select(
                func.pg_advisory_xact_lock(
                    advisory_locks.BUSINESS_PORTFOLIO, portfolio_id.int & 0x7FFFFFFF
                )
            )
        )

    async def usage(self, portfolio_id: uuid.UUID, *, at: datetime) -> WindowUsage:
        """Distinct people in the window ending at `at`, and when the first of them leaves it."""
        # No upper bound: a row a racing sender dated a moment after `at`
        # still counts. Counting too many is the safe error.
        in_window = and_(
            BusinessInitiatedSend.portfolio_id == portfolio_id,
            BusinessInitiatedSend.sent_at > at - WINDOW,
        )
        used = int(
            await self._session.scalar(
                select(func.count(func.distinct(BusinessInitiatedSend.recipient))).where(in_window)
            )
            or 0
        )
        last_per_person = (
            select(func.max(BusinessInitiatedSend.sent_at).label("last"))
            .where(in_window)
            .group_by(BusinessInitiatedSend.recipient)
            .subquery()
        )
        first_to_leave = await self._session.scalar(select(func.min(last_per_person.c.last)))
        return WindowUsage(
            used=used,
            next_capacity_at=(
                first_to_leave + WINDOW + RESUME_MARGIN if first_to_leave is not None else None
            ),
        )

    async def _counted(self, portfolio_id: uuid.UUID, recipient: str, *, at: datetime) -> bool:
        found = await self._session.scalar(
            select(BusinessInitiatedSend.id)
            .where(
                BusinessInitiatedSend.portfolio_id == portfolio_id,
                BusinessInitiatedSend.recipient == recipient,
                BusinessInitiatedSend.sent_at > at - WINDOW,
            )
            .limit(1)
        )
        return found is not None

    async def reserve(
        self,
        *,
        account_id: uuid.UUID,
        recipient: str,
        last_inbound_at: datetime | None,
        source: LedgerSource,
        enforcement: Enforcement,
        tenant_id: uuid.UUID,
        campaign_id: uuid.UUID | None,
        at: datetime,
    ) -> Reservation | None:
        """Decide this send under the portfolio's lock. None: inside the service window.

        Raises `PortfolioLimitReachedError` or `MessagingLimitUnknownError`
        before anything is written; the lock is released with the transaction.
        """
        if window_open(last_inbound_at, at=at):
            return None
        limit, portfolio_id = await self._portfolio(account_id)
        if enforcement in _FAIL_CLOSED and not limit.known:
            raise MessagingLimitUnknownError()
        if portfolio_id is None:
            return None
        await self._lock(portfolio_id)

        budget = (
            campaign_budget(limit.messaging_limit, self._share)
            if enforcement is Enforcement.CAMPAIGN
            else (
                limit.messaging_limit
                if enforcement is Enforcement.WHOLE_LIMIT
                or (enforcement is Enforcement.DEFER_AT_LIMIT and limit.known)
                else None
            )
        )
        if budget is not None and not await self._counted(portfolio_id, recipient, at=at):
            usage = await self.usage(portfolio_id, at=at)
            if usage.used + 1 > budget:
                raise PortfolioLimitReachedError(
                    resumes_at=usage.next_capacity_at or at + WINDOW,
                    used=usage.used,
                    budget=budget,
                )
        return Reservation(
            portfolio_id=portfolio_id,
            recipient=recipient,
            sent_at=at,
            source=source,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            limit_at_send=limit.messaging_limit if limit.known else None,
            budget_at_send=(
                campaign_budget(limit.messaging_limit, self._share)
                if source is LedgerSource.CAMPAIGN and limit.known
                else None
            ),
        )

    def record(self, reservation: Reservation, *, message_id: uuid.UUID) -> None:
        """Stage the row in the send intent's transaction."""
        self._session.add(
            BusinessInitiatedSend(
                portfolio_id=reservation.portfolio_id,
                recipient=reservation.recipient,
                sent_at=reservation.sent_at,
                source=reservation.source.value,
                tenant_id=reservation.tenant_id,
                campaign_id=reservation.campaign_id,
                message_id=message_id,
                limit_at_send=reservation.limit_at_send,
                budget_at_send=reservation.budget_at_send,
            )
        )

    async def recorded_without_limit(self, message_id: uuid.UUID) -> bool:
        """Whether this message's ledger row was written while the limit was unknown."""
        found = await self._session.scalar(
            select(BusinessInitiatedSend.id)
            .where(
                BusinessInitiatedSend.message_id == message_id,
                BusinessInitiatedSend.limit_at_send.is_(None),
            )
            .limit(1)
        )
        return found is not None

    async def release(self, message_id: uuid.UUID) -> int:
        """Forget the send of a message Meta provably did not accept."""
        result = await self._session.execute(
            delete(BusinessInitiatedSend)
            .where(BusinessInitiatedSend.message_id == message_id)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def prune(self, *, before: datetime) -> int:
        """Delete rows older than `before` (retention: 48 hours)."""
        result = await self._session.execute(
            delete(BusinessInitiatedSend)
            .where(BusinessInitiatedSend.sent_at < before)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class LimitView:
    """What the campaigns page may show about one number's limit (LIM-07).

    Aggregates of the whole portfolio and nothing else: no other workspace's
    name, number, campaign or count by source - even when another workspace
    shares the portfolio, because the page needs the truth about what this
    business can send.
    """

    tier: str | None
    known: bool
    messaging_limit: int | None
    checked_at: datetime | None
    source: str | None
    campaign_share: float
    campaign_budget: int | None
    used_last_24h: int
    campaign_available_now: int | None
    next_capacity_at: datetime | None
    shared_with_other_numbers: bool
    shared_with_other_workspaces: bool


async def limit_view(
    session: AsyncSession, account: WhatsAppAccount, *, share: float, at: datetime
) -> LimitView:
    """One number's portfolio limit, used and free, now (LIM-07)."""
    from app.db.models.messaging_limit import WhatsAppBusinessPortfolio

    limit = await portfolio_limits.limit_for_account(session, account.id)
    portfolio = (
        await session.get(WhatsAppBusinessPortfolio, limit.portfolio_id)
        if limit.portfolio_id is not None
        else None
    )
    usage = (
        await BusinessInitiatedLedger(session, share=share).usage(limit.portfolio_id, at=at)
        if limit.portfolio_id is not None
        else WindowUsage(used=0, next_capacity_at=None)
    )
    others = (
        list(
            (
                await session.execute(
                    select(WhatsAppAccount.tenant_id).where(
                        WhatsAppAccount.portfolio_id == limit.portfolio_id,
                        WhatsAppAccount.id != account.id,
                        WhatsAppAccount.released_at.is_(None),
                    )
                )
            ).scalars()
        )
        if limit.portfolio_id is not None
        else []
    )
    budget = campaign_budget(limit.messaging_limit, share) if limit.known else None
    return LimitView(
        tier=limit.tier,
        known=limit.known,
        messaging_limit=limit.messaging_limit,
        checked_at=limit.checked_at,
        source=portfolio.source if portfolio is not None else None,
        campaign_share=share,
        campaign_budget=budget,
        used_last_24h=usage.used,
        campaign_available_now=max(budget - usage.used, 0) if budget is not None else None,
        next_capacity_at=usage.next_capacity_at,
        shared_with_other_numbers=bool(others),
        shared_with_other_workspaces=any(tenant != account.tenant_id for tenant in others),
    )


__all__ = [
    "PORTFOLIO_LIMIT_REACHED_CODE",
    "RESUME_MARGIN",
    "SERVICE_WINDOW_WITH_MARGIN",
    "WINDOW",
    "BusinessInitiatedLedger",
    "Enforcement",
    "LimitView",
    "MessagingLimitUnknownError",
    "PortfolioLimitReachedError",
    "Reservation",
    "WindowUsage",
    "campaign_budget",
    "limit_view",
    "window_open",
]
