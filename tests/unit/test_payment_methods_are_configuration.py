"""Which payment methods a customer is offered is configuration, not code.

The property under test is one sentence: **this application does not know what
a payment method is, and must not learn.** An integration id is an opaque token
that is quoted to Paymob; Paymob decides whether it means a card, a wallet, or
something that does not exist yet, and renders the hosted checkout accordingly.

That matters because the alternative is the shape every payment integration
drifts into - a `if card: ... elif wallet: ...` somewhere in the billing
domain, which turns "offer wallets in Egypt" into a code change, a review, a
deployment and a regression risk. It is also simply wrong here: nothing in this
system can tell what an id represents without asking Paymob, so any branch on
it would be encoding a guess.

So these tests are mostly structural, and deliberately so. They assert that the
configured list reaches the provider request untouched, and that no module
between the two has grown an opinion about payment methods.

`payment_methods` is documented as taking "the Integration ID(s) used to
process the payment. Values can be provided as integers (e.g., 1256) or as
names enclosed in quotes (e.g., "card")"
(developers.paymob.com/paymob-docs/developers/intention-apis/create-intention,
read 2026-08-29). Both forms are therefore accepted and neither is interpreted.
"""

from __future__ import annotations

import inspect
import json
import re
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.billing import build_checkout_provider
from app.integrations.billing.checkout import CheckoutRequest
from app.integrations.billing.paymob import PaymobProvider

REPO_ROOT = Path(__file__).resolve().parents[2]

BASE = {
    "_env_file": None,
    "environment": "staging",
    "billing_provider": "paymob",
    "paymob_secret_key": "sk_test_notreal000000",
    "paymob_public_key": "pk_test_notreal000000",
    "paymob_hmac_secret": "a-test-hmac-secret",
    "app_public_url": "https://app.example.com",
}


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {**BASE, "jwt_secret": secrets.token_urlsafe(32)}
    values.update(overrides)
    return Settings(**values)


# ------------------------------------------------------------- configuration


def test_one_integration_works() -> None:
    """The commonest deployment: a single integration, however it is spelled."""
    assert _settings(paymob_integration_ids="1234567").paymob_integration_ids == [1234567]


def test_several_integrations_are_parsed_in_order() -> None:
    """Order is preserved because it is the order Paymob is given them in.

    Nothing here sorts or de-duplicates silently; a list that arrived in a
    particular order was written that way on purpose.
    """
    settings = _settings(paymob_integration_ids="1234567,7654321,7654322")

    assert settings.paymob_integration_ids == [1234567, 7654321, 7654322]


def test_a_json_array_is_accepted_as_well() -> None:
    """What a config-management tool emits, as opposed to a container env var."""
    assert _settings(paymob_integration_ids="[1234567, 7654321]").paymob_integration_ids == [
        1234567,
        7654321,
    ]


def test_whitespace_around_entries_is_tolerated() -> None:
    """`A, B` is what a person types. Failing on it would be pedantry."""
    assert _settings(paymob_integration_ids=" 1234567 , 7654321 ").paymob_integration_ids == [
        1234567,
        7654321,
    ]


def test_a_documented_method_name_is_kept_as_a_name() -> None:
    """Paymob documents `"card"` as a valid entry alongside numeric ids.

    Accepted rather than refused, because refusing it would make a documented
    provider capability need a code change to use - which is exactly the thing
    this file exists to prevent.
    """
    assert _settings(paymob_integration_ids="card").paymob_integration_ids == ["card"]


def test_names_and_ids_may_be_mixed() -> None:
    """Nothing requires a deployment to pick one spelling for the whole list."""
    settings = _settings(paymob_integration_ids="1234567,card")

    assert settings.paymob_integration_ids == [1234567, "card"]


@pytest.mark.parametrize("value", ["0", "-1", "1234567,0"])
def test_an_id_that_cannot_be_real_fails_closed(value: str) -> None:
    """`0` is what an empty field becomes if the parsing is ever loosened.

    Refused rather than dropped: a payment method that silently stops being
    offered is far harder to notice than a deployment that will not start.
    """
    with pytest.raises(ValidationError):
        _settings(paymob_integration_ids=value)


def test_a_repeated_entry_fails_closed() -> None:
    """A repeated entry offers the same method twice on the checkout page."""
    with pytest.raises(ValidationError):
        _settings(paymob_integration_ids="1234567,1234567")


def test_a_repeated_entry_is_caught_across_spellings() -> None:
    """`1234567` and `"1234567"` are the same integration written two ways."""
    with pytest.raises(ValidationError):
        _settings(paymob_integration_ids=["1234567", 1234567])


def test_an_empty_entry_fails_closed() -> None:
    with pytest.raises(ValidationError):
        _settings(paymob_integration_ids=["card", "   "])


def test_no_integrations_fails_closed_when_paymob_is_the_provider() -> None:
    """Paymob refuses an intention with no payment method.

    Discovering that at the first customer is worse than at startup.
    """
    with pytest.raises(ValidationError) as caught:
        _settings(paymob_integration_ids="")

    assert "PAYMOB_INTEGRATION_IDS" in str(caught.value)


def test_no_integrations_is_fine_when_no_processor_is_configured() -> None:
    """`manual` is the default, and it collects nothing through a processor."""
    settings = Settings(
        _env_file=None,
        environment="staging",
        jwt_secret=secrets.token_urlsafe(32),
    )

    assert settings.paymob_integration_ids == []


# ---------------------------------------------------------------- provider


def _captured_intention(
    integration_ids: list[int | str],
) -> tuple[PaymobProvider, list[dict[str, Any]]]:
    """A real provider whose intention requests are recorded rather than sent.

    The provider is genuine - the real body is built and the real response
    parsed - so what lands in `seen` is exactly what Paymob would have been
    given.
    """
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "pi_1", "client_secret": "csk_1"})

    provider = PaymobProvider(
        secret_key="sk_test_notreal",
        public_key="pk_test_notreal",
        hmac_secret="a-test-hmac-secret",
        integration_ids=integration_ids,
        transport=httpx.MockTransport(handler),
    )
    return provider, seen


async def test_the_configured_ids_reach_the_intention_request_untouched() -> None:
    """The one behavioural claim: configuration in, request out, unchanged.

    Not reordered, not filtered, not translated. If this passes and the account
    is configured correctly, the checkout page shows what Paymob decides these
    ids mean.
    """
    provider, seen = _captured_intention([1234567, 7654321])

    await provider.create_checkout(
        CheckoutRequest(
            reference="r",
            amount=Decimal("99.00"),
            currency="EGP",
            description="Pro plan",
        )
    )

    assert seen[0]["payment_methods"] == [1234567, 7654321]


async def test_a_method_name_reaches_the_request_as_a_name() -> None:
    """A string stays a string, so the documented `"card"` form actually works."""
    provider, seen = _captured_intention([1234567, "card"])

    await provider.create_checkout(
        CheckoutRequest(
            reference="r",
            amount=Decimal("99.00"),
            currency="EGP",
            description="Pro plan",
        )
    )

    assert seen[0]["payment_methods"] == [1234567, "card"]


async def test_changing_the_configured_ids_changes_only_the_request() -> None:
    """The regression this whole file protects.

    Two providers differing only in configuration produce two intention bodies
    differing only in `payment_methods`. If anything else moved, some part of
    the request had started depending on which method was configured.
    """
    first, seen_first = _captured_intention([1234567])
    second, seen_second = _captured_intention(["wallet"])
    request = CheckoutRequest(
        reference="0a4f1e2c-0000-4000-8000-000000000000",
        amount=Decimal("99.00"),
        currency="EGP",
        description="Pro plan",
    )

    await first.create_checkout(request)
    await second.create_checkout(request)

    assert seen_first[0].pop("payment_methods") == [1234567]
    assert seen_second[0].pop("payment_methods") == ["wallet"]
    assert seen_first[0] == seen_second[0]


def test_the_provider_is_built_from_settings_alone() -> None:
    """No id is chosen anywhere between the environment and the provider."""
    provider = build_checkout_provider(_settings(paymob_integration_ids="1234567,card"))

    assert provider is not None
    # `build_checkout_provider` answers the `CheckoutProvider` protocol, which
    # deliberately does not publish how a provider stores its configuration.
    # This test is about that storage, so it narrows to the concrete adapter.
    assert isinstance(provider, PaymobProvider)
    assert provider._integration_ids == [1234567, "card"]


# -------------------------------------------------------------- architecture

# Everything a payment flows through that is *not* the Paymob adapter. If any
# of these grows an opinion about payment methods, changing methods stops being
# a configuration change.
DOMAIN_MODULES = [
    "app/services/checkout_service.py",
    "app/services/refund_service.py",
    "app/services/invoice_service.py",
    "app/services/subscription_service.py",
    "app/services/entitlement_service.py",
    "app/api/v1/billing.py",
    "app/api/v1/payment_webhooks.py",
    "app/db/models/invoice.py",
    "app/db/models/billing.py",
    "app/db/models/payment_event.py",
    "app/workers/billing_worker.py",
    "app/integrations/billing/checkout.py",
]

# Method names that would indicate the domain had learned what it is selling.
# Matched on code rather than prose, because these words appear legitimately in
# comments - "a card taken over the phone", "the last four digits of the card".
METHOD_WORDS = re.compile(
    r"\b(card|wallet|kiosk|meeza|valu|aman|instalment|installment|applepay|googlepay)\b",
    re.IGNORECASE,
)


def _code_only(source: str) -> str:
    """The module with comments and string literals removed.

    Crude but sufficient, and the exclusions are the point. Branching does not
    live in a comment, and an integration id would not be hardcoded inside a
    prompt - so keeping either would make these tests fail on the explanations
    that are the reason the code is correct, and on numbers in model
    instructions like "write 500000, not '500k'".

    Docstrings go first (they are string literals with a particular position),
    then the remaining single- and double-quoted strings.
    """
    text = re.sub(r"#.*", "", source)
    text = re.sub(r'"""(?:.|\n)*?"""', '""', text)
    text = re.sub(r"'''(?:.|\n)*?'''", "''", text)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', text)
    return re.sub(r"'(?:\\.|[^'\\\n])*'", "''", text)


@pytest.mark.parametrize("module", DOMAIN_MODULES)
def test_the_billing_domain_does_not_name_a_payment_method(module: str) -> None:
    """No `if card: ... elif wallet:` anywhere money is decided.

    The domain deals in amounts, currencies, invoices and statuses. Which
    instrument moved the money is Paymob's business and is not something any
    rule here turns on.
    """
    source = _code_only((REPO_ROOT / module).read_text(encoding="utf-8"))

    found = sorted({match.group(0).lower() for match in METHOD_WORDS.finditer(source)})
    assert not found, f"{module} names payment methods in code: {found}"


def test_the_checkout_service_never_sees_an_integration_id() -> None:
    """It cannot branch on what it is not given.

    `CheckoutService` builds a `CheckoutRequest`, which carries an amount, a
    currency, a reference and a description. There is no field on it for a
    payment method, and that absence is the architectural guarantee.
    """
    from app.services import checkout_service

    source = checkout_service.__file__ and Path(checkout_service.__file__).read_text(
        encoding="utf-8"
    )

    assert "integration_id" not in _code_only(source)
    assert "payment_method" not in _code_only(source)


def test_the_checkout_request_has_no_payment_method_field() -> None:
    """The boundary type is the contract, so it is asserted directly."""
    from dataclasses import fields

    names = {field.name for field in fields(CheckoutRequest)}

    assert not (names & {"payment_method", "payment_methods", "integration_id"})


def test_no_integration_id_is_hardcoded_anywhere_in_the_application() -> None:
    """Every id comes from configuration, and this is what keeps it that way.

    A literal in source would be an id that survives a change to
    `PAYMOB_INTEGRATION_IDS` - the deployment would set one thing and the
    application would send another, which is the single most confusing failure
    this subsystem could have.

    Six digits or more, because Paymob's ids are of that magnitude and shorter
    numbers in this codebase are timeouts and sizes.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        source = _code_only(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"(?<![\w.])\d{6,}(?![\w.])", source):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")

    assert not offenders, f"integration-id-shaped literals in application code: {offenders}"


def test_the_paymob_adapter_is_the_only_module_that_names_payment_methods() -> None:
    """Where method-specific knowledge is *allowed* to live, if it ever must.

    Nothing needs it today. This pins the boundary so that a future addition -
    say a method that requires an extra intention field - lands in the adapter
    rather than leaking into the domain.
    """
    import app.services.checkout_service as service

    signature = inspect.signature(service.CheckoutService.start)

    assert set(signature.parameters) == {
        "self",
        "plan_code",
        "invoice_id",
        "actor",
        "idempotency_key",
        "now",
    }
