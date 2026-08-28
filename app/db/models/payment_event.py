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
MAX_EVENT_TYPE_LENGTH: Final = 100
MAX_DETAIL_LENGTH: Final = 300


class PaymentEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One callback from a payment provider, and what was done about it.

    The row is written *before* the decision is made - the insert is the claim
    - and the outcome is filled in once there is one. That order is what makes
    two simultaneous deliveries safe; it also means a crash between the two
    leaves a claimed event whose outcome says nothing happened, which is the
    correct thing for it to say.
    """

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
        # Answering "what has this provider been telling us today", which is
        # the query an operator runs when payments stop arriving.
        Index("ix_payment_events_provider_received_at", "provider", "received_at"),
        Index("ix_payment_events_outcome", "outcome"),
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # The provider's identifier for this event, which for a transaction
    # callback pairs the transaction with the state being reported. Not the
    # bare transaction id: one transaction produces several callbacks over its
    # life, and keying on it alone would file every later one as a duplicate of
    # the first. See `CallbackEvent.event_id`.
    provider_event_id: Mapped[str] = mapped_column(
        String(MAX_EVENT_ID_LENGTH),
        nullable=False,
    )
    # The bare transaction id, kept alongside because it is the number the
    # provider's dashboard and a support conversation both use.
    provider_transaction_id: Mapped[str | None] = mapped_column(
        String(MAX_EVENT_ID_LENGTH),
        nullable=True,
    )
    # What the provider reported, from a closed vocabulary the adapter owns -
    # `transaction.succeeded`, `transaction.refunded`. Stored so the ledger can
    # be read without re-deriving meaning from flags that have since changed.
    event_type: Mapped[str] = mapped_column(String(MAX_EVENT_TYPE_LENGTH), nullable=False)
    # CASCADE: the record of a callback about a deleted payment is a record
    # about nothing. Nullable for an event that matched no payment at all.
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=True,
    )
    # What this system decided, in one word - see `checkout_service`'s outcome
    # vocabulary. Deliberately a short category rather than a message: it is
    # read by filtering, and a free-text field becomes a place somebody stores
    # a payload.
    outcome: Mapped[str] = mapped_column(String(MAX_OUTCOME_LENGTH), nullable=False)
    # Why, when the outcome alone does not say. A refusal names the rule it
    # broke; a mismatch names the two figures. Bounded and written only by this
    # application - **no part of the provider's payload is stored here**, which
    # is deliberate: the callback body carries a masked card number, a
    # customer's billing details and a redirect URL containing a bearer token,
    # and none of that is ours to keep.
    detail: Mapped[str | None] = mapped_column(String(MAX_DETAIL_LENGTH), nullable=True)
    # When the callback reached us, as distinct from when it was decided. The
    # two are the same instant today; they stop being when a failed event is
    # retried, and the gap is the number an operator wants.
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"PaymentEvent(provider={self.provider!r}, outcome={self.outcome!r})"
