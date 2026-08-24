"""A provider that delivers nothing and remembers everything.

What the suite and local development run against. Deterministic on purpose:
message ids are sequential, outcomes are scripted, and every accepted message
is kept so a test can assert exactly what would have been sent - which is the
assertion that matters, since the body is where a leaked token would live.
"""

from __future__ import annotations

from app.integrations.email.base import EmailMessage, EmailSendResult, EmailSendState


class FakeEmailProvider:
    """Records sends; answers with scripted results or deterministic success."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        # Results to answer with, consumed in order. Empty means every send
        # succeeds, which is the common case a test wants.
        self.script: list[EmailSendResult] = []

    @property
    def name(self) -> str:
        return "fake"

    async def send(
        self,
        message: EmailMessage,
        *,
        idempotency_key: str | None = None,
    ) -> EmailSendResult:
        self.sent.append(message)
        if self.script:
            return self.script.pop(0)
        return EmailSendResult(
            state=EmailSendState.SENT,
            provider=self.name,
            provider_message_id=f"fake-{len(self.sent)}",
        )
