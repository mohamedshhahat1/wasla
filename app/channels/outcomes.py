"""What a provider did with a send, as the types a caller acts on.

These used to live in the WhatsApp client (OMNI-020), and nothing about them is
WhatsApp's: every provider Wasla will ever send through can refuse a request
before reading it, refuse the credential, or fail in a way nobody can see past.
The distinctions are ADR-093's and they decide what may happen next:

| Type | Delivered? | May the caller send again? |
| --- | --- | --- |
| `SendNotAttemptedError` | provably not | yes, as a new message |
| `ProviderAuthError` | provably not | not until the credential is fixed |
| `UncertainDeliveryError` | unknown | **never** on its own initiative |
| `RateLimitedError` (core) | provably not | later |

An adapter maps its provider's failures onto these and nothing else. The WhatsApp
client re-exports them under their old import path, so every `isinstance` and
`except` written against it still holds - they are the same classes.

Retry classification in the workers is by these types and never by message
text, which is what keeps a provider's error prose out of every decision.
"""

from __future__ import annotations

from app.core.exceptions import ExternalServiceError


class SendNotAttemptedError(ExternalServiceError):
    """The request provably never reached the provider, or was read and declined.

    Raised only where nothing can have been delivered: a connection that was
    never established, or a rejection issued before the message was read. The
    caller may record the send as undelivered and, if it wants to, make a new
    one - which is a decision it cannot safely take after any other failure
    (ADR-093).
    """


class UncertainDeliveryError(ExternalServiceError):
    """The provider may or may not have accepted the request, and nobody can tell.

    A read timeout, a reset connection, a 5xx, a success that named no message.
    The request left this process and no usable answer came back, and no
    provider in scope publishes a way to ask what became of it. So this is
    terminal by construction: the one thing that must not follow it is another
    send.
    """


class ProviderAuthError(SendNotAttemptedError):
    """The provider refused the credential, so nothing was sent and nothing will be.

    A subclass of `SendNotAttemptedError` because that is the truth about this
    message - declined before reading, nothing reached a customer. A type of its
    own because the truth about the *connection* is different: every other
    recipient fails identically until somebody reconnects it, so a sweep lets
    this one out rather than discovering it once per person (MSG-18), and the
    connection's health records it (OMNI-012).

    Carries no credential material, here or in its message.
    """

    message = "The provider refused this connection's credentials."


__all__ = [
    "ProviderAuthError",
    "SendNotAttemptedError",
    "UncertainDeliveryError",
]
