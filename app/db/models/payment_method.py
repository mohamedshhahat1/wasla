"""Cards a workspace has saved, as the provider describes them.

**Nothing here is card data.** `provider_token` is an opaque handle the
processor issued: it is not a card number, it is useless outside this merchant
account, and it exists so a renewal can be collected without this application
ever seeing a PAN. `masked_pan` is the last four digits the provider already
prints on a receipt, kept so a customer can tell which of their cards this is.

There is deliberately no column for a card number, an expiry date or a security
code. Those never arrive: the customer types them into the provider's own page,
and what comes back is a token and a description of it. A schema with nowhere
to put them is a stronger guarantee than a rule saying not to.

A separate table rather than columns on `tenants` or `subscriptions`, because a
workspace can reasonably have more than one card, replace one without losing
the history of what paid last month, and keep a card after the subscription it
was added for has ended. `is_default` is the one that renewals use.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.enums import _enum_type

MAX_TOKEN_LENGTH: Final = 200
MAX_MASKED_PAN_LENGTH: Final = 40
MAX_BRAND_LENGTH: Final = 40


class PaymentMethodStatus(StrEnum):
    """Whether this card may still be charged.

    `REVOKED` rather than a delete, because a payment that was collected with a
    card is still a payment collected with *that* card, and the row it points
    at should not vanish. A revoked method is never chosen for a renewal.
    """

    ACTIVE = "active"
    REVOKED = "revoked"


PAYMENT_METHOD_STATUS_TYPE = _enum_type(PaymentMethodStatus, name="payment_method_status")


class PaymentMethod(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One saved card belonging to one workspace."""

    __tablename__ = "payment_methods"
    __table_args__ = (
        # One row per provider token. The provider sends the saved-card
        # notification the same way it sends a payment one - more than once,
        # if it does not get a 2xx - so the insert has to be the claim here
        # too, or a retried notification becomes a second card.
        UniqueConstraint(
            "provider",
            "provider_token",
            name="uq_payment_methods_provider_provider_token",
        ),
        Index("ix_payment_methods_tenant_id", "tenant_id"),
        # Renewals read "this workspace's default card" on every attempt.
        Index("ix_payment_methods_tenant_id_is_default", "tenant_id", "is_default"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # The processor's opaque handle for the card. Not a card number; it cannot
    # be used anywhere but this merchant account.
    provider_token: Mapped[str] = mapped_column(String(MAX_TOKEN_LENGTH), nullable=False)
    # The provider's own id for the token record, which is the number their
    # dashboard and a support conversation use.
    provider_token_id: Mapped[str | None] = mapped_column(
        String(MAX_TOKEN_LENGTH),
        nullable=True,
    )
    # Last four digits as the provider masks them, so a customer can tell one
    # of their cards from another. Never more than this.
    masked_pan: Mapped[str | None] = mapped_column(String(MAX_MASKED_PAN_LENGTH), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(MAX_BRAND_LENGTH), nullable=True)
    status: Mapped[PaymentMethodStatus] = mapped_column(
        PAYMENT_METHOD_STATUS_TYPE,
        nullable=False,
        default=PaymentMethodStatus.ACTIVE,
    )
    # Which card a renewal uses. Not a constraint: enforcing "exactly one
    # default" in the database would make replacing a card a two-statement
    # dance that can fail halfway, and the service clears the old one in the
    # same transaction as it sets the new.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_chargeable(self) -> bool:
        return self.status is PaymentMethodStatus.ACTIVE

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"PaymentMethod(tenant_id={self.tenant_id!r}, masked_pan={self.masked_pan!r})"
