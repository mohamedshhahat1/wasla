"""Provider payment callbacks, recorded once each.

The whole reason this table exists is the unique constraint on it. Every
payment processor retries a callback it did not get a 2xx for, and a retry that
is processed a second time settles an invoice twice, extends a billing period
twice, and records money that arrived once as money that arrived twice. An
application-level "have I seen this?" check does not close that: two retries
arriving together both read no row and both proceed, which is exactly the shape
a provider's retry storm produces.

So `UNIQUE(provider, provider_event_id)` is the mechanism, and the insert is
the claim. A worker or a request that inserts successfully owns the event; one
that hits the constraint knows another already does, and stops. There is no
window between the check and the write because there is no check.

**The event id is the provider's, not ours.** For Paymob it is the transaction
id (`obj.id`), which is stable across retries of the same notification and
different for a later refund of the same payment - so a refund is a new event
rather than a duplicate of the payment it reverses. Inventing an id, or hashing
the body, would both be wrong: a body that differs by a whitespace character
would hash differently and be processed again.

`payment_id` is nullable because an authenticated callback naming a payment
this system does not have is still an event worth recording. It is refused, and
the record of the refusal is what an operator reads when a customer says they
paid and nothing happened.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

MAX_EVENT_ID_LENGTH: Final = 200
MAX_OUTCOME_LENGTH: Final = 50


class PaymentEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One callback from a payment provider, and what was done about it."""

    __tablename__ = "payment_events"
    __table_args__ = (
        # The idempotency guarantee. Not an index for speed - though it is that
        # too - but the thing that makes duplicate processing impossible rather
        # than unlikely.
        UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_payment_events_provider_provider_event_id",
        ),
        Index("ix_payment_events_payment_id", "payment_id"),
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(
        String(MAX_EVENT_ID_LENGTH),
        nullable=False,
    )
    # CASCADE: the record of a callback about a deleted payment is a record
    # about nothing. Nullable for an event that matched no payment at all.
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=True,
    )
    # What this system decided, in one word: applied, duplicate, unmatched,
    # mismatched. Deliberately a short category rather than a message - it is
    # read by filtering, and a free-text field becomes a place somebody stores
    # a payload.
    outcome: Mapped[str] = mapped_column(String(MAX_OUTCOME_LENGTH), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"PaymentEvent(provider={self.provider!r}, outcome={self.outcome!r})"
