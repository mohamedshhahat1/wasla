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
    DDL,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, RevisionedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.billing import (
    BILLING_INTERVAL_TYPE,
    CURRENCY_CHECK_SQL,
    CURRENCY_LENGTH,
    CUSTOM_PLAN_SCOPE_FUNCTION_SQL,
    DEFAULT_CURRENCY,
    BillingInterval,
)
from app.db.models.custom_plan_offer import INVOICE_OFFER_FUNCTION_SQL, INVOICE_OFFER_TRIGGER_SQL
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


class InvoicePurpose(StrEnum):
    """Why an invoice exists, which decides who may collect it and how (BILL-02).

    ``CHECKOUT``
        A customer chose to buy something and was sent to a payment page. Only
        that customer, at that page, pays it. **Never collected automatically**:
        a checkout somebody abandoned is not consent to be charged, and the
        renewal sweep debiting one from a saved card was an unauthorised
        merchant-initiated charge.
    ``RENEWAL``
        The billing sweep's bill for the next service period of a subscription,
        issued in advance. The only purpose automatic collection may claim.
    ``MANUAL``
        Raised by platform staff for money that arrives outside the product -
        a bank transfer against an operator plan change.
    ``ADJUSTMENT``
        Reserved for a future credit or correction document. Nothing creates
        one today; it is in the vocabulary so the day something does, it cannot
        be mistaken for any of the three above.
    ``TOPUP``
        A one-time purchase of extra allowance (ADR-113). Customer-initiated,
        paid at a hosted page, **never** recurring and never collected from a
        saved card - a trigger on `payments` refuses an automatic attempt
        against one. It buys no plan: settlement grants its `TopupPurchase`
        and leaves the subscription exactly as it was, and no reversal of it
        ever withdraws a plan.
    """

    CHECKOUT = "checkout"
    RENEWAL = "renewal"
    MANUAL = "manual"
    ADJUSTMENT = "adjustment"
    TOPUP = "topup"


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
INVOICE_PURPOSE_TYPE = _enum_type(InvoicePurpose, name="invoice_purpose")
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


class Invoice(Base, UUIDPrimaryKeyMixin, TimestampMixin, RevisionedMixin):
    """What one workspace owed for one period."""

    __tablename__ = "invoices"
    __table_args__ = (
        # One *renewal* per workspace per period. A sweep that runs twice, or
        # two replicas sweeping at once, must not bill a customer twice for
        # March - and that is a constraint's job rather than a check in a
        # service. Partial since 0071: a checkout is a purchase with its own
        # period, fixed at settlement, and two purchases are two invoices; the
        # constraint forcing them to share one is what let a second checkout
        # re-price the first (BILL-06).
        Index(
            "uq_invoices_renewal_tenant_id_period_start",
            "tenant_id",
            "period_start",
            unique=True,
            postgresql_where=text("purpose = 'renewal'"),
        ),
        Index("ix_invoices_tenant_id", "tenant_id"),
        Index("ix_invoices_tenant_id_status", "tenant_id", "status"),
        Index("ix_invoices_status", "status"),
        Index("ix_invoices_subscription_id", "subscription_id"),
        Index("ix_invoices_purpose_status", "purpose", "status"),
        # Defence in depth beneath every settlement path (BILL-12, BILL-20):
        # the database used to accept a negative amount and an overpaid
        # invoice, and a paid invoice holding more than it was for is a refund
        # nobody has noticed they owe.
        CheckConstraint("amount_due >= 0", name="amount_due_non_negative"),
        CheckConstraint("amount_paid >= 0", name="amount_paid_non_negative"),
        CheckConstraint("amount_paid <= amount_due", name="amount_paid_within_due"),
        CheckConstraint(CURRENCY_CHECK_SQL, name="currency_supported"),
        # A paid invoice says when (DB-005).
        CheckConstraint("status <> 'paid' OR paid_at IS NOT NULL", name="paid_is_dated"),
        # An invoice's period does not run backwards (DB-012). Equal is
        # allowed: a zero-length period is a real, if degenerate, record.
        CheckConstraint("period_end >= period_start", name="period_ordered"),
        # What a payment, a top-up, an incident or an adjustment names, so
        # each can name only its own workspace's invoice (DB-004, ADR-100).
        UniqueConstraint("tenant_id", "id", name="uq_invoices_tenant_id_id"),
        # An invoice's subscription is its own workspace's (DB-004).
        ForeignKeyConstraint(
            ["tenant_id", "subscription_id"],
            ["subscriptions.tenant_id", "subscriptions.id"],
            name="fk_invoices_tenant_subscription",
            ondelete="SET NULL (subscription_id)",
        ),
        # An invoice for a custom plan offer names an offer of its own
        # workspace, and only a customer's checkout does (ADR-114).
        ForeignKeyConstraint(
            ["custom_plan_offer_id", "tenant_id"],
            ["custom_plan_offers.id", "custom_plan_offers.tenant_id"],
            name="fk_invoices_custom_plan_offer_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "custom_plan_offer_id IS NULL OR "
            "(purpose = 'checkout' AND plan_version_id IS NOT NULL)",
            name="offer_is_a_checkout",
        ),
        Index(
            "ix_invoices_custom_plan_offer_id",
            "custom_plan_offer_id",
            postgresql_where=text("custom_plan_offer_id IS NOT NULL"),
        ),
        # The price an invoice charges is a price of the version it names
        # (ADR-116), and its billing term is copied beside it - all three or
        # none, so a historical invoice reads the same after the price retires.
        ForeignKeyConstraint(
            ["plan_version_id", "plan_price_id"],
            ["plan_prices.plan_version_id", "plan_prices.id"],
            name="fk_invoices_plan_price_of_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "plan_price_id IS NULL OR plan_version_id IS NOT NULL", name="price_pinned"
        ),
        CheckConstraint(
            "(plan_price_id IS NULL) = (billing_interval IS NULL)"
            " AND (plan_price_id IS NULL) = (interval_count IS NULL)",
            name="price_snapshot_complete",
        ),
        Index(
            "ix_invoices_plan_price_id",
            "plan_price_id",
            postgresql_where=text("plan_price_id IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT, not CASCADE (BILL-19). An invoice is financial history, and
        # deleting a tenant row - by a mistaken statement, a restore script, a
        # future purge - must be refused rather than silently take the ledger
        # with it. Retention already never deletes the tenant row; this makes
        # that a property of the schema rather than of one code path.
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # SET NULL (the column only): an invoice outlives the subscription it came
    # from. A customer who left last year can still be shown what they paid.
    # The key is composite with `tenant_id` - see `__table_args__`.
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    status: Mapped[InvoiceStatus] = mapped_column(INVOICE_STATUS_TYPE, nullable=False)
    # Who may collect this and how - see `InvoicePurpose`. The automatic
    # collection sweep claims `RENEWAL` and nothing else (BILL-02).
    purpose: Mapped[InvoicePurpose] = mapped_column(INVOICE_PURPOSE_TYPE, nullable=False)
    # Copied from the plan at issue time, never joined for. A plan renamed or
    # repriced afterwards must not change what March says.
    plan_code: Mapped[str] = mapped_column(String(50), nullable=False)
    # The immutable terms this invoice charges for (BILL-06, BILL-12). Written
    # when the invoice is created and never changed: a second checkout for
    # another plan opens its own invoice rather than re-pricing this one, so a
    # payment can only ever buy what its own invoice says. NULL on an invoice
    # issued before 0071, whose meaning is carried by `plan_code`, `lines` and
    # `amount_due` - no historical version is invented for it.
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # The exact price this invoice charges, and the billing term it covers,
    # copied at issue (ADR-116): `2,990 EGP per 1 yearly` is one invoice for a
    # year, never twelve. NULL on a free renewal, a top-up and an invoice issued
    # before 0071. A trigger requires every priced purchase and renewal to name
    # one, and the amount and currency to be exactly that price's.
    plan_price_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    billing_interval: Mapped[BillingInterval | None] = mapped_column(
        BILLING_INTERVAL_TYPE, nullable=True
    )
    interval_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The custom plan offer this checkout accepts (ADR-114). Settlement reads
    # it to activate the offer, and refuses the money if the offer was
    # declined or withdrawn meanwhile. Fixed at creation by a trigger.
    custom_plan_offer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
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


class Payment(Base, UUIDPrimaryKeyMixin, TimestampMixin, RevisionedMixin):
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
        # What a TOKEN callback and the Paymob order binding look payments up
        # by (BILL-04, BILL-11).
        Index("ix_payments_provider_provider_order_id", "provider", "provider_order_id"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("refunded_amount >= 0", name="refunded_amount_non_negative"),
        CheckConstraint("refunded_amount <= amount", name="refunded_within_amount"),
        CheckConstraint(CURRENCY_CHECK_SQL, name="currency_supported"),
        # Only money that was collected can have been applied to an invoice.
        CheckConstraint(
            "applied_at IS NULL OR status IN ('succeeded', 'refunded')",
            name="applied_only_when_collected",
        ),
        # Collected money says when it was processed (DB-005).
        CheckConstraint(
            "status NOT IN ('succeeded', 'refunded') OR processed_at IS NOT NULL",
            name="collected_is_processed",
        ),
        # A payment collects its own workspace's invoice, on its own
        # workspace's card (DB-004): the audit wrote one against another
        # workspace's invoice, and an automatic one on another workspace's
        # saved card, and PostgreSQL accepted both.
        UniqueConstraint("tenant_id", "id", name="uq_payments_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"],
            ["invoices.tenant_id", "invoices.id"],
            name="fk_payments_tenant_invoice",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "payment_method_id"],
            ["payment_methods.tenant_id", "payment_methods.id"],
            name="fk_payments_tenant_payment_method",
            ondelete="SET NULL (payment_method_id)",
        ),
        # An operator's reference for money that arrived outside a processor
        # is unique within one workspace and method, not across the platform:
        # two workspaces' bank transfers may well share "BT-1" (DB-017). The
        # processor's own transaction ids keep their global uniqueness above.
        Index(
            "uq_payments_tenant_id_provider_manual_reference",
            "tenant_id",
            "provider",
            "manual_reference",
            unique=True,
            postgresql_where=text("manual_reference IS NOT NULL"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # RESTRICT (BILL-19): see `Invoice.tenant_id`.
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # RESTRICT: a payment is money that moved, and it outlives any attempt to
    # delete the invoice it was collected against. Composite with `tenant_id`
    # - see `__table_args__`.
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
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
    # The provider's id for the *intended* payment - Paymob's intention id
    # (`pi_test_...`) - written when the intention is created, for a hosted
    # checkout and for an automatic charge alike. One meaning and one only
    # (BILL-04): it is not the order id and not a transaction id, and nothing
    # correlates a callback on it. Kept so support can find an abandoned
    # checkout in the provider's dashboard.
    provider_intent_reference: Mapped[str | None] = mapped_column(
        String(MAX_REFERENCE_LENGTH),
        nullable=True,
    )
    # The provider's order under that intention - Paymob's
    # `intention_order_id`. **The identifier every callback is bound to**
    # (BILL-04, BILL-11): a transaction callback's `order.id` is inside the
    # HMAC and must equal this before anything settles, and a TOKEN callback's
    # `order_id` finds its workspace by it. NULL only on a payment created
    # before 0071; no historical order id is invented for one.
    provider_order_id: Mapped[str | None] = mapped_column(
        String(MAX_REFERENCE_LENGTH),
        nullable=True,
    )
    # `test` or `live`: which of the provider's environments this attempt was
    # created in. Identifiers are not meaningful across the two, and a signed
    # Test callback must never settle a Live payment (BILL-11).
    provider_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # The provider integration the settling transaction ran on, recorded from
    # the verified callback - the card integration for a hosted checkout, the
    # MOTO integration for an automatic charge.
    provider_integration_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
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
    # How much the standing refund request asked for, recorded with the
    # request (BILL-13). Compared with what the provider confirms, and what
    # distinguishes a partial refund an operator approved as goodwill from a
    # reversal nobody here asked for.
    refund_requested_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
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
    # When this payment's money was applied to its invoice - counted in its
    # `amount_paid` - by settlement (DB-001). NULL for an attempt that
    # collected nothing, and for collected money that was *held*: a second
    # payment for an invoice already paid, a page paid after its offer was
    # declined. Held money always has a billing incident beside it, and
    # `amount_paid` always equals the net of the applied payments; the
    # database checks both at commit (migration 0074).
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # What an operator wrote down for money that arrived outside a processor -
    # a bank transfer's reference. Display and duplicate detection within the
    # workspace only; the ledger identifies the payment by
    # `provider_reference`, which for such a payment is ours (DB-017).
    manual_reference: Mapped[str | None] = mapped_column(
        String(MAX_REFERENCE_LENGTH),
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


# An invoice may only name a version of a plan its workspace may hold - see
# `CUSTOM_PLAN_SCOPE_FUNCTION_SQL` in `billing.py` (ADR-113).
INVOICES_CUSTOM_PLAN_TRIGGER_SQL: Final = (
    "CREATE TRIGGER invoices_custom_plan_scope BEFORE INSERT OR UPDATE OF "
    "tenant_id, plan_version_id ON invoices "
    "FOR EACH ROW EXECUTE FUNCTION billing_refuse_foreign_custom_plan()"
)

# **An invoice charges exactly the price it names, and keeps it** (ADR-116).
# On insert: a purchase or renewal of a priced version names one of its prices
# (the composite key keeps it that version's), and the amount, currency,
# interval and count are that price's own - so a client-supplied figure, a
# renewal at the monthly price for a yearly term, or a checkout priced from a
# newer price than it pinned cannot be written. On update: the price and its
# snapshot never move, whatever the invoice's status. A top-up, a free renewal
# and a pre-0071 invoice name no price. Restated verbatim by migration 0081.
INVOICE_PRICE_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION invoices_refuse_price_mismatch() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND (
               NEW.plan_price_id IS DISTINCT FROM OLD.plan_price_id
            OR NEW.billing_interval IS DISTINCT FROM OLD.billing_interval
            OR NEW.interval_count IS DISTINCT FROM OLD.interval_count
            OR (NEW.plan_price_id IS NOT NULL AND (
                   NEW.amount_due IS DISTINCT FROM OLD.amount_due
                OR NEW.currency IS DISTINCT FROM OLD.currency))
        ) THEN
            RAISE EXCEPTION 'an invoice keeps the price it was issued at'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'INSERT' AND NEW.plan_price_id IS NULL
           AND NEW.purpose::text IN ('checkout', 'renewal', 'manual')
           AND EXISTS (SELECT 1 FROM plan_versions v
                        WHERE v.id = NEW.plan_version_id AND v.price > 0) THEN
            RAISE EXCEPTION 'an invoice for a priced plan version names the price it charges'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.plan_price_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM plan_prices p
             WHERE p.id = NEW.plan_price_id
               AND p.amount = NEW.amount_due
               AND p.currency = NEW.currency
               AND p.billing_interval = NEW.billing_interval
               AND p.interval_count = NEW.interval_count
        ) THEN
            RAISE EXCEPTION 'an invoice charges exactly the price it names'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$
    """
INVOICE_PRICE_TRIGGER_SQL: Final = (
    "CREATE TRIGGER invoices_price_snapshot BEFORE INSERT OR UPDATE OF "
    "plan_price_id, billing_interval, interval_count, amount_due, currency ON invoices "
    "FOR EACH ROW EXECUTE FUNCTION invoices_refuse_price_mismatch()"
)

# **A top-up is never a merchant-initiated charge** (ADR-113). The collection
# sweep only claims renewals, and `RecurringService` checks the purpose again;
# this makes the property the ledger's own, so no future collection path can
# debit a saved card for an add-on the customer did not just choose to buy.
PAYMENTS_NO_AUTOMATIC_TOPUP_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION payments_refuse_automatic_topup() RETURNS trigger AS $$
    BEGIN
        IF NEW.is_automatic AND EXISTS (
            SELECT 1 FROM invoices i
             WHERE i.id = NEW.invoice_id AND i.purpose::text = 'topup'
        ) THEN
            RAISE EXCEPTION 'topup invoices are never collected automatically'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_catalog
    """
PAYMENTS_NO_AUTOMATIC_TOPUP_TRIGGER_SQL: Final = (
    "CREATE TRIGGER payments_no_automatic_topup BEFORE INSERT OR UPDATE OF "
    "is_automatic, invoice_id ON payments "
    "FOR EACH ROW EXECUTE FUNCTION payments_refuse_automatic_topup()"
)

# **The books balance, and held money is explained** (DB-001). Checked at
# commit rather than per statement: a settlement writes the invoice and the
# payment in whichever order its flushes happen, and only the transaction's
# end state has to agree. Two things, for every invoice or payment a
# transaction touched:
#
# - `invoices.amount_paid` equals the net (amount - refunded) of the payments
#   *applied* to it. A second settlement that read the invoice before another
#   committed - the concurrent double settlement the audit reproduced - leaves
#   two applied payments against one invoice's worth of `amount_paid`, and is
#   refused here at commit however it got there.
# - collected money that was not applied is held with a billing incident.
#   Nothing may keep a customer's money silently.
#
# The invoice is locked (`FOR NO KEY UPDATE`) before it is summed, so two
# transactions cannot each check before the other commits. Every settlement
# path already holds that lock; this only makes the check honest when one
# does not. The rows are re-read: a deferred trigger's NEW is the row as the
# queued statement left it, not as the transaction ends.
COLLECTION_RECONCILES_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION billing_refuse_unreconciled_collection() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    DECLARE
        target uuid;
        payment_status text;
        payment_applied timestamptz;
        held numeric;
        applied numeric;
    BEGIN
        IF TG_TABLE_NAME = 'payments' THEN
            SELECT p.invoice_id, p.status::text, p.applied_at
              INTO target, payment_status, payment_applied
              FROM payments p WHERE p.id = NEW.id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            IF payment_status IN ('succeeded', 'refunded') AND payment_applied IS NULL
               AND NOT EXISTS (SELECT 1 FROM billing_incidents b WHERE b.payment_id = NEW.id) THEN
                RAISE EXCEPTION 'collected money not applied to its invoice is held by an incident'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSE
            target := NEW.id;
        END IF;
        SELECT i.amount_paid INTO held FROM invoices i WHERE i.id = target FOR NO KEY UPDATE;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        SELECT coalesce(sum(p.amount - p.refunded_amount), 0) INTO applied
          FROM payments p WHERE p.invoice_id = target AND p.applied_at IS NOT NULL;
        IF applied <> held THEN
            RAISE EXCEPTION 'an invoice holds exactly the money applied to it'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END;
    $$
    """
INVOICES_COLLECTION_TRIGGER_SQL: Final = (
    "CREATE CONSTRAINT TRIGGER invoices_collection_reconciles "
    "AFTER INSERT OR UPDATE OF amount_paid ON invoices "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION billing_refuse_unreconciled_collection()"
)
PAYMENTS_COLLECTION_TRIGGER_SQL: Final = (
    "CREATE CONSTRAINT TRIGGER payments_collection_reconciles "
    "AFTER INSERT OR UPDATE OF status, amount, refunded_amount, applied_at, invoice_id "
    "ON payments DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "WHEN (NEW.status::text IN ('succeeded', 'refunded') OR NEW.applied_at IS NOT NULL) "
    "EXECUTE FUNCTION billing_refuse_unreconciled_collection()"
)

# **Settled history is not rewritten** (DB-005, ADR-113 §5). Once an invoice is
# paid or void, what it charged for - workspace, purpose, plan and version,
# offer, amount due, currency, lines, period - is fixed; the only moves out of
# `paid` are the documented reversals, which always give money back
# (`amount_paid` falls): a refund reopening it, or an operator's full refund
# voiding it. A void invoice stays void. `subscription_id` may only become NULL
# (its foreign key's SET NULL); notes, provider fields and collection
# bookkeeping stay writable.
INVOICE_HISTORY_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION invoices_refuse_history_rewrite() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF OLD.status::text NOT IN ('paid', 'void') THEN
            RETURN NEW;
        END IF;
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.purpose IS DISTINCT FROM OLD.purpose
           OR NEW.plan_code IS DISTINCT FROM OLD.plan_code
           OR NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id
           OR NEW.custom_plan_offer_id IS DISTINCT FROM OLD.custom_plan_offer_id
           OR NEW.amount_due IS DISTINCT FROM OLD.amount_due
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.lines IS DISTINCT FROM OLD.lines
           OR NEW.period_start IS DISTINCT FROM OLD.period_start
           OR NEW.period_end IS DISTINCT FROM OLD.period_end
           OR (NEW.subscription_id IS DISTINCT FROM OLD.subscription_id
               AND NEW.subscription_id IS NOT NULL) THEN
            RAISE EXCEPTION 'a settled invoice keeps the terms it was settled on'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.status::text = 'void'
           AND (NEW.status IS DISTINCT FROM OLD.status
                OR NEW.amount_paid IS DISTINCT FROM OLD.amount_paid) THEN
            RAISE EXCEPTION 'a void invoice stays void'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF OLD.status::text = 'paid' THEN
            IF NEW.amount_paid > OLD.amount_paid THEN
                RAISE EXCEPTION 'a paid invoice takes no more money'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status::text <> 'paid' AND NEW.amount_paid >= OLD.amount_paid THEN
                RAISE EXCEPTION 'a paid invoice reopens only when money goes back'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status::text = 'paid' AND NEW.paid_at IS DISTINCT FROM OLD.paid_at THEN
                RAISE EXCEPTION 'a paid invoice keeps when it was paid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$
    """
INVOICE_HISTORY_TRIGGER_SQL: Final = (
    "CREATE TRIGGER invoices_history_immutable BEFORE UPDATE ON invoices "
    "FOR EACH ROW EXECUTE FUNCTION invoices_refuse_history_rewrite()"
)

# The same for money collected. A succeeded or refunded payment keeps what it
# was - workspace, invoice, amount, currency, provider and its identifiers,
# when it was processed, how it was taken, the operator's reference - and
# whether it was applied, once it was. It only ever goes back: `succeeded ->
# refunded`, with `refunded_amount` never falling. The card may be detached
# (its foreign key's SET NULL); refund bookkeeping, reconciliation and the
# failure note stay writable.
PAYMENT_HISTORY_FUNCTION_SQL: Final = """
    CREATE OR REPLACE FUNCTION payments_refuse_history_rewrite() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = public, pg_catalog
    AS $$
    BEGIN
        IF OLD.status::text NOT IN ('succeeded', 'refunded') THEN
            RETURN NEW;
        END IF;
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.invoice_id IS DISTINCT FROM OLD.invoice_id
           OR NEW.amount IS DISTINCT FROM OLD.amount
           OR NEW.currency IS DISTINCT FROM OLD.currency
           OR NEW.provider IS DISTINCT FROM OLD.provider
           OR NEW.provider_reference IS DISTINCT FROM OLD.provider_reference
           OR NEW.provider_order_id IS DISTINCT FROM OLD.provider_order_id
           OR NEW.provider_integration_id IS DISTINCT FROM OLD.provider_integration_id
           OR NEW.processed_at IS DISTINCT FROM OLD.processed_at
           OR NEW.is_automatic IS DISTINCT FROM OLD.is_automatic
           OR NEW.manual_reference IS DISTINCT FROM OLD.manual_reference
           OR (OLD.applied_at IS NOT NULL AND NEW.applied_at IS DISTINCT FROM OLD.applied_at)
           OR (NEW.payment_method_id IS DISTINCT FROM OLD.payment_method_id
               AND NEW.payment_method_id IS NOT NULL) THEN
            RAISE EXCEPTION 'collected money keeps what it was'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.status::text NOT IN ('succeeded', 'refunded')
           OR (OLD.status::text = 'refunded' AND NEW.status::text <> 'refunded')
           OR NEW.refunded_amount < OLD.refunded_amount THEN
            RAISE EXCEPTION 'collected money only ever goes back'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$
    """
PAYMENT_HISTORY_TRIGGER_SQL: Final = (
    "CREATE TRIGGER payments_history_immutable BEFORE UPDATE ON payments "
    "FOR EACH ROW EXECUTE FUNCTION payments_refuse_history_rewrite()"
)

event.listen(Invoice.__table__, "after_create", DDL(CUSTOM_PLAN_SCOPE_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Invoice.__table__, "after_create", DDL(INVOICES_CUSTOM_PLAN_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Payment.__table__, "after_create", DDL(PAYMENTS_NO_AUTOMATIC_TOPUP_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Payment.__table__, "after_create", DDL(PAYMENTS_NO_AUTOMATIC_TOPUP_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Invoice.__table__, "after_create", DDL(INVOICE_OFFER_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Invoice.__table__, "after_create", DDL(INVOICE_OFFER_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Invoice.__table__, "after_create", DDL(COLLECTION_RECONCILES_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Invoice.__table__, "after_create", DDL(INVOICES_COLLECTION_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Payment.__table__, "after_create", DDL(PAYMENTS_COLLECTION_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Invoice.__table__, "after_create", DDL(INVOICE_HISTORY_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Invoice.__table__, "after_create", DDL(INVOICE_HISTORY_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Payment.__table__, "after_create", DDL(PAYMENT_HISTORY_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Payment.__table__, "after_create", DDL(PAYMENT_HISTORY_TRIGGER_SQL))  # type: ignore[no-untyped-call]
event.listen(Invoice.__table__, "after_create", DDL(INVOICE_PRICE_FUNCTION_SQL))  # type: ignore[no-untyped-call]
event.listen(Invoice.__table__, "after_create", DDL(INVOICE_PRICE_TRIGGER_SQL))  # type: ignore[no-untyped-call]
