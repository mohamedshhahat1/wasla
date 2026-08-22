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

from app.integrations.billing.base import (
    ChargeOutcome,
    PaymentProvider,
    ProviderError,
)
from app.integrations.billing.manual import MANUAL_PROVIDER, ManualProvider

__all__ = [
    "MANUAL_PROVIDER",
    "ChargeOutcome",
    "ManualProvider",
    "PaymentProvider",
    "ProviderError",
]
