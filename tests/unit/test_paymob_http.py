"""What the provider does at the HTTP boundary, against a mock transport.

The real `PaymobProvider` runs here - the real intention body is built, the
real URL is assembled, the real response is parsed. Only the socket is
replaced. A test double in place of the provider would prove that something
gets called, which is not the part that goes wrong.

Two things are being pinned.

**The addresses.** Paymob's API host and its checkout host are different
hosts, and an integration that assumed one for both would create intentions
perfectly and send every customer to a 404. That is not discoverable without
credentials, so it is asserted against the documented values here.

**The failure classification.** Every call has three ways to end - an answer,
no answer, and a refusal - and telling them apart is what decides whether a
retry is safe or a loop. A timeout may mean the request *was* carried out; a
400 means it never will be.

Nothing in this file reaches a network. `httpx.MockTransport` answers every
request, so a test that accidentally pointed at the real Paymob would fail to
connect rather than quietly succeed against somebody's live merchant account.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.integrations.billing.base import ProviderError
from app.integrations.billing.checkout import CheckoutRequest, RefundRequest
from app.integrations.billing.paymob import (
    INTENTION_PATH,
    REFUND_PATH,
    REGIONS,
    UNKNOWN_BILLING_FIELD,
    PaymobProvider,
)

SECRET_KEY = "sk_test_notreal000000000000"
PUBLIC_KEY = "pk_test_notreal000000000000"
CLIENT_SECRET = "egy_csk_test_0123456789abcdef"
HMAC_SECRET = "a-test-hmac-secret"


def _provider(handler, **overrides) -> PaymobProvider:
    settings = {
        "secret_key": SECRET_KEY,
        "public_key": PUBLIC_KEY,
        "hmac_secret": HMAC_SECRET,
        "integration_ids": [4097558],
        "transport": httpx.MockTransport(handler),
    }
    settings.update(overrides)
    return PaymobProvider(**settings)  # type: ignore[arg-type]


def _intention_ok(seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(201, json={"id": "pi_test_1", "client_secret": CLIENT_SECRET})

    return handler


def _refund_ok(seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        # Shaped like the documented refund response: a new transaction, whose
        # id is what a later callback about this reversal carries.
        return httpx.Response(
            200,
            json={
                "id": 579305,
                "success": True,
                "pending": False,
                "amount_cents": 1000,
                "has_parent_transaction": True,
            },
        )

    return handler


def _request() -> CheckoutRequest:
    return CheckoutRequest(
        reference="0a4f1e2c-0000-4000-8000-000000000000",
        amount=Decimal("99.00"),
        currency="EGP",
        description="Pro plan",
    )


# ------------------------------------------------------------------ addresses


async def test_the_intention_goes_to_the_documented_api_host() -> None:
    seen: list[httpx.Request] = []

    await _provider(_intention_ok(seen)).create_checkout(_request())

    assert str(seen[0].url) == f"https://accept.paymob.com{INTENTION_PATH}"


async def test_the_customer_is_sent_to_the_checkout_host_not_the_api_host() -> None:
    """The mistake that would break every payment while looking correct.

    Intentions are created on `accept.paymob.com` and customers pay on
    `eg.checkout.paymob.com`. An integration using one host for both creates
    intentions successfully - so every test of *this* call passes - and sends
    every customer to a page that does not exist.
    """
    session = await _provider(_intention_ok()).create_checkout(_request())

    assert session.redirect_url.startswith("https://eg.checkout.paymob.com/?")
    assert "accept.paymob.com" not in session.redirect_url


@pytest.mark.parametrize("region", sorted(REGIONS))
async def test_every_region_uses_a_different_host_for_paying_than_for_asking(
    region: str,
) -> None:
    """Asserted for all four rather than for Egypt, since only one is exercised.

    A region added with the API host copied into both slots would send that
    country's customers to a 404 and nothing else would notice.
    """
    api_host, checkout_host = REGIONS[region]

    assert api_host != checkout_host
    assert api_host.startswith("https://")
    assert checkout_host.startswith("https://")


async def test_the_checkout_url_carries_the_public_key_and_never_the_secret() -> None:
    """The two keys are one character apart in a name and opposite in kind.

    The public key is meant to be in a URL a browser follows. The secret key
    authenticates us to Paymob and would let anybody who read a server log
    create charges.
    """
    session = await _provider(_intention_ok()).create_checkout(_request())

    assert f"publicKey={PUBLIC_KEY}" in session.redirect_url
    assert SECRET_KEY not in session.redirect_url


async def test_the_secret_key_authenticates_the_request_and_appears_nowhere_else() -> None:
    seen: list[httpx.Request] = []

    await _provider(_intention_ok(seen)).create_checkout(_request())

    assert seen[0].headers["Authorization"] == f"Token {SECRET_KEY}"
    assert SECRET_KEY not in seen[0].content.decode()
    assert SECRET_KEY not in str(seen[0].url)


async def test_the_amount_is_sent_in_integer_cents() -> None:
    seen: list[httpx.Request] = []

    await _provider(_intention_ok(seen)).create_checkout(_request())

    assert json.loads(seen[0].read())["amount"] == 9900


async def test_a_fraction_of_a_cent_is_refused_rather_than_rounded() -> None:
    """Rounding somebody's money silently is worse than refusing to charge it.

    It would also fail later anyway: the callback check compares the reported
    amount against the invoice, so a rounded charge is a payment that arrives
    and cannot be applied.
    """
    with pytest.raises(ProviderError):
        await _provider(_intention_ok()).create_checkout(
            CheckoutRequest(
                reference="r",
                amount=Decimal("9.999"),
                currency="EGP",
                description="Pro plan",
            )
        )


# ------------------------------------------------------- failure classification


async def test_a_timeout_is_retryable() -> None:
    """No answer, so the request may have been carried out. Ask again."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(ProviderError) as caught:
        await _provider(handler).create_checkout(_request())

    assert caught.value.retryable


async def test_a_connection_failure_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as caught:
        await _provider(handler).create_checkout(_request())

    assert caught.value.retryable


@pytest.mark.parametrize("status_code", [500, 502, 503, 504, 429])
async def test_their_failures_and_their_backpressure_are_retryable(status_code: int) -> None:
    """A 5xx may pass; a 429 is an explicit "later"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "nope"})

    with pytest.raises(ProviderError) as caught:
        await _provider(handler).create_checkout(_request())

    assert caught.value.retryable


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
async def test_a_refusal_is_not_retried(status_code: int) -> None:
    """Bad credentials and a malformed request do not improve on repetition.

    Retrying a 401 is a loop that ends when somebody reads the log, and in the
    meantime it looks exactly like an outage.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "no"})

    with pytest.raises(ProviderError) as caught:
        await _provider(handler).create_checkout(_request())

    assert not caught.value.retryable


@pytest.mark.parametrize("secret", [SECRET_KEY, HMAC_SECRET])
async def test_a_provider_error_does_not_quote_our_credentials_back(secret: str) -> None:
    """Paymob quotes the request in its errors, and the request carries a key.

    Truncating the body bounds how much of a provider's text comes back and
    does nothing about *what*: a credential echoed in the first two hundred
    characters survives truncation perfectly. The error is carried into an
    exception message and from there into a log, so the values this process
    holds are taken back out of it first.

    The HMAC secret is checked too, because an error about the wrong key is
    exactly the error most likely to quote one.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"invalid request with Token {secret} and more")

    with pytest.raises(ProviderError) as caught:
        await _provider(handler).create_checkout(_request())

    assert secret not in str(caught.value)
    assert "[redacted]" in str(caught.value)


# ------------------------------------------------------------ malformed answers


async def test_a_success_without_a_client_secret_is_a_provider_failure() -> None:
    """A 2xx missing the one field the whole flow needs.

    Parsing around it would build a checkout URL with `clientSecret=None` in
    it, which is a broken page and a payment that can never arrive.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "pi_test_1"})

    with pytest.raises(ProviderError):
        await _provider(handler).create_checkout(_request())


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'"a string"', b"null"],
)
async def test_a_response_of_the_wrong_shape_is_a_provider_failure(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(ProviderError):
        await _provider(handler).create_checkout(_request())


def test_a_provider_with_no_integration_ids_refuses_to_exist() -> None:
    """Paymob refuses an intention with no payment method.

    Discovered at construction rather than at the first customer, which is the
    difference between a deployment that will not start and one that takes
    money nowhere.
    """
    with pytest.raises(ProviderError):
        PaymobProvider(
            secret_key=SECRET_KEY,
            public_key=PUBLIC_KEY,
            hmac_secret="s",
            integration_ids=[],
        )


def test_an_unknown_region_refuses_to_exist() -> None:
    with pytest.raises(ProviderError):
        PaymobProvider(
            secret_key=SECRET_KEY,
            public_key=PUBLIC_KEY,
            hmac_secret="s",
            integration_ids=[1],
            region="atlantis",
        )


def test_the_repr_does_not_carry_a_credential() -> None:
    """`repr` ends up in exception messages, debuggers and log records.

    A dataclass-style default would print every constructor argument, which is
    three secrets.
    """
    provider = PaymobProvider(
        secret_key=SECRET_KEY,
        public_key=PUBLIC_KEY,
        hmac_secret="a-test-hmac-secret",
        integration_ids=[1],
    )

    rendered = repr(provider)
    assert SECRET_KEY not in rendered
    assert "a-test-hmac-secret" not in rendered


# -------------------------------------------------------------------- refunds


async def test_a_refund_goes_to_the_documented_endpoint_with_the_secret_key() -> None:
    seen: list[httpx.Request] = []

    await _provider(_refund_ok(seen)).refund(
        RefundRequest(transaction_reference="192036465", amount=Decimal("99.00"), currency="EGP")
    )

    assert str(seen[0].url) == f"https://accept.paymob.com{REFUND_PATH}"
    assert seen[0].headers["Authorization"] == f"Token {SECRET_KEY}"


async def test_a_refund_sends_the_transaction_and_the_amount_in_cents() -> None:
    seen: list[httpx.Request] = []

    await _provider(_refund_ok(seen)).refund(
        RefundRequest(transaction_reference="192036465", amount=Decimal("10.00"), currency="EGP")
    )

    body = json.loads(seen[0].read())
    assert body == {"transaction_id": "192036465", "amount_cents": 1000}


async def test_a_refund_returns_the_reversal_transaction_not_the_original() -> None:
    """They are different transactions, and the difference is load-bearing.

    The callback reporting this reversal carries the reversal's id, so storing
    the original would leave nothing to tie that callback back to the request
    that caused it.
    """
    outcome = await _provider(_refund_ok()).refund(
        RefundRequest(transaction_reference="192036465", amount=Decimal("10.00"), currency="EGP")
    )

    assert outcome.provider_reference == "579305"
    assert outcome.provider_reference != "192036465"


async def test_a_two_hundred_that_says_it_failed_is_treated_as_a_refusal() -> None:
    """Reading past it would tell a customer their money is coming back.

    A refund the provider declined and a refund it accepted look identical at
    the HTTP layer; only `success` tells them apart.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 579305, "success": False})

    with pytest.raises(ProviderError):
        await _provider(handler).refund(
            RefundRequest(
                transaction_reference="192036465",
                amount=Decimal("10.00"),
                currency="EGP",
            )
        )


async def test_a_refund_without_a_transaction_id_is_a_provider_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    with pytest.raises(ProviderError):
        await _provider(handler).refund(
            RefundRequest(
                transaction_reference="192036465",
                amount=Decimal("10.00"),
                currency="EGP",
            )
        )


async def test_a_refund_timeout_is_retryable() -> None:
    """And safe to retry, because the caller stores nothing until it succeeds.

    A refund that timed out may have been carried out. The service leaves
    `refund_reference` empty in that case, so the operation can be repeated -
    and Paymob is asked about the same transaction rather than a new one.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as caught:
        await _provider(handler).refund(
            RefundRequest(
                transaction_reference="192036465",
                amount=Decimal("10.00"),
                currency="EGP",
            )
        )

    assert caught.value.retryable


# ------------------------------------------------------------- billing data


async def test_the_required_billing_keys_are_always_present() -> None:
    """Found against the live API, not here, which is the point of the test.

    Paymob validates `billing_data` rather than treating it as decoration. An
    intention carrying a partial one is refused with
    `{"billing_data":{"phone_number":["This field is required."]}}` - and every
    mocked test passed against the version that sent only the fields we had,
    because a mock transport accepts whatever it is given.

    So this pins the *shape of the request* rather than the provider's
    reaction, which is the only part a test without credentials can hold.
    """
    seen: list[httpx.Request] = []

    await _provider(_intention_ok(seen)).create_checkout(
        CheckoutRequest(
            reference="r",
            amount=Decimal("25.00"),
            currency="EGP",
            description="Pro plan",
            customer_email="owner@example.com",
            customer_name="Ada Lovelace",
        )
    )

    billing = json.loads(seen[0].read())["billing_data"]
    assert set(billing) >= {"email", "first_name", "last_name", "phone_number"}
    assert billing["email"] == "owner@example.com"
    assert billing["first_name"] == "Ada"
    assert billing["last_name"] == "Lovelace"


async def test_a_field_we_do_not_collect_is_sent_as_an_obvious_placeholder() -> None:
    """Wasla holds no telephone number, and Paymob requires one.

    A placeholder is the only option other than not taking card payments, and
    it is spelled to be unmistakable in Paymob's dashboard so nobody reading a
    transaction takes it for a customer's real number.
    """
    seen: list[httpx.Request] = []

    await _provider(_intention_ok(seen)).create_checkout(
        CheckoutRequest(
            reference="r",
            amount=Decimal("25.00"),
            currency="EGP",
            description="Pro plan",
        )
    )

    billing = json.loads(seen[0].read())["billing_data"]
    assert billing["phone_number"] == UNKNOWN_BILLING_FIELD
    # No account details at all, so every field falls back rather than the
    # block being omitted - an omitted block is a refused intention.
    assert billing["email"] == UNKNOWN_BILLING_FIELD
    assert billing["first_name"] == UNKNOWN_BILLING_FIELD


async def test_a_single_word_name_still_fills_both_name_fields() -> None:
    """`"Ada".partition(" ")` leaves the surname empty, and Paymob wants one."""
    seen: list[httpx.Request] = []

    await _provider(_intention_ok(seen)).create_checkout(
        CheckoutRequest(
            reference="r",
            amount=Decimal("25.00"),
            currency="EGP",
            description="Pro plan",
            customer_name="Ada",
        )
    )

    billing = json.loads(seen[0].read())["billing_data"]
    assert billing["first_name"] == "Ada"
    assert billing["last_name"] == UNKNOWN_BILLING_FIELD


async def test_no_address_is_ever_sent() -> None:
    """Wasla collects none, and a payments integration is not where that starts.

    An address field here would be a data-protection question rather than a
    payments one, so the absence is deliberate and worth pinning.
    """
    seen: list[httpx.Request] = []

    await _provider(_intention_ok(seen)).create_checkout(
        CheckoutRequest(
            reference="r",
            amount=Decimal("25.00"),
            currency="EGP",
            description="Pro plan",
            customer_email="owner@example.com",
        )
    )

    billing = json.loads(seen[0].read())["billing_data"]
    assert not (set(billing) & {"street", "building", "apartment", "floor", "city", "state"})
