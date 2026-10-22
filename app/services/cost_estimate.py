"""What a campaign may cost at Meta's prices (COST-02, ADR-150).

**An estimate, and an upper bound.** Meta charges per *delivered* template
message, by category and by the recipient's calling-code market; this counts
every person the campaign could reach as delivered, at the active rate card's
marketing rate for their market. Free entry point windows are not modelled.
**Wasla charges nothing for messages** - Meta bills the business's WhatsApp
Business Account directly - and the answer says so.

**Exact arithmetic.** Rates are stored as decimals and multiplied as decimals;
no float touches a price. **No card, no number**: without an active card the
total is null with the reason `no_rate_card`, never a guessed price.

**Who is counted.** A written audience: its pending and sent people (the
skipped and failed are listed as excluded). Before the audience is written:
the eligible population of the campaign's number under its stored rules - the
same people the audience step would write.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.campaign import Campaign, CampaignRecipient, RecipientStatus
from app.db.models.conversation import Contact, Conversation
from app.db.models.lead import LeadStatus
from app.db.models.rate_card import PricingCategory, WhatsAppRateCard
from app.repositories.campaign_repository import (
    AudienceFilter,
    AudienceRepository,
    CampaignRepository,
)
from app.repositories.consent_repository import refused
from app.services.campaign_service import MAX_AUDIENCE_SIZE
from app.services.pricing_markets import market_for
from app.services.rate_card_service import RateCardService

NO_RATE_CARD: Final = "no_rate_card"
ASSUMPTIONS: Final = (
    "an estimate, not a quote",
    "Meta charges only for delivered messages; this counts everyone as delivered",
    "Meta bills the business's WhatsApp Business Account directly; Wasla does not charge for "
    "messages",
    "priced at the active rate card's marketing rate for each recipient's calling-code market",
    "free entry point windows are not modelled",
)


@dataclass(frozen=True, slots=True)
class MarketLine:
    market: str
    recipients: int
    rate: Decimal
    subtotal: Decimal


@dataclass(frozen=True, slots=True)
class CostEstimate:
    currency: str | None
    total: Decimal | None
    per_market: list[MarketLine]
    recipients_counted: int
    excluded: dict[str, int]
    rate_card: WhatsAppRateCard | None
    computed_at: datetime
    reason: str | None
    assumptions: tuple[str, ...] = field(default=ASSUMPTIONS)


class CampaignCostEstimator:
    """One workspace's campaign estimates against the platform's active card."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._campaigns = CampaignRepository(session, tenant_id=tenant_id)
        self._audience = AudienceRepository(session, tenant_id=tenant_id)
        self._cards = RateCardService(session)

    async def estimate(self, campaign_id: uuid.UUID) -> CostEstimate:
        campaign = await self._campaigns.require_by_id(campaign_id)
        numbers, excluded = (
            await self._written(campaign)
            if campaign.audience_size > 0
            else await self._eligible(campaign)
        )
        now = datetime.now(UTC)
        card = await self._cards.active()
        if card is None:
            return CostEstimate(
                currency=None,
                total=None,
                per_market=[],
                recipients_counted=len(numbers),
                excluded=excluded,
                rate_card=None,
                computed_at=now,
                reason=NO_RATE_CARD,
            )
        mapping = await self._cards.market_map_of(card.id)
        prices = {
            rate.market: rate.rate
            for rate in await self._cards.rates_of(card.id)
            if rate.category == PricingCategory.MARKETING.value
        }
        by_market = Counter(market_for(number, mapping) for number in numbers)
        lines: list[MarketLine] = []
        unpriced = 0
        for market, count in sorted(by_market.items()):
            rate = prices.get(market)
            if rate is None:
                unpriced += count
                continue
            lines.append(MarketLine(market, count, rate, rate * count))
        if unpriced:
            excluded = {**excluded, "unpriced": unpriced}
        return CostEstimate(
            currency=card.currency,
            total=sum((line.subtotal for line in lines), Decimal(0)),
            per_market=lines,
            recipients_counted=sum(line.recipients for line in lines),
            excluded=excluded,
            rate_card=card,
            computed_at=now,
            reason=None,
        )

    async def _written(self, campaign: Campaign) -> tuple[list[str], dict[str, int]]:
        rows = await self._session.execute(
            select(CampaignRecipient.status, CampaignRecipient.reason_code, Contact.wa_id)
            .join(
                Contact,
                (Contact.id == CampaignRecipient.contact_id)
                & (Contact.tenant_id == self._tenant_id),
            )
            .where(
                CampaignRecipient.tenant_id == self._tenant_id,
                CampaignRecipient.campaign_id == campaign.id,
            )
        )
        numbers: list[str] = []
        excluded: Counter[str] = Counter()
        for status, reason, wa_id in rows.all():
            if status in (RecipientStatus.PENDING, RecipientStatus.SENT):
                if wa_id:
                    numbers.append(wa_id)
                else:
                    excluded["no_phone_number"] += 1
            elif reason == "opted_out":
                excluded["opted_out"] += 1
            else:
                excluded[status.value] += 1
        return numbers, dict(excluded)

    async def _eligible(self, campaign: Campaign) -> tuple[list[str], dict[str, int]]:
        rules = campaign.audience or {}
        filters = AudienceFilter(
            last_inbound_within_days=rules.get("last_inbound_within_days"),
            lead_statuses=tuple(LeadStatus(s) for s in rules.get("lead_statuses") or ()),
            contact_ids=tuple(uuid.UUID(c) for c in rules.get("contact_ids") or ()),
            nobody=bool(rules.get("nobody")),
        )
        members = await self._audience.list_eligible_members(
            account_id=campaign.account_id, filters=filters, limit=MAX_AUDIENCE_SIZE
        )
        numbers = [contact.wa_id for contact, _ in members if contact.wa_id]
        excluded: dict[str, int] = {}
        missing = len(members) - len(numbers)
        if missing:
            excluded["no_phone_number"] = missing
        opted_out = int(
            await self._session.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(
                    Conversation.tenant_id == self._tenant_id,
                    Conversation.account_id == campaign.account_id,
                    refused(self._tenant_id, Conversation.contact_id, Conversation.channel),
                )
            )
            or 0
        )
        if opted_out:
            excluded["opted_out"] = opted_out
        return numbers, excluded


__all__ = ["ASSUMPTIONS", "NO_RATE_CARD", "CampaignCostEstimator", "CostEstimate", "MarketLine"]
