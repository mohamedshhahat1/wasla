"""The facts a campaign's delivery plan and ETA are computed from (LIM-05, PG-13).

`delivery_plan.plan` is pure; this gathers what it needs for one campaign, in
one workspace, in a handful of indexed reads: who is still pending and whether
each one's service window is open or the ledger already counts them, the
portfolio's campaign budget and the people it has counted in the moving 24
hours, the workspace's quiet hours, and the rate - the campaign's own, or the
rate it was observed to send at over the last ten minutes once it is running.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.business_initiated import BusinessInitiatedSend
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.db.models.channel import ContactIdentity
from app.db.models.conversation import Contact, Conversation
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services import messaging_limit_service as portfolio_limits
from app.services.business_initiated_ledger import WINDOW, campaign_budget, window_open
from app.services.delivery_plan import (
    DeliveryPlan,
    EtaBasis,
    PlanInputs,
    PlannedRecipient,
    confidence,
    plan,
)
from app.services.quiet_hours import QuietHours

#: How far back the observed send rate is measured (PG-13).
OBSERVED_WINDOW: Final = timedelta(minutes=10)
#: Why an ETA is null, for a campaign that is not going to send by itself.
STATUS_REASONS: Final[dict[CampaignStatus, str]] = {
    CampaignStatus.DRAFT: "draft",
    CampaignStatus.PAUSED: "paused",
    CampaignStatus.FAILED: "failed",
    CampaignStatus.CANCELLED: "cancelled",
    CampaignStatus.COMPLETED: "completed",
}


@dataclass(frozen=True, slots=True)
class Eta:
    """When a live campaign is expected to finish, and on what basis (PG-13)."""

    estimated_completion_at: datetime | None
    basis: tuple[EtaBasis, ...]
    confidence: str | None
    #: Why there is no estimate: the status, or `limit_unknown`.
    reason: str | None


async def quiet_hours_of(session: AsyncSession, tenant_id: uuid.UUID) -> QuietHours:
    row = (
        await session.execute(
            select(
                Tenant.campaign_quiet_hours_enabled,
                Tenant.campaign_quiet_hours_start,
                Tenant.campaign_quiet_hours_end,
                Tenant.timezone,
            ).where(Tenant.id == tenant_id)
        )
    ).one()
    return QuietHours(enabled=row[0], start=row[1], end=row[2], timezone=row[3])


class CampaignPlanner:
    """Plans and ETAs for one workspace's campaigns."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        share: float,
        cap_interval: timedelta = timedelta(hours=24),
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._share = share
        self._cap_interval = cap_interval

    async def _portfolio(
        self, account_id: uuid.UUID, *, at: datetime
    ) -> tuple[portfolio_limits.PortfolioLimit, dict[str, datetime], bool]:
        """The limit, each counted person's last send in the window, and whether it is shared."""
        limit = await portfolio_limits.limit_for_account(self._session, account_id)
        if limit.portfolio_id is None:
            return limit, {}, False
        rows = (
            await self._session.execute(
                select(BusinessInitiatedSend.recipient, func.max(BusinessInitiatedSend.sent_at))
                .where(
                    BusinessInitiatedSend.portfolio_id == limit.portfolio_id,
                    BusinessInitiatedSend.sent_at > at - WINDOW,
                )
                .group_by(BusinessInitiatedSend.recipient)
            )
        ).all()
        shared = (
            await self._session.scalar(
                select(func.count())
                .select_from(WhatsAppAccount)
                .where(
                    WhatsAppAccount.portfolio_id == limit.portfolio_id,
                    WhatsAppAccount.id != account_id,
                    WhatsAppAccount.released_at.is_(None),
                )
            )
            or 0
        ) > 0
        counted: dict[str, datetime] = {}
        for recipient, last in rows:
            counted[recipient] = last
        return limit, counted, shared

    async def _pending(
        self, campaign: Campaign
    ) -> list[tuple[datetime | None, datetime | None, str | None, datetime | None]]:
        """Each pending person: deferral, last inbound on this number, identity, marketing cap."""
        rows = (
            await self._session.execute(
                select(
                    CampaignRecipient.not_before,
                    Conversation.last_inbound_at,
                    ContactIdentity.value,
                    Contact.last_marketing_sent_at,
                )
                .outerjoin(
                    Conversation,
                    and_(
                        Conversation.tenant_id == CampaignRecipient.tenant_id,
                        Conversation.contact_id == CampaignRecipient.contact_id,
                        Conversation.account_id == campaign.account_id,
                    ),
                )
                .outerjoin(
                    ContactIdentity, ContactIdentity.id == CampaignRecipient.participant_identity_id
                )
                .outerjoin(Contact, Contact.id == CampaignRecipient.contact_id)
                .where(
                    CampaignRecipient.tenant_id == self._tenant_id,
                    CampaignRecipient.campaign_id == campaign.id,
                    CampaignRecipient.status == RecipientStatus.PENDING,
                )
            )
        ).all()
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    async def _observed_rate(self, campaign: Campaign, *, at: datetime) -> float | None:
        sent = await self._session.scalar(
            select(func.count())
            .select_from(CampaignRecipient)
            .where(
                CampaignRecipient.tenant_id == self._tenant_id,
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.sent_at > at - OBSERVED_WINDOW,
            )
        )
        if not sent:
            return None
        return float(sent) / (OBSERVED_WINDOW.total_seconds() / 60)

    async def plan(
        self,
        campaign: Campaign,
        *,
        now: datetime,
        observed: bool = False,
    ) -> DeliveryPlan:
        """The campaign's plan from now, or from its schedule if that is later."""
        quiet = await quiet_hours_of(self._session, self._tenant_id)
        limit, counted, shared = await self._portfolio(campaign.account_id, at=now)
        recipients = []
        for not_before, last_inbound_at, identity, last_marketing in await self._pending(campaign):
            recipients.append(
                PlannedRecipient(
                    window_open=window_open(last_inbound_at, at=now),
                    counted=identity is not None and identity in counted,
                    not_before=not_before,
                    # Their next marketing message, once a day (FREQ-01).
                    cap_until=(
                        last_marketing + self._cap_interval
                        if last_marketing is not None and last_marketing + self._cap_interval > now
                        else None
                    ),
                )
            )
        rate = float(campaign.messages_per_minute)
        if observed:
            rate = await self._observed_rate(campaign, at=now) or rate
        start = (
            campaign.scheduled_at
            if campaign.status is CampaignStatus.SCHEDULED and campaign.scheduled_at is not None
            else now
        )
        return plan(
            PlanInputs(
                recipients=tuple(recipients),
                rate=rate,
                quiet=quiet,
                budget=campaign_budget(limit.messaging_limit, self._share) if limit.known else None,
                counted_at=tuple(counted.values()),
                shared=shared,
                limit_known=limit.known,
                send_until=campaign.send_until,
                start=start,
            ),
            now=now,
        )

    async def eta(self, campaign: Campaign, *, now: datetime) -> Eta:
        """A live campaign's ETA, from the observed rate (PG-13); null with a reason otherwise."""
        reason = STATUS_REASONS.get(campaign.status)
        if reason is not None:
            return Eta(estimated_completion_at=None, basis=(), confidence=None, reason=reason)
        planned = await self.plan(campaign, now=now, observed=True)
        if any(notice.value == "limit_unknown" for notice in planned.notices):
            return Eta(
                estimated_completion_at=None, basis=(), confidence=None, reason="limit_unknown"
            )
        return Eta(
            estimated_completion_at=planned.estimated_completion_at,
            basis=planned.basis,
            confidence=confidence(planned),
            reason=None if planned.estimated_completion_at is not None else "deadline",
        )


__all__ = ["CampaignPlanner", "Eta", "quiet_hours_of"]
