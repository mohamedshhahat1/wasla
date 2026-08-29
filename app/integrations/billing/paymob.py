"""Paymob, spoken to directly over HTTPS.

Two things happen here and nothing else: an intention is created so a customer
can be sent somewhere to pay, and a callback that arrives later is
authenticated and translated into the billing domain's vocabulary. No database,
no subscription, no plan - `CheckoutService` owns all of that, and this module
could not reach it if it wanted to.

Everything below follows the current official documentation
(https://developers.paymob.com/paymob-docs), read on 2026-08-27. The parts that
would be easy to get wrong from an older tutorial, and are therefore written
down here with the source:

**The API host and the checkout host are different.** Intentions are created at
``{base}/v1/intention/`` on ``accept.paymob.com``; the customer is redirected to
``eg.checkout.paymob.com``. An integration that assumed one host for both would
create intentions successfully and send every customer to a 404.

**Test and live share the base URL.** The docs are explicit: "Test and live use
the same regional base URL for each region. The mode is controlled by the keys
and integration IDs you use." There is no sandbox host to point at, so there is
no sandbox setting here - the key decides.

**Amounts are integer cents.** `amount` is "the total transaction amount,
expressed in cents", so a `Decimal` price is converted once, here, and the
conversion refuses anything that is not a whole number of cents rather than
rounding somebody's money silently.

**`special_reference` comes back as `merchant_order_id`.** That is the
documented round trip, and it is what ties a callback to a payment row. We send
the internal payment id and match on it, so the mapping is decided by us rather
than by anything that passed through a browser.

**The HMAC is over twenty named fields in a fixed order.** See
`hmac_signature`, which carries the documented order verbatim and is pinned by
the worked example the documentation publishes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any, Final

import httpx

from app.core.logging import get_logger
from app.db.models.invoice import PaymentStatus
from app.integrations.billing.base import ProviderError
from app.integrations.billing.checkout import (
    CallbackEvent,
    CallbackVerificationError,
    CheckoutRequest,
    CheckoutSession,
    EventKind,
    RefundOutcome,
    RefundRequest,
)

logger = get_logger(__name__)

PAYMOB_PROVIDER: Final = "paymob"
DEFAULT_TIMEOUT_SECONDS: Final = 20.0
# Enough of a provider error to act on, too little to carry a credential back
# out through a log. Paymob error bodies quote the request.
MAX_ERROR_LENGTH: Final = 300
# A provider's decline text, bounded. Long enough to be actionable, short
# enough that nobody stores a payload in it.
MAX_FAILURE_REASON_LENGTH: Final = 200

# Stands in for a billing field Paymob requires and this product does not
# collect. Spelled to be unmistakable in the provider's dashboard: somebody
# reading a transaction must not take it for a customer's real telephone
# number. See `PaymobProvider._billing_data`.
UNKNOWN_BILLING_FIELD: Final = "NOT_COLLECTED"

# The regions Paymob publishes, each with its API base and its checkout host.
# Both are needed and they are not the same host - see the module docstring.
REGIONS: Final[dict[str, tuple[str, str]]] = {
    "egypt": ("https://accept.paymob.com", "https://eg.checkout.paymob.com"),
    "uae": ("https://uae.paymob.com", "https://uae.checkout.paymob.com"),
    "oman": ("https://oman.paymob.com", "https://om.checkout.paymob.com"),
    "saudi": ("https://ksa.paymob.com", "https://ksa.checkout.paymob.com"),
}

INTENTION_PATH: Final = "/v1/intention/"
# Reversal of a transaction, documented at
# developers.paymob.com/paymob-docs/developers/manage-payment-apis/refund
# (read 2026-08-29): POST with the secret key, taking `transaction_id` and
# `amount_cents`. The same page documents `/api/acceptance/void_refund/void`
# for a transaction that has not settled yet - deliberately not called here,
# see `PaymobProvider.refund`.
REFUND_PATH: Final = "/api/acceptance/void_refund/refund"

# What each reported state means for the payment row. Voiding and refunding
# both leave a payment that was collected and given back; the difference is
# whether it had settled first, and that is kept as the event kind rather than
# flattened into the status.
_STATUS_FOR_KIND: Final[dict[EventKind, PaymentStatus]] = {
    EventKind.PENDING: PaymentStatus.PENDING,
    EventKind.SUCCEEDED: PaymentStatus.SUCCEEDED,
    EventKind.FAILED: PaymentStatus.FAILED,
    EventKind.REFUNDED: PaymentStatus.REFUNDED,
    EventKind.VOIDED: PaymentStatus.REFUNDED,
}

# The exact keys the HMAC is built from, in the order the documentation lists
# them, for a *processed POST* callback. Written as a tuple of paths into the
# transaction object because two of them are nested, and because a list that
# can be read against the documentation line by line is the only way anybody
# checks this again later.
#
# The documented order is lexicographic by key name, with `obj.id` sorting as
# "id" and `order.id` sorting as "order_id" - which is why they sit where they
# do rather than where a naive sort of these strings would put them.
HMAC_FIELDS: Final[tuple[str, ...]] = (
    "amount_cents",
    "created_at",
    "currency",
    "error_occured",
    "has_parent_transaction",
    "id",
    "integration_id",
    "is_3d_secure",
    "is_auth",
    "is_capture",
    "is_refunded",
    "is_standalone_payment",
    "is_voided",
    "order.id",
    "owner",
    "pending",
    "source_data.pan",
    "source_data.sub_type",
    "source_data.type",
    "success",
)


def _dig(obj: dict[str, Any], path: str) -> Any:
    """Read a dotted path out of the transaction object.

    Returns `None` for anything missing rather than raising: a field absent
    from the payload contributes an empty string to the signed text, and the
    signature then simply fails to match. Guessing a default would be inventing
    the very bytes being authenticated.
    """
    current: Any = obj
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _canonical(value: Any) -> str:
    """One value as Paymob writes it into the signed string.

    Booleans are lowercase `true`/`false`, which is JSON's spelling and not
    Python's - `str(True)` would produce `True` and every signature would fail.
    `None` becomes empty. Everything else is its plain string form; the
    documented worked example concatenates integers with no separators, so
    there is no formatting to apply.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def hmac_message(transaction: dict[str, Any]) -> str:
    """The exact string Paymob signs for a transaction callback.

    Public and separately testable because this is the part that is worth
    pinning: the documentation publishes a worked example, and
    `tests/unit/test_paymob_hmac.py` asserts this function reproduces it
    character for character. A field reordered by a future edit fails that test
    rather than failing silently in production against a live secret.
    """
    return "".join(_canonical(_dig(transaction, field)) for field in HMAC_FIELDS)


def hmac_signature(transaction: dict[str, Any], *, secret: str) -> str:
    """HMAC-SHA512 of the signed string, hex encoded, as documented."""
    return hmac.new(
        secret.encode("utf-8"),
        hmac_message(transaction).encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()


def _to_cents(amount: Decimal) -> int:
    """A decimal amount as the integer cents Paymob expects.

    Refuses a fraction of a cent instead of rounding it. A rounded amount is a
    charge that disagrees with the invoice by a hundredth, which the callback
    check would then reject anyway - and doing it here means the customer is
    never sent to a checkout for the wrong figure in the first place.
    """
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ProviderError("An amount must be a whole number of cents.")
    return int(cents)


def _from_cents(value: Any) -> Decimal:
    """Integer cents from a payload back into money.

    Anything that is not an integer becomes zero rather than raising. The
    caller compares this against the invoice and refuses a mismatch, so a
    malformed amount is refused by that comparison - which is a better failure
    than an exception inside an already-verified callback, because the event
    still gets recorded and an operator can see it.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return Decimal("0")
    return Decimal(value) / 100


class PaymobProvider:
    """Creates Paymob intentions and authenticates Paymob callbacks."""

    def __init__(
        self,
        *,
        secret_key: str,
        public_key: str,
        hmac_secret: str,
        integration_ids: list[int | str],
        region: str = "egypt",
        notification_url: str | None = None,
        redirection_url: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if region not in REGIONS:
            raise ProviderError(f"Unknown Paymob region: {region}.")
        if not integration_ids:
            # An intention with no payment method is refused by Paymob, and
            # discovering that at the first customer is worse than at startup.
            raise ProviderError("At least one Paymob integration id is required.")

        self._secret_key = secret_key
        self._public_key = public_key
        self._hmac_secret = hmac_secret
        self._integration_ids = integration_ids
        self._api_base, self._checkout_base = REGIONS[region]
        self._notification_url = notification_url
        self._redirection_url = redirection_url
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def name(self) -> str:
        return PAYMOB_PROVIDER

    def __repr__(self) -> str:  # pragma: no cover - stops a key reaching a log
        return f"PaymobProvider(region={self._api_base!r})"

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        """Create an intention and build the URL the customer is sent to.

        The amount and currency come from the caller, which got them from the
        database - there is no path by which a request body reaches this.
        """
        body: dict[str, Any] = {
            "amount": _to_cents(request.amount),
            "currency": request.currency,
            # Exactly what was configured, in order, uninterpreted. Which
            # methods the customer is then offered is Paymob's decision from
            # this list - there is deliberately no branching here on card
            # versus wallet, because this integration does not know which an
            # entry is and does not need to.
            "payment_methods": list(self._integration_ids),
            # Ours, and quoted back to us as `merchant_order_id`. This is the
            # entire mapping from a callback to a payment row.
            "special_reference": request.reference,
            "items": [
                {
                    "name": request.description,
                    "amount": _to_cents(request.amount),
                    "quantity": 1,
                }
            ],
        }
        if request.metadata:
            body["extras"] = dict(request.metadata)
        if self._notification_url:
            body["notification_url"] = self._notification_url
        if self._redirection_url:
            body["redirection_url"] = self._redirection_url

        # Always sent, never conditionally: Paymob refuses an intention whose
        # billing block is missing a required key, so an empty one is not a
        # smaller request, it is a rejected one.
        body["billing_data"] = self._billing_data(request)

        payload = await self._post(INTENTION_PATH, body)

        client_secret = payload.get("client_secret")
        intention_id = payload.get("id")
        if not isinstance(client_secret, str) or not client_secret:
            # A 2xx without the one field the whole flow needs. Treated as a
            # provider failure rather than parsed around: sending a customer to
            # a checkout URL built from a missing secret is a broken page and a
            # payment that can never arrive.
            raise ProviderError("Paymob did not return a client secret.")

        logger.info(
            "billing.paymob_intention_created",
            extra={
                "event": "billing.paymob_intention_created",
                "reference": request.reference,
                "intention_id": str(intention_id) if intention_id else None,
                # Never the client secret: it is a bearer value for this
                # payment page, and a log is not where it belongs.
            },
        )
        return CheckoutSession(
            redirect_url=self.checkout_url(client_secret),
            provider_reference=str(intention_id) if intention_id else request.reference,
        )

    def checkout_url(self, client_secret: str) -> str:
        """The documented Unified Checkout URL for this region.

        Built from configuration and the provider's own secret, with no caller
        input anywhere in it - the same rule the emailed links follow.
        """
        return (
            f"{self._checkout_base}/?publicKey={self._public_key}" f"&clientSecret={client_secret}"
        )

    @staticmethod
    def _billing_data(request: CheckoutRequest) -> dict[str, str]:
        """The billing block Paymob requires, filled from what we actually hold.

        Paymob validates this object rather than treating it as decoration: an
        intention carrying a partial one is refused with
        `{"billing_data":{"phone_number":["This field is required."]}}` - which
        is how this was found, against the live test API, after a version that
        sent only the fields we had was accepted by every mocked test.

        So the required keys are always present. Where the account holds a real
        value it is sent; where it does not, `NOT_COLLECTED` stands in.

        That placeholder is a deliberate reversal of what this method used to
        do, and the reasoning has changed with the facts. Declining to invent
        values is right for *optional* fields - it keeps fiction off somebody
        else's systems. It is not available for required ones: the choice there
        is a placeholder or no card payments at all. The string is spelled to
        be unmistakable in Paymob's dashboard, so nobody reading a transaction
        mistakes it for a customer's real telephone number. Paymob's own
        documented example fills these with `"dumy"`, and their callback sample
        ships `"NA"`, so a placeholder is the convention rather than an abuse.

        Wasla collects no address and no telephone number for a billing
        contact, and this is not the place to start: an address field here
        would be a data-protection question, not a payments one.
        """
        first, _, last = (request.customer_name or "").partition(" ")
        return {
            "email": request.customer_email or UNKNOWN_BILLING_FIELD,
            "first_name": first or UNKNOWN_BILLING_FIELD,
            "last_name": last or UNKNOWN_BILLING_FIELD,
            "phone_number": UNKNOWN_BILLING_FIELD,
        }

    def _redacted(self, text: str) -> str:
        """This deployment's own secrets taken back out of a provider's text.

        Paymob quotes the request back in its error bodies, and a provider
        error is carried into an exception message and from there into a log
        and possibly into an operator's screen. Truncating the body bounds how
        much comes back; it does nothing about *what*, because a credential
        echoed in the first two hundred characters survives truncation intact.

        Only values this process holds are removed, which is the only thing
        that can be done reliably - a general secret-shaped-string scrubber
        would be guessing. Both are checked because an error about the wrong
        key is exactly the error most likely to quote it.
        """
        for secret in (self._secret_key, self._hmac_secret):
            if secret and secret in text:
                text = text.replace(secret, "[redacted]")
        return text

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """One JSON POST, with the credential in exactly one place.

        Every failure leaves as a `ProviderError` with the body truncated:
        Paymob quotes the request back in its errors, and this request carries
        the secret key.
        """
        url = f"{self._api_base}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    url,
                    json=body,
                    headers={
                        "Authorization": f"Token {self._secret_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException as error:
            # No answer, so the request may well have been carried out.
            # Retryable, and safe to retry: an intention is keyed on our own
            # unique reference, and a refund is guarded by the reference stored
            # before one is believed to have happened.
            raise ProviderError("Paymob did not respond in time.", retryable=True) from error
        except httpx.HTTPError as error:
            # Deliberately not interpolating the error: httpx puts the request
            # URL in it, and a future signed URL would end up in a log.
            raise ProviderError("Paymob could not be reached.", retryable=True) from error

        if response.status_code >= 400:
            # 5xx is their failure and may pass; 4xx is a statement about this
            # request - wrong credentials, an unknown integration, an amount
            # they will not take - and sending it again changes nothing. 429 is
            # grouped with the first: it is an explicit "later".
            retryable = response.status_code >= 500 or response.status_code == 429
            detail = self._redacted(response.text[:MAX_ERROR_LENGTH])
            logger.warning(
                "billing.paymob_rejected",
                extra={
                    "event": "billing.paymob_rejected",
                    "status_code": response.status_code,
                    "retryable": retryable,
                },
            )
            raise ProviderError(
                f"Paymob refused the request ({response.status_code}): {detail}",
                retryable=retryable,
            )

        try:
            payload = response.json()
        except ValueError as error:
            # A 2xx we cannot read. Not retryable: the same request produces
            # the same unreadable answer.
            raise ProviderError("Paymob returned a response that was not JSON.") from error
        if not isinstance(payload, dict):
            raise ProviderError("Paymob returned a response of an unexpected shape.")
        return payload

    def verify_callback(
        self,
        *,
        payload: bytes,
        signature: str | None,
    ) -> CallbackEvent:
        """Authenticate a processed-transaction callback and read it.

        Fails closed on every path. A missing signature, a body that is not
        JSON, a body that is not a transaction, and a signature that does not
        match all raise - there is no branch that returns an event for input
        this could not authenticate.

        The comparison is `hmac.compare_digest`, so a near-miss takes the same
        time as a wild guess. A byte-at-a-time equality check against a
        128-character hex digest is a genuinely practical oracle over enough
        requests.
        """
        if not signature:
            raise CallbackVerificationError("The callback carried no signature.")

        try:
            document = json.loads(payload)
        except ValueError as error:
            raise CallbackVerificationError("The callback body was not JSON.") from error
        if not isinstance(document, dict):
            raise CallbackVerificationError("The callback body was not an object.")

        transaction = document.get("obj")
        if not isinstance(transaction, dict):
            raise CallbackVerificationError("The callback carried no transaction.")

        expected = hmac_signature(transaction, secret=self._hmac_secret)
        if not hmac.compare_digest(expected, signature):
            # Neither digest is logged. A rejected signature is worth knowing
            # about; the value that would have matched is not something to
            # write down next to the value that did not.
            raise CallbackVerificationError("The callback signature did not match.")

        return self._event(transaction)

    @staticmethod
    def _event(transaction: dict[str, Any]) -> CallbackEvent:
        """Translate a verified transaction into the billing vocabulary.

        Two judgements, and both decide whether money is believed to have
        moved.

        **What is being reported.** The reversal flags win over the success
        flags, because `is_refunded` arrives on a transaction that *did*
        succeed - the documentation is explicit that a refund produces
        callbacks for the parent transaction carrying `is_refunded: true`, and
        that transaction still says `success: true` because it did succeed
        before the money was given back. Reading `success` first would file a
        refund as another collection and settle the invoice twice.

        **Whether a success is a success.** Deliberately strict: `success` true
        while `pending` is also true is a transaction still in flight, and
        treating it as collected would activate a subscription for money that
        has not moved. Everything that is neither a clean success nor still in
        progress is a failure, because a payment that is neither did not
        happen.

        The event id pairs the transaction with the state - see
        `CallbackEvent.event_id`. That pairing is the whole reason a refund
        notification about the original transaction is not swallowed as a
        duplicate of the payment.
        """
        success = bool(transaction.get("success"))
        pending = bool(transaction.get("pending"))
        error_occured = bool(transaction.get("error_occured"))

        if transaction.get("is_voided"):
            kind = EventKind.VOIDED
        elif transaction.get("is_refunded"):
            kind = EventKind.REFUNDED
        elif success and not pending and not error_occured:
            kind = EventKind.SUCCEEDED
        elif pending and not error_occured:
            kind = EventKind.PENDING
        else:
            kind = EventKind.FAILED

        order = transaction.get("order")
        reference = order.get("merchant_order_id") if isinstance(order, dict) else None

        transaction_id = str(transaction.get("id"))
        parent = transaction.get("parent_transaction")
        refunded_cents = transaction.get("refunded_amount_cents")

        failure_reason: str | None = None
        if kind is EventKind.FAILED:
            data = transaction.get("data")
            if isinstance(data, dict):
                message = data.get("message")
                if isinstance(message, str):
                    failure_reason = message[:MAX_FAILURE_REASON_LENGTH]

        return CallbackEvent(
            event_id=f"{transaction_id}:{kind.value}",
            reference=str(reference) if reference else None,
            kind=kind,
            status=_STATUS_FOR_KIND[kind],
            amount=_from_cents(transaction.get("amount_cents")),
            currency=str(transaction.get("currency") or ""),
            provider_transaction_id=transaction_id,
            parent_transaction_id=str(parent) if parent else None,
            # None rather than zero when absent, so a caller can tell "the
            # provider did not say" from "the provider says nothing has been
            # given back". The documented sample carries null on a fresh
            # payment.
            refunded_amount=(
                _from_cents(refunded_cents)
                if isinstance(refunded_cents, int) and not isinstance(refunded_cents, bool)
                else None
            ),
            failure_reason=failure_reason,
        )

    async def refund(self, request: RefundRequest) -> RefundOutcome:
        """Reverse a collected payment through the documented refund endpoint.

        `POST /api/acceptance/void_refund/refund`, authenticated with the
        secret key, taking the transaction id and an amount in cents. The
        response is a *new* transaction - the reversal - and its id is what a
        later callback about this refund carries.

        Void is not attempted as a fallback. Paymob documents a separate
        endpoint for reversing a transaction that has not settled yet, and
        choosing between the two from an error body would mean guessing at
        response codes this integration has never seen. A refund the provider
        refuses surfaces as a `ProviderError` carrying its reason, which is a
        state an operator can act on.
        """
        payload = await self._post(
            REFUND_PATH,
            {
                "transaction_id": request.transaction_reference,
                "amount_cents": _to_cents(request.amount),
            },
        )

        reversal_id = payload.get("id")
        if reversal_id is None:
            raise ProviderError("Paymob did not identify the refund transaction.")
        if payload.get("success") is False:
            # A 200 that says it did not work. Treated as a refusal rather than
            # read past: recording a refund the provider declined would tell a
            # customer their money is coming back when it is not.
            raise ProviderError("Paymob refused the refund.")

        logger.info(
            "billing.paymob_refund_accepted",
            extra={
                "event": "billing.paymob_refund_accepted",
                "transaction_id": request.transaction_reference,
                "refund_transaction_id": str(reversal_id),
            },
        )
        return RefundOutcome(
            provider_reference=str(reversal_id),
            amount=request.amount,
            pending=bool(payload.get("pending")),
        )
