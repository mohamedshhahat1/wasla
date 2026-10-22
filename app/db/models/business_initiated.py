"""The ledger of business-initiated sends, per business portfolio (LIM-03, ADR-146).

Meta's messaging limit counts "unique WhatsApp user phone numbers your business
can deliver messages to, outside of a customer service window, within a moving
24-hour period", per business portfolio ("Messaging limits", read 2026-10-16).
This is Wasla's own count of exactly that: one row per template message sent
to a person whose service window on that number was closed, whatever path sent
it - a campaign, a test send, a person's manual template, a follow-up.

**Reserved before the send, under the portfolio's lock.** The row is written
in the transaction that commits the send intent, after the distinct people of
the last 24 hours were counted under `pg_advisory_xact_lock` on the portfolio
(the CMP-03 reservation's pattern), and the lock ends at that commit - before
Meta is asked (ADR-093). A send Meta provably did not accept deletes its row;
one whose answer is uncertain keeps it, because counting a person who may not
have been reached is the safe error.

**Platform-level, not tenant data.** The count binds every workspace whose
numbers share a portfolio, so the ledger is read across workspaces by the
reservation and by nothing a request can reach: a workspace sees only the
aggregate for its own number (LIM-07). `tenant_id` is kept so a workspace's
rows go with the workspace. Rows older than 48 hours are deleted by the
retention sweep: the reservation never looks further back than 24.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

#: Wide enough for any identity value Meta counts by.
MAX_RECIPIENT_LENGTH: Final = 255


class LedgerSource(StrEnum):
    """Which path sent a business-initiated message (LIM-03). Closed."""

    CAMPAIGN = "campaign"
    CAMPAIGN_TEST = "campaign_test"
    MANUAL_TEMPLATE = "manual_template"
    FOLLOW_UP = "follow_up"
    OTHER = "other"


class BusinessInitiatedSend(Base, UUIDPrimaryKeyMixin):
    """One template message to a person outside their service window, on one portfolio."""

    __tablename__ = "business_initiated_sends"
    __table_args__ = (
        # The reservation's count: distinct people of one portfolio in a window.
        Index(
            "ix_business_initiated_sends_portfolio_id_sent_at",
            "portfolio_id",
            "sent_at",
        ),
        # Whether one person is already counted in the window.
        Index(
            "ix_business_initiated_sends_portfolio_id_recipient_sent_at",
            "portfolio_id",
            "recipient",
            "sent_at",
        ),
        # Releasing the row of a send Meta provably refused.
        Index(
            "ix_business_initiated_sends_message_id",
            "message_id",
            postgresql_where=text("message_id IS NOT NULL"),
        ),
        Index("ix_business_initiated_sends_tenant_id", "tenant_id"),
        # Retention: rows older than 48 hours.
        Index("ix_business_initiated_sends_sent_at", "sent_at"),
        CheckConstraint(
            "source IN (" + ", ".join(f"'{source.value}'" for source in LedgerSource) + ")",
            name="source_known",
        ),
        CheckConstraint(
            "limit_at_send IS NULL OR limit_at_send > 0", name="limit_at_send_positive"
        ),
        CheckConstraint(
            "budget_at_send IS NULL OR budget_at_send >= 0", name="budget_at_send_non_negative"
        ),
    )

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # Named explicitly: the convention's name is past PostgreSQL's 63.
        ForeignKey(
            "whatsapp_business_portfolios.id",
            ondelete="CASCADE",
            name="fk_business_initiated_sends_portfolio_id",
        ),
        nullable=False,
    )
    #: Who Meta counts: the identity the message was addressed to.
    recipient: Mapped[str] = mapped_column(String(MAX_RECIPIENT_LENGTH), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    #: The portfolio's limit when this was sent; null for unlimited.
    limit_at_send: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The campaign share of it when this was sent (`floor(share x limit)`),
    #: for a campaign's row; null otherwise or for unlimited (CR-12).
    budget_at_send: Mapped[int | None] = mapped_column(Integer, nullable=True)


__all__ = ["MAX_RECIPIENT_LENGTH", "BusinessInitiatedSend", "LedgerSource"]
