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
)

logger = get_logger(__name__)

PAYMOB_PROVIDER: Final = "paymob"
DEFAULT_TIMEOUT_SECONDS: Final = 20.0
# Enough of a provider error to act on, too little to carry a credential back
# out through a log. Paymob error bodies quote the request.
MAX_ERROR_LENGTH: Final = 300

# The regions Paymob publishes, each with its API base and its checkout host.
# Both are needed and they are not the same host - see the module docstring.
REGIONS: Final[dict[str, tuple[str, str]]] = {
    "egypt": ("https://accept.paymob.com", "https://eg.checkout.paymob.com"),
    "uae": ("https://uae.paymob.com", "https://uae.checkout.paymob.com"),
    "oman": ("https://oman.paymob.com", "https://om.checkout.paymob.com"),
    "saudi": ("https://ksa.paymob.com", "https://ksa.checkout.paymob.com"),
}

INTENTION_PATH: Final = "/v1/intention/"

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


class PaymobProvider:
    """Creates Paymob intentions and authenticates Paymob callbacks."""

    def __init__(
        self,
        *,
        secret_key: str,
        public_key: str,
        hmac_secret: str,
        integration_ids: list[int],
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
            "payment_methods": self._integration_ids,
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

        billing = self._billing_data(request)
        if billing:
            body["billing_data"] = billing

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
        """The little Paymob shows on the checkout page.

        Only what the account already holds. This product does not collect a
        customer's address or telephone number, and inventing placeholder
        values to fill a provider's optional fields would be putting fiction
        onto somebody else's systems.
        """
        data: dict[str, str] = {}
        if request.customer_email:
            data["email"] = request.customer_email
        if request.customer_name:
            first, _, last = request.customer_name.partition(" ")
            data["first_name"] = first
            if last:
                data["last_name"] = last
        return data

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
            raise ProviderError("Paymob did not respond in time.") from error
        except httpx.HTTPError as error:
            # Deliberately not interpolating the error: httpx puts the request
            # URL in it, and a future signed URL would end up in a log.
            raise ProviderError("Paymob could not be reached.") from error

        if response.status_code >= 400:
            detail = response.text[:MAX_ERROR_LENGTH]
            logger.warning(
                "billing.paymob_rejected",
                extra={
                    "event": "billing.paymob_rejected",
                    "status_code": response.status_code,
                },
            )
            raise ProviderError(f"Paymob refused the request ({response.status_code}): {detail}")

        try:
            payload = response.json()
        except ValueError as error:
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

        The status mapping is the only judgement here, and it is deliberately
        strict: **only an unambiguous success is a success.** `success` true
        while `pending` is also true is a transaction still in flight, and
        treating it as collected would activate a subscription for money that
        has not moved. Everything that is not a clean success and not still
        pending is a failure, because a payment that is neither succeeded nor
        in progress did not happen.
        """
        success = bool(transaction.get("success"))
        pending = bool(transaction.get("pending"))
        error_occured = bool(transaction.get("error_occured"))

        if success and not pending and not error_occured:
            status = PaymentStatus.SUCCEEDED
        elif pending and not error_occured:
            status = PaymentStatus.PENDING
        else:
            status = PaymentStatus.FAILED

        # `is_refunded` and `is_voided` describe a payment that was collected
        # and then given back. Reported as refunded so a caller can tell it
        # apart from a payment that never succeeded.
        if transaction.get("is_refunded") or transaction.get("is_voided"):
            status = PaymentStatus.REFUNDED

        order = transaction.get("order")
        reference = order.get("merchant_order_id") if isinstance(order, dict) else None

        amount_cents = transaction.get("amount_cents")
        amount = Decimal(int(amount_cents)) / 100 if isinstance(amount_cents, int) else Decimal("0")

        failure_reason: str | None = None
        if status is PaymentStatus.FAILED:
            data = transaction.get("data")
            if isinstance(data, dict):
                message = data.get("message")
                if isinstance(message, str):
                    failure_reason = message[:200]

        return CallbackEvent(
            event_id=str(transaction.get("id")),
            reference=str(reference) if reference else None,
            status=status,
            amount=amount,
            currency=str(transaction.get("currency") or ""),
            provider_payment_id=str(transaction.get("id")),
            failure_reason=failure_reason,
        )
