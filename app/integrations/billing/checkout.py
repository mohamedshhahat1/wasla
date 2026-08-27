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


@dataclass(frozen=True, slots=True)
class CallbackEvent:
    """A verified statement from the provider about one payment attempt.

    Produced only after the signature has been checked, which is why there is
    no `verified` flag: an unverified callback never becomes one of these. A
    caller holding this object is holding something the provider said.

    `reference` is the value we sent in `CheckoutRequest.reference` and is how
    the event is matched to a payment. `event_id` is the provider's identifier
    for *this event*, and is what the idempotency constraint is built on - two
    deliveries of one event share it, and a later refund of the same payment
    does not.

    `amount` and `currency` are what the provider says was actually collected.
    They are carried so the caller can refuse an event that disagrees with the
    invoice rather than trusting it, which is the difference between a webhook
    that reports a payment and a webhook that decides one.
    """

    event_id: str
    reference: str | None
    status: PaymentStatus
    amount: Decimal
    currency: str
    provider_payment_id: str | None = None
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is PaymentStatus.SUCCEEDED


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


class CallbackVerificationError(Exception):
    """A callback could not be authenticated as coming from the provider.

    Deliberately not an `ExternalServiceError`: nothing external failed. Either
    somebody forged a request, or the deployment's signing secret is wrong, and
    both are refusals rather than outages.
    """
