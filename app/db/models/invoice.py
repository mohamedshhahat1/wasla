"""Invoices and payments: what was owed for a period, and what was paid.

An invoice is a **record of a past period**, not a live calculation. Once issued
it stops moving: the plan can change, a price can be edited, usage can keep
accruing, and last month's invoice still says what last month said. That is the
entire reason this table exists rather than a function that adds things up on
demand — a figure recomputed from today's configuration cannot answer "why was I
charged this in March", which is the only question anybody ever asks about an
invoice.

So the amounts are copied, not referenced. `plan_code` and the line amounts are
written onto the row at issue time; nothing here joins back to `plans` to render
a total.

A payment is an attempt, not a state. Attempts fail and are retried, and each
one is a row: collapsing them into a single status on the invoice would lose the
history a dispute turns on, which is exactly the history a chargeback needs.

Nothing here moves money. `provider` and `provider_reference` are where a real
payment processor will identify its own objects; until then invoices are issued
and marked paid by the platform, which is what makes local development and every
test in this suite possible without credentials (ADR-031).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.billing import CURRENCY_LENGTH, DEFAULT_CURRENCY
from app.db.models.enums import _enum_type

MAX_REFERENCE_LENGTH: Final = 200
MAX_DESCRIPTION_LENGTH: Final = 300
# Long enough for a provider's decline text without becoming a place somebody
# stores a stack trace.
MAX_FAILURE_LENGTH: Final = 500
# Bounded because it is caller-supplied and indexed. Generous enough for a
# UUID, a ULID or a request id, small enough that it is not a place to put a
# payload.
MAX_IDEMPOTENCY_KEY_LENGTH: Final = 100


class InvoiceStatus(StrEnum):
    """Where an invoice stands.

    `DRAFT` exists so an invoice can be assembled and checked before anybody is
    asked for money. `VOID` is how a mistake is undone: an issued invoice is
    never deleted and never edited, because the customer has seen it.
    """

    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    UNCOLLECTIBLE = "uncollectible"
    VOID = "void"


class PaymentStatus(StrEnum):
    """What happened to one attempt at collecting."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class CollectionState(StrEnum):
    """How far an *automatic* collection attempt got, which is not its status.

    `PaymentStatus` answers "did money move", and for a renewal being debited
    from a saved card that question has one answer for a long time: nobody
    knows, because a provider decides it and says so on a callback. This
    column answers the different question the collection protocol has to ask
    before it may act - **may another charge be sent for this invoice** - and
    the two are not the same. A payment can sit in `PENDING` for either of two
    reasons that must never be confused: nothing has been sent yet, or
    something has been sent and the answer is missing.

    NULL for every payment that is not an automatic collection attempt. A
    hosted checkout is somebody at a payment page, and nothing here applies to
    it (ADR-088).

    ``CLAIMED``
        The attempt is durable and the provider has not been asked. Money
        cannot have moved, because the move to `REQUESTED` commits before the
        request is built. Safe to abandon and give the attempt back.

    ``REQUESTED``
        The provider was asked, or may have been. **Money may have moved.**
        The only things that may resolve this are a signed callback and a
        lookup by reference; nothing may send a second charge while an invoice
        has one of these, which is enforced by a partial unique index rather
        than by remembering to check.

    ``SETTLED``
        The outcome is known and recorded on the payment. Terminal.

    ``ABANDONED``
        The provider was shown to have never received the request, so this
        attempt moved no money and closes without one. Terminal, and the only
        state that returns an attempt to the budget.
    """

    CLAIMED = "claimed"
    REQUESTED = "requested"
    SETTLED = "settled"
    ABANDONED = "abandoned"


# The states in which nobody knows what an attempt did, and therefore the ones
# that forbid another charge against the same invoice. Named once because the
# claim query, the partial unique index and the reconciler all mean this set
# and must not drift apart.
UNRESOLVED_COLLECTION_STATES: Final[frozenset[CollectionState]] = frozenset(
    {CollectionState.CLAIMED, CollectionState.REQUESTED}
)

# The same set as a SQL fragment, for the partial indexes that enforce it.
_UNRESOLVED_SQL: Final = "collection_state IN ('claimed', 'requested')"


# Statuses in which an invoice is finished and will not change again.
TERMINAL_INVOICE_STATUSES: Final[frozenset[InvoiceStatus]] = frozenset(
    {
        InvoiceStatus.PAID,
        InvoiceStatus.UNCOLLECTIBLE,
        InvoiceStatus.VOID,
    }
)

INVOICE_STATUS_TYPE = _enum_type(InvoiceStatus, name="invoice_status")
PAYMENT_STATUS_TYPE = _enum_type(PaymentStatus, name="payment_status")
COLLECTION_STATE_TYPE = _enum_type(CollectionState, name="payment_collection_state")


# Where one payment attempt may go from where it is. Written down because the
# statuses arrive from *outside*: a provider decides what a payment did, and a
# callback is a stranger's assertion until it has been checked against what we
# already believe. Without this, a forged - or merely late, or merely
# out-of-order - callback saying `succeeded` about a payment we have already
# refunded would settle the invoice a second time.
#
# A status is absent from its own set on purpose. Restating what a payment
# already says is neither legal nor illegal; it is nothing, and the caller
# distinguishes it so the ledger can record that nothing happened rather than
# recording a change that did not occur.
PAYMENT_TRANSITIONS: Final[dict[PaymentStatus, frozenset[PaymentStatus]]] = {
    # In flight. It may still land either way.
    PaymentStatus.PENDING: frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.FAILED}),
    # Collected. The only thing that can happen to money we hold is giving it
    # back.
    PaymentStatus.SUCCEEDED: frozenset({PaymentStatus.REFUNDED}),
    # A declined attempt is finished. A customer trying again produces another
    # attempt and another row, which is what makes the history readable; a
    # failed row that later says `succeeded` would erase the decline.
    PaymentStatus.FAILED: frozenset(),
    # Given back. Nothing follows, and `refunded -> succeeded` in particular
    # must never happen: it is how a refunded customer keeps the product.
    PaymentStatus.REFUNDED: frozenset(),
}


# The same for invoices, which move for our own reasons rather than a
# provider's - except one.
#
# `PAID -> OPEN` is that one, and it is only reachable by refunding. It looks
# wrong and is not: `amount_paid` records money we *hold*, so giving it back
# means an invoice whose payments no longer cover it, and an invoice that is
# not covered is not paid. An operator refunding because a customer is leaving
# voids the invoice afterwards, which is a separate deliberate act rather than
# something inferred from a reversal.
INVOICE_TRANSITIONS: Final[dict[InvoiceStatus, frozenset[InvoiceStatus]]] = {
    InvoiceStatus.DRAFT: frozenset({InvoiceStatus.OPEN, InvoiceStatus.VOID}),
    InvoiceStatus.OPEN: frozenset(
        {InvoiceStatus.PAID, InvoiceStatus.UNCOLLECTIBLE, InvoiceStatus.VOID}
    ),
    InvoiceStatus.PAID: frozenset({InvoiceStatus.OPEN}),
    InvoiceStatus.UNCOLLECTIBLE: frozenset({InvoiceStatus.PAID, InvoiceStatus.VOID}),
    # Withdrawn. A bill the customer was told to ignore does not come back.
    InvoiceStatus.VOID: frozenset(),
}


def payment_may_move(current: PaymentStatus, target: PaymentStatus) -> bool:
    """Whether a payment may go from `current` to `target`.

    False for a move to the status it already holds: that is not a move. See
    `PAYMENT_TRANSITIONS`.
    """
    return target in PAYMENT_TRANSITIONS[current]


def invoice_may_move(current: InvoiceStatus, target: InvoiceStatus) -> bool:
    """Whether an invoice may go from `current` to `target`."""
    return target in INVOICE_TRANSITIONS[current]


class Invoice(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """What one workspace owed for one period."""

    __tablename__ = "invoices"
    __table_args__ = (
        # One invoice per workspace per period. A sweep that runs twice, or two
        # replicas sweeping at once, must not bill a customer twice for March -
        # and that is a constraint's job rather than a check in a service.
        UniqueConstraint(
            "tenant_id",
            "period_start",
            name="uq_invoices_tenant_id_period_start",
        ),
        Index("ix_invoices_tenant_id", "tenant_id"),
        Index("ix_invoices_tenant_id_status", "tenant_id", "status"),
        Index("ix_invoices_status", "status"),
        Index("ix_invoices_subscription_id", "subscription_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # SET NULL: an invoice outlives the subscription it came from. A
        # customer who left last year can still be shown what they paid.
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[InvoiceStatus] = mapped_column(INVOICE_STATUS_TYPE, nullable=False)
    # Copied from the plan at issue time, never joined for. A plan renamed or
    # repriced afterwards must not change what March says.
    plan_code: Mapped[str] = mapped_column(String(50), nullable=False)
    amount_due: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The lines as they were, including the usage figures behind them. JSONB
    # rather than a child table because nothing queries inside a line: an
    # invoice is read whole, by one customer, to answer one question.
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(
        String(MAX_REFERENCE_LENGTH),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How many times the platform has tried to debit a card for this invoice.
    # On the invoice rather than the subscription because it counts attempts at
    # collecting *this* bill: a customer who fixes their card next month starts
    # from zero on next month's invoice, which is what anybody would expect.
    collection_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # When the next automatic attempt becomes due. NULL means "not scheduled",
    # which is the state for an invoice nobody is chasing and for one whose
    # attempts have run out.
    next_collection_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_INVOICE_STATUSES

    @property
    def outstanding(self) -> Decimal:
        """What is still owed. Never negative: an overpayment is a credit, and
        a credit is a decision this system does not make yet."""
        remaining = self.amount_due - self.amount_paid
        return remaining if remaining > 0 else Decimal("0.00")

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Invoice(tenant_id={self.tenant_id!r}, status={self.status!r})"


class Payment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One attempt at collecting an invoice.

    Attempts are rows rather than a status, because a failed one is not
    forgotten when a later one succeeds: the history is what a dispute, a
    chargeback and an angry email all turn on.
    """

    __tablename__ = "payments"
    __table_args__ = (
        # A provider's own idempotency key. Two webhooks describing the same
        # charge must not become two payments, and a retried request must not
        # collect twice.
        UniqueConstraint(
            "provider",
            "provider_reference",
            name="uq_payments_provider_provider_reference",
        ),
        # A retried checkout request must not become a second payment page.
        # Scoped to the workspace because the key comes from that workspace's
        # client: two customers picking the same string is their business, and
        # a global constraint would let either of them deny the other a
        # checkout by guessing.
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_payments_tenant_id_idempotency_key",
        ),
        # **One invoice, at most one unresolved automatic attempt.** The
        # constraint that closes WSL-01, and the reason it is an index rather
        # than a check in a service: a second charge must be impossible while
        # nobody knows what the first one did, and "impossible" is a property
        # of the table. `SKIP LOCKED` cannot supply it - a lock belongs to a
        # process, and the process this protects against is one that has
        # stopped existing (ADR-088).
        #
        # Partial, so it constrains nothing once an attempt is settled or
        # abandoned: an invoice may be tried three times, one at a time.
        Index(
            "uq_payments_unresolved_collection",
            "invoice_id",
            unique=True,
            postgresql_where=text(_UNRESOLVED_SQL),
        ),
        # Reconciliation's only query: the oldest attempt nobody has resolved.
        # Partial for the same reason retention's is - on a healthy deployment
        # this index is empty, and a full one would be paid for on every
        # payment written.
        Index(
            "ix_payments_unresolved_collection",
            "created_at",
            postgresql_where=text(_UNRESOLVED_SQL),
        ),
        # `collection_state` belongs to the automatic path and to nothing else.
        # Stated as a constraint because the alternative is every reader
        # deciding for itself whether a NULL means "not automatic" or "an
        # automatic attempt written before this column existed".
        CheckConstraint(
            "(collection_state IS NULL) = (is_automatic IS FALSE)",
            name="collection_state",
        ),
        Index("ix_payments_tenant_id", "tenant_id"),
        Index("ix_payments_invoice_id", "invoice_id"),
        Index("ix_payments_status", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[PaymentStatus] = mapped_column(PAYMENT_STATUS_TYPE, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(CURRENCY_LENGTH),
        nullable=False,
        default=DEFAULT_CURRENCY,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(
        String(MAX_REFERENCE_LENGTH),
        nullable=True,
    )
    # The provider's id for the *intended* payment, written when a hosted
    # checkout is created. Distinct from `provider_reference`, which is the id
    # of the transaction that eventually settled it and does not exist yet at
    # the moment a customer is sent to a payment page. Kept so support can find
    # an abandoned checkout in the provider's dashboard - the commonest real
    # question being "I started paying and nothing happened".
    provider_intent_reference: Mapped[str | None] = mapped_column(
        String(MAX_REFERENCE_LENGTH),
        nullable=True,
    )
    # What the provider said when it refused. Kept because "declined" alone
    # tells a customer nothing they can act on.
    failure_reason: Mapped[str | None] = mapped_column(String(MAX_FAILURE_LENGTH), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How much of this payment has been given back. A column rather than a
    # boolean because a processor may reverse a payment in parts, and a
    # workspace asking "what did I actually pay" needs the figure rather than
    # the fact.
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0.00"),
    )
    # When somebody asked the provider to reverse this, as distinct from when
    # the provider confirmed it. The gap between the two is the state worth
    # being able to find: a refund requested days ago and never confirmed
    # usually means the callback URL is wrong, and a customer is waiting.
    refund_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The provider's id for the *reversal*, which is a different transaction
    # from the one being reversed. Kept so the callback reporting the reversal
    # can be tied back to the request that caused it.
    refund_reference: Mapped[str | None] = mapped_column(
        String(MAX_REFERENCE_LENGTH),
        nullable=True,
    )
    # A caller's own key for the request that created this attempt, so a
    # retried request is recognised rather than becoming a second payment
    # page. Nullable: most callers do not send one, and NULLs are distinct
    # under the unique constraint, so any number of attempts without a key
    # coexist.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(MAX_IDEMPOTENCY_KEY_LENGTH),
        nullable=True,
    )
    # Whether a person was at a payment page for this attempt, or the platform
    # debited a card on file. Recorded because the two are different events to
    # a customer and to a card scheme: one they did, one happened to them, and
    # a dispute turns on which.
    is_automatic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The card this attempt used, when it was taken automatically. SET NULL
    # rather than CASCADE: a payment outlives the card that made it, and the
    # record of what was collected must not disappear when somebody removes a
    # card from their account.
    payment_method_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payment_methods.id", ondelete="SET NULL"),
        nullable=True,
    )
    # How far the automatic collection protocol got, which is a different
    # question from `status` - see `CollectionState`. NULL for a hosted
    # checkout, where somebody was at a payment page and none of this applies.
    collection_state: Mapped[CollectionState | None] = mapped_column(
        COLLECTION_STATE_TYPE,
        nullable=True,
    )
    # When the provider was last asked what became of this attempt. Written
    # before the lookup rather than after it, which makes it the lease as well
    # as the record: a second reconciler skips a row somebody is already
    # asking about, and a reconciler that dies mid-lookup leaves a row that
    # becomes claimable again once the lease is older than the interval,
    # without a reaper existing to notice.
    reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    @property
    def is_refundable(self) -> bool:
        """Whether there is money here that could be given back.

        Collected, and not already returned. Deliberately a property on the
        row: the question is asked by the service, by the API and by the tests,
        and three copies of `status is SUCCEEDED and ...` is how they come to
        disagree.
        """
        return self.status is PaymentStatus.SUCCEEDED and self.refunded_amount < self.amount

    @property
    def is_unresolved_collection(self) -> bool:
        """Whether this attempt is one nobody yet knows the outcome of.

        The question the collection path asks before charging and the
        reconciler asks before looking. A property on the row for the reason
        `is_refundable` is one: three copies of the same set membership is how
        they come to disagree, and disagreeing about this one means charging a
        card twice.
        """
        return self.collection_state in UNRESOLVED_COLLECTION_STATES

    def __repr__(self) -> str:  # pragma: no cover - diagnostic helper
        return f"Payment(invoice_id={self.invoice_id!r}, status={self.status!r})"
