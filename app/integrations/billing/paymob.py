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
from app.core.net import UnsafeUrlError, build_guarded_client
from app.core.telemetry import CallOutcome, Provider, record_provider_call
from app.db.models.invoice import PaymentStatus
from app.integrations.billing.base import ProviderError
from app.integrations.billing.checkout import (
    CallbackEvent,
    CallbackVerificationError,
    CheckoutRequest,
    CheckoutSession,
    EventKind,
    RecurringUnavailableError,
    RefundOutcome,
    RefundRequest,
    SavedMethodCharge,
    SavedPaymentMethod,
)

logger = get_logger(__name__)

PAYMOB_PROVIDER: Final = "paymob"
# Applied to connect, read, write and pool alike - `httpx.Timeout` of a single
# value sets all four. A provider that accepts a connection and then stalls is
# the failure mode that matters here, and it is a read timeout rather than a
# connect one.
DEFAULT_TIMEOUT_SECONDS: Final = 20.0
# Enough of a provider error to act on, too little to carry a credential back
# out through a log. Paymob error bodies quote the request.
MAX_ERROR_LENGTH: Final = 300

# What each provider call is counted under. Four fixed values, chosen here
# rather than derived from the request path: a path can carry a transaction
# reference, and a metric label domain must not grow with the traffic.
CHECKOUT: Final = "checkout"
MOTO_INTENTION: Final = "moto_intention"
SAVED_CARD_CHARGE: Final = "saved_card_charge"
REFUND: Final = "refund"

# Paymob's own "later", which is a different operational story from a
# refusal: one clears on its own and the other needs somebody to look.
RATE_LIMITED_STATUS: Final = 429
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
# Charging a card the customer already saved, without them present. Documented
# at developers.paymob.com/paymob-docs/developers/pay-with-saved-cards/mit
# (read 2026-08-29): create an intention against a **Moto** integration, take
# `payment_keys[0].key` from the response, then POST the card token and that
# payment token here.
PAY_PATH: Final = "/api/acceptance/payments/pay"

# The fields a *card token* callback is signed over, in the documented order.
# A different set and therefore a different string from a transaction
# callback - which is why saved cards need their own verification rather than
# being squeezed through `verify_callback`.
# developers.paymob.com/paymob-docs/developers/webhook-callbacks-and-hmac/hmac/hmac-for-card-tokens
# (read 2026-08-29).
TOKEN_HMAC_FIELDS: Final[tuple[str, ...]] = (
    "card_subtype",
    "created_at",
    "email",
    "id",
    "masked_pan",
    "merchant_id",
    "order_id",
    "token",
)

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
    return _digest(hmac_message(transaction), secret=secret)


def token_hmac_message(token: dict[str, Any]) -> str:
    """The exact string Paymob signs for a card-token callback.

    Eight fields rather than twenty, in their own documented order. Public and
    separately testable for the same reason `hmac_message` is: the
    documentation publishes a worked example, and a test pins this function
    against it so a reordered field fails here rather than against a live
    merchant account.
    """
    return "".join(_canonical(_dig(token, field)) for field in TOKEN_HMAC_FIELDS)


def token_hmac_signature(token: dict[str, Any], *, secret: str) -> str:
    """HMAC-SHA512 of the card-token string, hex encoded, as documented."""
    return _digest(token_hmac_message(token), secret=secret)


def _digest(message: str, *, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
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


def _optional_str(value: Any) -> str | None:
    """A payload field as a string, or None when the provider omitted it."""
    if value is None or value == "":
        return None
    return str(value)


def _callback_object(payload: bytes, *, expected: str) -> dict[str, Any]:
    """The `obj` of a callback of the expected `type`, or a refusal.

    Shared by both verification paths so the parsing rules cannot drift apart,
    and so neither can be persuaded to read a body of the wrong kind: a card
    token callback checked against the transaction field list would be
    verifying a signature over the wrong string, which is not verification.
    """
    try:
        document = json.loads(payload)
    except ValueError as error:
        raise CallbackVerificationError("The callback body was not JSON.") from error
    if not isinstance(document, dict):
        raise CallbackVerificationError("The callback body was not an object.")
    if document.get("type") != expected:
        raise CallbackVerificationError(f"The callback was not a {expected} notification.")

    obj = document.get("obj")
    if not isinstance(obj, dict):
        raise CallbackVerificationError("The callback carried no object.")
    return obj


def callback_type(payload: bytes) -> str | None:
    """What kind of callback this is, without authenticating it.

    Read *before* verification purely to choose which signature scheme applies,
    and therefore trusted for nothing else: a body claiming to be a card token
    is still checked against the card-token signature, so lying about the type
    only changes which way it fails.
    """
    try:
        document = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    kind = document.get("type")
    return kind if isinstance(kind, str) else None


def _first_payment_key(intention: dict[str, Any]) -> str | None:
    """`payment_keys[0].key` from an intention response.

    Documented as the token the pay request needs. Read defensively: a 2xx
    without it is a provider failure rather than something to send onward.
    """
    keys = intention.get("payment_keys")
    if not isinstance(keys, list) or not keys:
        return None
    first = keys[0]
    if not isinstance(first, dict):
        return None
    key = first.get("key")
    return key if isinstance(key, str) and key else None


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
        moto_integration_id: int | None = None,
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
        # Separate from `integration_ids` on purpose. Those are the methods a
        # customer may choose at checkout; this one is never offered to anybody
        # - it exists solely so a renewal can be taken from a saved card, and
        # Paymob issues it as a distinct integration type.
        self._moto_integration_id = moto_integration_id
        self._api_base, self._checkout_base = REGIONS[region]
        self._notification_url = notification_url
        self._redirection_url = redirection_url
        self._timeout = timeout_seconds
        # Only a test supplies one. Production leaves it `None` and gets
        # `build_guarded_client`; see `_client`.
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

        payload = await self._post(INTENTION_PATH, body, operation=CHECKOUT)

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

    def _client(self) -> httpx.AsyncClient:
        """The HTTP client every Paymob call uses.

        `build_guarded_client`, the same constructor OpenAI, WhatsApp and Google
        use, so the answer to "which outbound clients are guarded?" is "all of
        them" rather than a list that goes stale (SEC-08). This integration was
        the stale entry: it built a bare `httpx.AsyncClient`, and it is the one
        request that carries the Paymob secret key.

        The URLs are constants from `REGIONS`, so the exposure this closes is
        narrow - a hijacked or poisoned resolver for `accept.paymob.com` and its
        regional siblings. Narrow is not the same as absent, and the guard
        costs nothing: it resolves once, refuses any answer that is not
        publicly routable, and connects to the address it judged rather than to
        a name that can change its mind between the check and the socket.

        What the guard is **not** is a retry policy. It adds no attempts, and
        deliberately: `_post` creates payment intentions and charges saved
        cards, and a transparent retry after a timeout would turn one payment
        request into two financial operations. Retryability stays a *label* on
        `ProviderError` for a caller with idempotency to decide about.

        Redirects stay off, which is the `httpx` default and also what
        `GuardedTransport` assumes - it validates one hop, so a client that
        followed redirects by itself would follow the second one unjudged.
        Paymob's API endpoints are JSON POSTs and do not redirect.

        A `transport` is supplied only by tests, which need a mock socket
        rather than a real one. Production never passes it, so the guarded path
        is the only path a deployment can take.
        """
        timeout = httpx.Timeout(self._timeout)
        if self._transport is not None:
            return httpx.AsyncClient(timeout=timeout, transport=self._transport)
        return build_guarded_client(timeout=timeout)

    async def _post(self, path: str, body: dict[str, Any], *, operation: str) -> dict[str, Any]:
        """One JSON POST, with the credential in exactly one place.

        Every failure leaves as a `ProviderError` with the body truncated:
        Paymob quotes the request back in its errors, and this request carries
        the secret key.

        A destination the guard refuses leaves as a `ProviderError` too, and
        **not** as `UnsafeUrlError`. That exception is deliberately not a
        `WaslaError`, so left uncaught it reaches the unhandled-error handler
        and becomes a 500 - which is the wrong report for "this deployment
        cannot safely reach its payment provider", and it would be raised from
        inside a route. `GoogleOAuthClient.exchange` makes the same catch for
        the same reason. What the caller learns is that Paymob could not be
        reached; which address was refused stays out of the message, following
        the argument `app/core/net.py` gives for keeping it out of its own log
        line.
        """
        url = f"{self._api_base}{path}"
        try:
            async with self._client() as client:
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
            await _count(operation, CallOutcome.UNAVAILABLE)
            raise ProviderError("Paymob did not respond in time.", retryable=True) from error
        except UnsafeUrlError as error:
            # The guarded transport refused the destination: a Paymob host that
            # resolved to an address inside the deployment network, or a base
            # URL that is not https. Not retryable, and that is the difference
            # from the branch below - nothing was sent, and sending it again
            # produces the same refusal, because this is a statement about
            # where the request was pointed rather than about the network.
            logger.error(
                "billing.paymob_destination_refused",
                extra={
                    # No address and no host. The refusal is the thing being
                    # probed for, so the log must not become the oracle.
                    "event": "billing.paymob_destination_refused",
                    "reason": type(error).__name__,
                },
            )
            await _count(operation, CallOutcome.FAILURE)
            raise ProviderError("Paymob could not be reached.") from error
        except httpx.HTTPError as error:
            # Deliberately not interpolating the error: httpx puts the request
            # URL in it, and a future signed URL would end up in a log.
            await _count(operation, CallOutcome.UNAVAILABLE)
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
            await _count(
                operation,
                (
                    CallOutcome.RATE_LIMITED
                    if response.status_code == RATE_LIMITED_STATUS
                    else CallOutcome.FAILURE
                ),
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
            await _count(operation, CallOutcome.FAILURE)
            raise ProviderError("Paymob returned a response that was not JSON.") from error
        if not isinstance(payload, dict):
            raise ProviderError("Paymob returned a response of an unexpected shape.")
        await _count(operation, CallOutcome.SUCCESS)
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

        transaction = _callback_object(payload, expected="TRANSACTION")

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

    @property
    def can_charge_saved_methods(self) -> bool:
        """Whether this deployment can debit a card with nobody present.

        True only when a Moto integration id is configured, because Paymob
        gates merchant-initiated transactions on one: the MIT documentation
        states the intention must use a Moto card integration, and the
        Subscriptions Module says the same of a subscription plan.

        A Moto integration is issued by Paymob per merchant and cannot be
        created from the dashboard alongside the ordinary card types, so this
        being False is an account fact rather than a mistake in configuration.
        Renewals fall back to invoicing the customer, which is how this product
        billed before saved cards existed.
        """
        return self._moto_integration_id is not None

    def verify_token_callback(
        self,
        *,
        payload: bytes,
        signature: str | None,
    ) -> SavedPaymentMethod:
        """Authenticate a saved-card notification and read the card out of it.

        Arrives at the same endpoint as a transaction callback, distinguished
        by `type: "TOKEN"`, and signed over a different set of fields - so it
        cannot be verified by `verify_callback` and must not be, since a
        signature checked against the wrong field list is not a check.

        Fails closed on every path, exactly as the transaction one does.
        """
        document = _callback_object(payload, expected="TOKEN")
        expected = token_hmac_signature(document, secret=self._hmac_secret)
        if not signature or not hmac.compare_digest(expected, signature):
            raise CallbackVerificationError("The card token signature did not match.")

        token = document.get("token")
        if not isinstance(token, str) or not token:
            raise CallbackVerificationError("The card token callback carried no token.")

        return SavedPaymentMethod(
            token=token,
            provider_token_id=str(document.get("id") or ""),
            # The provider's own masking. Four digits, never a card number -
            # see `PaymentMethod`, which has nowhere to put one.
            masked_pan=_optional_str(document.get("masked_pan")),
            brand=_optional_str(document.get("card_subtype")),
            # Ties the card to the checkout that saved it, which is how it is
            # matched to a workspace.
            order_reference=_optional_str(document.get("order_id")),
            email=_optional_str(document.get("email")),
        )

    async def charge_saved_method(self, request: SavedMethodCharge) -> str:
        """Debit a saved card, in the two documented steps.

        An intention against the Moto integration, then the pay request
        carrying the card token and the payment token that intention returned.
        The *outcome* is not here: it arrives at the callback endpoint like any
        other payment, which is why this returns the provider's reference and
        nothing about success.
        """
        if self._moto_integration_id is None:
            raise RecurringUnavailableError(
                "This Paymob account has no Moto integration, which is what "
                "merchant-initiated charges require."
            )

        intention = await self._post(
            INTENTION_PATH,
            {
                "amount": _to_cents(request.amount),
                "currency": request.currency,
                "payment_methods": [self._moto_integration_id],
                "special_reference": request.reference,
                "items": [
                    {
                        "name": request.description,
                        "amount": _to_cents(request.amount),
                        "quantity": 1,
                    }
                ],
                "billing_data": {
                    "email": UNKNOWN_BILLING_FIELD,
                    "first_name": UNKNOWN_BILLING_FIELD,
                    "last_name": UNKNOWN_BILLING_FIELD,
                    "phone_number": UNKNOWN_BILLING_FIELD,
                },
                **({"notification_url": self._notification_url} if self._notification_url else {}),
            },
            operation=MOTO_INTENTION,
        )

        payment_token = _first_payment_key(intention)
        if payment_token is None:
            raise ProviderError("Paymob did not return a payment token for the saved card.")

        paid = await self._post(
            PAY_PATH,
            {
                "source": {"identifier": request.token, "subtype": "TOKEN"},
                "payment_token": payment_token,
            },
            operation=SAVED_CARD_CHARGE,
        )

        reference = paid.get("id")
        if reference is None:
            raise ProviderError("Paymob did not identify the saved-card transaction.")

        logger.info(
            "billing.paymob_saved_method_charged",
            extra={
                "event": "billing.paymob_saved_method_charged",
                "reference": request.reference,
                "transaction_id": str(reference),
                # Never the card token: it is a bearer value for charging that
                # card, and a log is not where it belongs.
            },
        )
        return str(reference)

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
            operation=REFUND,
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


async def _count(operation: str, outcome: CallOutcome) -> None:
    """Record one provider call's outcome. Best-effort by construction.

    Counted here because this is where the outcome is already distinguished:
    a timeout, a refused destination, a 429 and a 4xx are four different
    operational problems and only this method can tell them apart. Nothing
    about the *money* is a label - not the amount, not the currency, not the
    reference - because a payment's value is what a metric label domain must
    never be keyed on.
    """
    await record_provider_call(provider=Provider.PAYMOB, operation=operation, outcome=outcome)
