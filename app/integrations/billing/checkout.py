"""The hosted-checkout boundary: a second provider shape, not a bigger first one.

`PaymentProvider` in `base.py` models a **pull**: we hand a processor an amount
and it tells us what happened, synchronously, in one call. That is the right
model for a stored card and for the manual provider, and it is the wrong model
for every hosted checkout there is.

A hosted checkout inverts it. We describe an intended payment, the provider
gives back somewhere to send the customer, and the *answer* arrives later on a
different connection that the provider opens to us. Nothing is collected during
the call we make; a `ChargeOutcome` returned from `create_checkout` could only
ever say `PENDING`, which is a value that means "ask again later" and has no
later to be asked in.

So this is a separate protocol rather than three more methods on the first one.
`ManualProvider` cannot host a checkout and should not have to raise
`NotImplementedError` to say so, and a future stored-card provider may implement
`PaymentProvider` alone. A provider implements whichever shapes it actually has.

**Nothing here is Paymob.** The names are the ones the billing domain already
uses - an amount, a currency, a reference, an outcome - and a service above this
module cannot tell which processor is behind it. That is the whole point of the
boundary: `CheckoutService` asks for a checkout, and the day a second provider
is added it changes which object is constructed and nothing else (ADR-031).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.db.models.invoice import PaymentStatus


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    """What the billing domain knows about a payment it wants collected.

    Deliberately every value the provider needs and nothing it does not. There
    is no plan object, no subscription and no workspace here: a processor is
    told an amount, a currency and an opaque reference to quote back, because
    anything more is our business model leaking into somebody else's system.

    `reference` is ours and is what the callback must carry home. It is the
    internal payment id, so the row a callback resolves to is decided by us
    rather than by anything the customer's browser passed through.

    `customer_email` and `customer_name` exist because hosted checkouts show
    them and some require them; they come from the account, never from request
    input. No address, no phone: this product does not collect them, and a
    field sent because a provider has a slot for it is a field leaking data.
    """

    reference: str
    amount: Decimal
    currency: str
    description: str
    customer_email: str | None = None
    customer_name: str | None = None
    # Small, non-secret facts the provider echoes back, used only for support
    # correlation. Never anything that would be damaging to disclose: whatever
    # is put here is stored on a third party's systems and comes back through a
    # request anybody can send at our webhook.
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    """Where to send the customer, and what the provider called this attempt.

    `redirect_url` is the only part the API hands out. `provider_reference` is
    the provider's own id for the intended payment, stored so support can find
    it in their dashboard - and so a callback that names it can be tied back to
    the row that started it.
    """

    redirect_url: str
    provider_reference: str


class EventKind(StrEnum):
    """What a provider callback is *reporting*, as opposed to what it is about.

    A processor does not send one notification per payment; it sends one per
    thing that happened to a payment, and the same transaction produces several
    over its life - in flight, collected, and later given back. This is that
    distinction, and it exists because idempotency depends on it: two
    deliveries of "collected" are the same event, and "collected" followed by
    "refunded" are two.

    `VOIDED` is kept apart from `REFUNDED` even though both map to
    `PaymentStatus.REFUNDED`. The money moved differently - a void cancels
    before settlement and a refund returns what was settled - and an operator
    reading the ledger to answer "where did this go" needs to see which.
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"
    VOIDED = "voided"


@dataclass(frozen=True, slots=True)
class CallbackEvent:
    """A verified statement from the provider about one payment attempt.

    Produced only after the signature has been checked, which is why there is
    no `verified` flag: an unverified callback never becomes one of these. A
    caller holding this object is holding something the provider said.

    `reference` is the value we sent in `CheckoutRequest.reference` and is how
    the event is matched to a payment.

    `event_id` is what the idempotency constraint is built on, and it is
    deliberately **not** the raw transaction id. It pairs the transaction with
    the state being reported, because a processor sends more than one callback
    about one transaction: in flight, then collected, then - if somebody
    refunds it - collected-and-refunded. Keying on the transaction id alone
    would make each of those a duplicate of the first, so a 3-D Secure payment
    that reported `pending` before it reported `success` would settle nothing,
    and a refund notification on the original transaction would be silently
    dropped. Pairing keeps every delivery of one state a duplicate of itself,
    which is the property the constraint actually needs.

    `provider_transaction_id` is the raw id, kept because it is the number a
    support conversation and the provider's dashboard both use.

    `parent_transaction_id` is documented as present on a refund or a void: it
    names the transaction being reversed. It is a second way to find the
    payment when the reversal does not carry our own reference home.

    `amount` and `currency` are what the provider says was actually collected.
    They are carried so the caller can refuse an event that disagrees with the
    invoice rather than trusting it, which is the difference between a webhook
    that reports a payment and a webhook that decides one.
    """

    event_id: str
    reference: str | None
    kind: EventKind
    status: PaymentStatus
    amount: Decimal
    currency: str
    provider_transaction_id: str | None = None
    parent_transaction_id: str | None = None
    refunded_amount: Decimal | None = None
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is PaymentStatus.SUCCEEDED

    @property
    def event_type(self) -> str:
        """The ledger's name for this kind of event, namespaced by subject."""
        return f"transaction.{self.kind.value}"


@dataclass(frozen=True, slots=True)
class RefundRequest:
    """What the billing domain knows about money it wants returned.

    `transaction_reference` is the provider's own id for the transaction that
    collected the money, read off the payment row. Never anything a client
    sent: a caller that could name a transaction could refund somebody else's.

    `amount` is likewise the payment's, computed here rather than accepted.
    """

    transaction_reference: str
    amount: Decimal
    currency: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RefundOutcome:
    """What the provider said when asked to reverse a payment.

    `provider_reference` is the reversal's own identifier, which is a different
    transaction from the one being reversed. Stored so the callback that
    reports the reversal can be tied back to the request that caused it.

    `pending` is the honest default for a processor that accepts a reversal and
    performs it later. A caller must not tell a customer their money is back
    because this returned.
    """

    provider_reference: str
    amount: Decimal
    pending: bool = False


@runtime_checkable
class CheckoutProvider(Protocol):
    """A provider that hosts the payment page and calls us back."""

    @property
    def name(self) -> str:
        """Stored on the payment, so a row says who handled it."""
        ...

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        """Describe an intended payment and get somewhere to send the customer.

        Raises `ProviderError` if the provider cannot be reached or refuses to
        create the session. Nothing is collected here, so there is no outcome
        to return - the answer arrives at the callback endpoint.
        """
        ...

    def verify_callback(
        self,
        *,
        payload: bytes,
        signature: str | None,
    ) -> CallbackEvent:
        """Authenticate a callback and say what it means.

        Synchronous and pure: it takes bytes and returns a fact, touching no
        database and no network, which is what makes the signature rule
        testable in isolation against the provider's own published vectors.

        Raises `CallbackVerificationError` when the signature does not check
        out, and `ValueError` when the body is not a shape this provider
        recognises. It must never return an event for input it could not
        authenticate: failing closed is the whole security property.
        """
        ...

    async def refund(self, request: RefundRequest) -> RefundOutcome:
        """Ask the provider to give a collected payment back.

        Synchronous in the sense that a result comes back on this call, unlike
        `create_checkout` - the provider either accepts the reversal or refuses
        it, and there is nobody's browser in the middle. The money still moves
        on the provider's own schedule, and a callback reporting the reversal
        arrives afterwards; this outcome says the request was accepted, not
        that a customer has their money.

        Raises `ProviderError` when the provider cannot be reached or refuses.
        A refusal is an error here rather than an outcome, and that is the
        opposite of `PaymentProvider.charge` on purpose: a declined card is an
        ordinary thing a customer did, while a refund the processor will not
        perform is a problem an operator has to look at.
        """
        ...


@dataclass(frozen=True, slots=True)
class SavedPaymentMethod:
    """A card a customer chose to keep, as the provider describes it.

    Everything here is safe to store. `token` is the provider's opaque handle -
    it is not a card number, it cannot be used anywhere but this merchant
    account, and it is what makes charging a renewal possible without anybody
    holding a PAN. `masked_pan` is the last four digits the provider already
    prints on receipts.

    There is deliberately no field for a card number, an expiry or a CVV.
    Those never reach this application: the customer types them into the
    provider's own page, and what comes back is this.
    """

    token: str
    provider_token_id: str
    masked_pan: str | None = None
    brand: str | None = None
    # Ties the saved card back to the checkout that created it, which is how a
    # token callback is matched to the workspace that owns it. The provider
    # quotes back the order it was saved against.
    order_reference: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class SavedMethodCharge:
    """A renewal we want taken from a card already on file.

    `reference` is ours and is what the resulting callback quotes home, exactly
    as at checkout. `token` is the provider's, read from a stored payment
    method - never from a request.
    """

    reference: str
    token: str
    amount: Decimal
    currency: str
    description: str


@runtime_checkable
class RecurringProvider(Protocol):
    """A provider that can charge a card the customer already saved.

    A third shape, and separate for the same reason `CheckoutProvider` is
    separate from `PaymentProvider`: this is a *merchant-initiated* charge with
    nobody's browser involved, and a provider may support hosted checkout
    without supporting it. `ManualProvider` cannot do this and should not have
    to raise `NotImplementedError` to say so.

    Implementing this protocol is not the same as being able to use it. A
    provider may require merchant-level capability that a given account does
    not have, which is why `RecurringUnavailableError` exists as a distinct
    outcome from "the request failed".
    """

    @property
    def name(self) -> str:
        """Stored on the payment, so a row says who handled it."""
        ...

    @property
    def can_charge_saved_methods(self) -> bool:
        """Whether this deployment is configured to charge without a customer.

        False is a normal, supported state: it means renewals are collected by
        asking the customer to pay an invoice rather than by debiting a card.
        Callers check this instead of catching an exception, because "we do not
        do that here" is a configuration fact rather than a failure.
        """
        ...

    def verify_token_callback(
        self,
        *,
        payload: bytes,
        signature: str | None,
    ) -> SavedPaymentMethod:
        """Authenticate a saved-card notification and read the card out of it.

        Separate from `verify_callback` because providers sign these
        differently - a different field set, and therefore a different string.
        Fails closed on every path, exactly as the transaction one does.
        """
        ...

    async def charge_saved_method(self, request: SavedMethodCharge) -> str:
        """Debit a stored card and return the provider's reference for it.

        The outcome arrives at the callback endpoint like any other payment,
        so this returns only what the attempt was called - not whether it
        worked. A provider that answered "collected" here would be asking to be
        believed without a signature.

        Raises `RecurringUnavailableError` when the account cannot do this at
        all, and `ProviderError` when it can and the attempt failed.
        """
        ...


class CallbackVerificationError(Exception):
    """A callback could not be authenticated as coming from the provider.

    Deliberately not an `ExternalServiceError`: nothing external failed. Either
    somebody forged a request, or the deployment's signing secret is wrong, and
    both are refusals rather than outages.
    """


class RecurringUnavailableError(Exception):
    """This account cannot charge a saved card, however well the code works.

    Deliberately not a `ProviderError`. Nothing failed and nothing is
    misconfigured on our side: the merchant account lacks a capability the
    provider gates, and no retry, credential or code change here will alter
    that. Callers treat it as "collect this renewal the other way" rather than
    as an outage, so a deployment without the capability bills exactly as it
    did before.
    """
