"""Meta's WhatsApp rate cards, as platform staff enter them (COST-01, ADR-150).

Platform-level, not tenant data: a rate card is Meta's price list, the same
for every workspace. Wasla does not charge for messages - Meta bills the
business's WhatsApp Business Account - so a card only ever feeds an
*estimate*. Uploading one changes nothing until it is activated; the active
card is the one activated most recently. 0119.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

MAX_MARKET_LENGTH: Final = 80
#: Rates are stored exactly: Meta publishes up to four decimals; six leave room.
RATE_SCALE: Final = 6


class PricingCategory(StrEnum):
    """Meta's template categories on a rate card. Closed."""

    MARKETING = "marketing"
    UTILITY = "utility"
    AUTHENTICATION = "authentication"
    AUTHENTICATION_INTERNATIONAL = "authentication_international"
    SERVICE = "service"


class WhatsAppRateCard(Base, UUIDPrimaryKeyMixin):
    """One uploaded rate card: its file, currency and when it applies."""

    __tablename__ = "whatsapp_rate_cards"
    __table_args__ = (
        Index("ix_whatsapp_rate_cards_activated_at", "activated_at"),
        CheckConstraint("char_length(currency) = 3", name="currency_code"),
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WhatsAppRateCardRate(Base, UUIDPrimaryKeyMixin):
    """One market's price for one category on one card, as an exact decimal."""

    __tablename__ = "whatsapp_rate_card_rates"
    __table_args__ = (
        UniqueConstraint(
            "rate_card_id", "market", "category", name="uq_whatsapp_rate_card_rates_card_market"
        ),
        CheckConstraint("rate >= 0", name="rate_non_negative"),
        CheckConstraint(
            "category IN (" + ", ".join(f"'{c.value}'" for c in PricingCategory) + ")",
            name="category_known",
        ),
    )

    rate_card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_rate_cards.id", ondelete="CASCADE"),
        nullable=False,
    )
    market: Mapped[str] = mapped_column(String(MAX_MARKET_LENGTH), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, RATE_SCALE), nullable=False)


class WhatsAppRateCardMarket(Base, UUIDPrimaryKeyMixin):
    """Which market a recipient's country calling code is priced in, for one card."""

    __tablename__ = "whatsapp_rate_card_markets"
    __table_args__ = (
        UniqueConstraint(
            "rate_card_id", "calling_code", name="uq_whatsapp_rate_card_markets_card_code"
        ),
        CheckConstraint("calling_code ~ '^[0-9]{1,6}$'", name="calling_code_digits"),
    )

    rate_card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_rate_cards.id", ondelete="CASCADE"),
        nullable=False,
    )
    calling_code: Mapped[str] = mapped_column(String(6), nullable=False)
    market: Mapped[str] = mapped_column(String(MAX_MARKET_LENGTH), nullable=False)


__all__ = [
    "MAX_MARKET_LENGTH",
    "RATE_SCALE",
    "PricingCategory",
    "WhatsAppRateCard",
    "WhatsAppRateCardMarket",
    "WhatsAppRateCardRate",
]
