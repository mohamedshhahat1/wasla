"""Email delivery, behind one abstraction.

Application and domain code never name a provider. Everything that sends
speaks `EmailMessage` and receives `EmailSendResult`; which provider is behind
that is a configuration value resolved here and nowhere else.
"""

from __future__ import annotations

from app.core.config import Settings
from app.integrations.email.base import (
    EmailMessage,
    EmailProvider,
    EmailSendResult,
    EmailSendState,
)
from app.integrations.email.fake import FakeEmailProvider
from app.integrations.email.resend import ResendEmailProvider


def build_email_provider(settings: Settings) -> EmailProvider:
    """Construct the configured provider, refusing an unusable configuration.

    Called by the process that sends - the email worker - at construction
    time, so a missing credential is a container that fails fast at startup
    rather than an outbox that silently accumulates rows. The check lives here
    rather than in `Settings` validation deliberately: only the sending
    process needs the credential, and requiring it globally would force the
    API container to carry a secret it never uses.
    """
    if settings.email_provider == "fake":
        return FakeEmailProvider()
    if not settings.resend_api_key:
        raise ValueError(
            "RESEND_API_KEY is required to send email: EMAIL_ENABLED is true and "
            "EMAIL_PROVIDER is resend, so the sending process cannot start without it"
        )
    return ResendEmailProvider(api_key=settings.resend_api_key)


def require_delivery_verification(settings: Settings) -> None:
    """Refuse an API process that could not verify a delivery event.

    The mirror of `build_email_provider`, and here for the same reason. Only
    the process that *serves the webhook* needs RESEND_WEBHOOK_SECRET, so the
    requirement belongs to that process rather than to `Settings` - a check in
    the shared settings model runs in every container and therefore obliges the
    worker to carry a secret it never reads (ADR-063).

    The guarantee is unchanged: a production API with email on still refuses to
    start without it. Without the secret the delivery-event endpoint answers
    503 to every call, so bounces and complaints are never recorded and the
    platform keeps writing to dead mailboxes until the sending domain is the
    thing that fails.

    Production only, matching the setting it replaces. A local deployment
    exercises sending through the fake provider and has nothing to verify.
    """
    if not settings.email_enabled or not settings.is_production:
        return
    if not settings.resend_webhook_secret:
        raise ValueError(
            "RESEND_WEBHOOK_SECRET must be set so delivery events can be "
            "verified; without it bounces and complaints are never recorded"
        )


__all__ = [
    "EmailMessage",
    "EmailProvider",
    "EmailSendResult",
    "EmailSendState",
    "FakeEmailProvider",
    "ResendEmailProvider",
    "build_email_provider",
    "require_delivery_verification",
]
