"""The payment provider boundary.

Wasla charges nobody yet, and this package is what makes that a configuration
rather than a gap in the design. `PaymentProvider` is the whole surface a real
processor has to implement; `ManualProvider` implements it by recording what a
human did, which is how a business actually collects today — a bank transfer, an
invoice emailed to a finance department, a card taken over the phone.

Nothing above this package knows a provider exists beyond its name. The billing
service issues invoices, asks the provider to collect, and records what came
back; swapping the manual provider for a real one changes which object is
constructed and nothing else (ADR-031).
"""

from app.core.config import Settings
from app.integrations.billing.base import (
    ChargeOutcome,
    PaymentProvider,
    ProviderError,
)
from app.integrations.billing.checkout import (
    CallbackEvent,
    CallbackVerificationError,
    CheckoutProvider,
    CheckoutRequest,
    CheckoutSession,
    EventKind,
    RefundOutcome,
    RefundRequest,
)
from app.integrations.billing.manual import MANUAL_PROVIDER, ManualProvider
from app.integrations.billing.paymob import PAYMOB_PROVIDER, PaymobProvider

# Where a provider's callbacks arrive. A literal, because it is half of the URL
# handed to the provider at intention time and half of the route decorator, and
# the two silently disagreeing is a payment nobody is ever told about.
CALLBACK_PATH = "/api/v1/webhooks/paymob"


def build_checkout_provider(settings: Settings) -> CheckoutProvider | None:
    """The hosted-checkout provider this deployment is configured for, or None.

    None is a real and supported state rather than a failure: it means
    `BILLING_PROVIDER` is `manual`, which is how this product has always billed
    and how every local deployment and test still does. The checkout endpoint
    refuses in that state, and nothing else changes.

    Credentials are not re-validated here. `Settings` already refuses to build
    when `BILLING_PROVIDER=paymob` and any of them is missing, so a deployment
    reaching this function with a half-configured provider does not exist -
    checking again would be a second, quieter rule that could disagree with the
    first.
    """
    if settings.billing_provider != "paymob":
        return None

    # Narrowed for the type checker: the settings validator has already made
    # these non-None, and `assert` would vanish under -O.
    secret_key = settings.paymob_secret_key or ""
    public_key = settings.paymob_public_key or ""
    hmac_secret = settings.paymob_hmac_secret or ""
    public_url = (settings.app_public_url or "").rstrip("/")

    return PaymobProvider(
        secret_key=secret_key,
        public_key=public_key,
        hmac_secret=hmac_secret,
        integration_ids=list(settings.paymob_integration_ids),
        region=settings.paymob_region,
        # Built from configuration and a literal path, never from a request
        # Host header - a callback URL an attacker can aim is a callback that
        # never reaches us and a payment that is never recorded.
        notification_url=f"{public_url}{CALLBACK_PATH}" if public_url else None,
        redirection_url=f"{public_url}/billing/checkout/return" if public_url else None,
    )


__all__ = [
    "CALLBACK_PATH",
    "MANUAL_PROVIDER",
    "PAYMOB_PROVIDER",
    "CallbackEvent",
    "CallbackVerificationError",
    "ChargeOutcome",
    "CheckoutProvider",
    "CheckoutRequest",
    "CheckoutSession",
    "EventKind",
    "ManualProvider",
    "PaymentProvider",
    "PaymobProvider",
    "ProviderError",
    "RefundOutcome",
    "RefundRequest",
    "build_checkout_provider",
]
