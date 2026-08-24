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


__all__ = [
    "EmailMessage",
    "EmailProvider",
    "EmailSendResult",
    "EmailSendState",
    "FakeEmailProvider",
    "ResendEmailProvider",
    "build_email_provider",
]
