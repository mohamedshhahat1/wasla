"""Rate cards and cost estimates as the API shows them (COST-01, COST-02).

Prices are exact decimals, serialised as strings ("0.0644"), never floats.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel

from app.db.models.rate_card import WhatsAppRateCard
from app.services.cost_estimate import CostEstimate
from app.services.rate_card_service import RateCardView


class RateCardRead(BaseModel):
    id: uuid.UUID
    currency: str
    effective_from: date
    source_filename: str | None
    sha256: str
    uploaded_by_id: uuid.UUID | None
    uploaded_at: datetime
    activated_at: datetime | None

    @classmethod
    def from_model(cls, card: WhatsAppRateCard) -> Self:
        return cls(
            id=card.id,
            currency=card.currency,
            effective_from=card.effective_from,
            source_filename=card.source_filename,
            sha256=card.sha256,
            uploaded_by_id=card.uploaded_by_id,
            uploaded_at=card.uploaded_at,
            activated_at=card.activated_at,
        )


class RateRowRead(BaseModel):
    market: str
    category: str
    rate: Decimal


class RateChangeRead(BaseModel):
    market: str
    category: str
    old: Decimal | None
    new: Decimal | None


class RateCardDetailRead(RateCardRead):
    """A card with its rates and how they differ from the active card (the preview)."""

    rates: list[RateRowRead]
    markets: int
    changes_from_active: list[RateChangeRead]

    @classmethod
    def from_view(cls, view: RateCardView) -> Self:
        base = RateCardRead.from_model(view.card).model_dump()
        return cls(
            **base,
            rates=[
                RateRowRead(market=r.market, category=r.category, rate=r.rate) for r in view.rates
            ],
            markets=view.markets,
            changes_from_active=[
                RateChangeRead(market=c.market, category=c.category, old=c.old, new=c.new)
                for c in view.changes
            ],
        )


class MarketLineRead(BaseModel):
    market: str
    recipients: int
    rate: Decimal
    subtotal: Decimal


class EstimateCardRead(BaseModel):
    id: uuid.UUID
    effective_from: date
    activated_at: datetime | None


class CostEstimateRead(BaseModel):
    """What a campaign may cost at Meta's prices - an upper-bound estimate (COST-02)."""

    label: str = "estimate"
    currency: str | None
    # Null with `reason: no_rate_card` when no card is active: never a guess.
    total: Decimal | None
    per_market: list[MarketLineRead]
    category: str = "marketing"
    recipients_counted: int
    excluded: dict[str, int]
    rate_card: EstimateCardRead | None
    computed_at: datetime
    reason: str | None
    assumptions: list[str]

    @classmethod
    def from_estimate(cls, estimate: CostEstimate) -> Self:
        card = estimate.rate_card
        return cls(
            currency=estimate.currency,
            total=estimate.total,
            per_market=[
                MarketLineRead(
                    market=line.market,
                    recipients=line.recipients,
                    rate=line.rate,
                    subtotal=line.subtotal,
                )
                for line in estimate.per_market
            ],
            recipients_counted=estimate.recipients_counted,
            excluded=estimate.excluded,
            rate_card=(
                EstimateCardRead(
                    id=card.id, effective_from=card.effective_from, activated_at=card.activated_at
                )
                if card is not None
                else None
            ),
            computed_at=estimate.computed_at,
            reason=estimate.reason,
            assumptions=list(estimate.assumptions),
        )


__all__ = [
    "CostEstimateRead",
    "RateCardDetailRead",
    "RateCardRead",
]
