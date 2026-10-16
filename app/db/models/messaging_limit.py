"""Meta's messaging limit, per business portfolio (LIM-01, LIM-02, ADR-146).

Meta allows a business to reach a number of unique WhatsApp users outside a
customer service window in a moving 24 hours, and that number belongs to the
**business portfolio**, shared by every phone number in it ("Messaging
limits", developers.facebook.com, read 2026-10-16; changelog 2025-10-08:
"Messaging limits are now business portfolio-based instead of business phone
number-based").

**Platform-level, not tenant data.** A portfolio is Meta's object and may hold
numbers of two workspaces; the limit it carries binds them together. It holds
no customer data - a key, a tier and when it was read - and no request reads
it except through a connection of the caller's own workspace (LIM-07).

**Keyed on the WhatsApp Business Account.** Meta documents no field on the
WABA naming its owning portfolio (WhatsApp Business Account reference, read
2026-10-16), so `meta_key` is the WABA id and `key_kind` says so (LIM-02's
fallback). Two numbers of one WABA share one limit, as they must; two WABAs of
one portfolio would be counted apart - an EXTERNAL VERIFICATION item, recorded
in docs/WHATSAPP.md.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

MAX_TIER_LENGTH: Final = 40
MAX_META_KEY_LENGTH: Final = 64


class PortfolioKeyKind(StrEnum):
    """What `meta_key` names."""

    #: A WhatsApp Business Account id: Meta names no portfolio field (LIM-02).
    WABA = "waba"


class LimitSource(StrEnum):
    """Where the stored limit came from (LIM-01)."""

    #: A read of `whatsapp_business_manager_messaging_limit`.
    API = "api"
    #: A `business_capability_update` webhook.
    WEBHOOK = "webhook"


class WhatsAppBusinessPortfolio(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One business portfolio's messaging limit, as Meta last stated it.

    Never read: `checked_at` is null and the limit is unknown - a campaign on
    one of its numbers is neither scheduled nor sent (LIM-01). Read:
    `messaging_limit` is the number of unique people, or null for unlimited.
    `recognised` is false when Meta's value was one Wasla does not know, in
    which case the limit is the lowest level, never guessed upward.
    """

    __tablename__ = "whatsapp_business_portfolios"
    __table_args__ = (
        UniqueConstraint("key_kind", "meta_key", name="uq_whatsapp_business_portfolios_key"),
        CheckConstraint(
            "key_kind IN (" + ", ".join(f"'{kind.value}'" for kind in PortfolioKeyKind) + ")",
            name="key_kind_known",
        ),
        CheckConstraint(
            "source IS NULL OR source IN ("
            + ", ".join(f"'{source.value}'" for source in LimitSource)
            + ")",
            name="source_known",
        ),
        CheckConstraint("messaging_limit IS NULL OR messaging_limit > 0", name="limit_positive"),
        # Known means read: a tier, a source and a time, together.
        CheckConstraint(
            "(checked_at IS NULL AND tier IS NULL AND source IS NULL AND messaging_limit IS NULL)"
            " OR (checked_at IS NOT NULL AND tier IS NOT NULL AND source IS NOT NULL)",
            name="read_complete",
        ),
    )

    key_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    meta_key: Mapped[str] = mapped_column(String(MAX_META_KEY_LENGTH), nullable=False)
    #: Meta's own value, as it was given (`TIER_250`, `TIER_2K`, or an integer
    #: rendered as text), bounded.
    tier: Mapped[str | None] = mapped_column(String(MAX_TIER_LENGTH), nullable=True)
    #: Unique people per moving 24 hours; null for unlimited, or never read.
    messaging_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: False when `tier` was a value Wasla does not know (treated as the
    #: lowest level, counted and alerted).
    recognised: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: When Wasla last learned the limit, by a read or a webhook.
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the limit last changed value.
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def known(self) -> bool:
        return self.checked_at is not None

    @property
    def unlimited(self) -> bool:
        return self.known and self.messaging_limit is None


__all__ = [
    "MAX_META_KEY_LENGTH",
    "MAX_TIER_LENGTH",
    "LimitSource",
    "PortfolioKeyKind",
    "WhatsAppBusinessPortfolio",
]
