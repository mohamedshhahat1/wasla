"""The shapes every email send speaks, whoever delivers it.

Two rules are enforced here rather than trusted to callers, because a rule
enforced in one place cannot drift:

**No header injection, as a class.** A carriage return, line feed or NUL in an
address or a subject is refused at construction. There is no field for
arbitrary headers at all - what cannot be expressed cannot be abused - and the
recipient list is bounded, because a transactional email has one reader and a
message with fifty is a relay being tested.

**Failure is classified, not passed through.** The outbox worker's retry
decision hangs on whether a failure is transient or permanent, and that
judgement belongs to the provider adapter that understands its own error
surface - not to a worker pattern-matching on strings from somebody else's
API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

MAX_RECIPIENTS: Final = 10
MAX_SUBJECT_LENGTH: Final = 200
MAX_ADDRESS_LENGTH: Final = 320
# Generous for a transactional message and hostile to a payload: a rendered
# notice is a few kilobytes, and a body approaching this is a bug.
MAX_BODY_BYTES: Final = 262_144

# The characters that turn one header into two. Checked in every field that
# reaches the SMTP conversation.
_CONTROL_CHARACTERS: Final = re.compile(r"[\r\n\x00]")
# A shape check, not RFC 5322: the addresses that arrive here come from rows
# this application wrote, and the check exists to catch corruption and
# injection rather than to adjudicate exotic-but-legal addresses.
_ADDRESS_SHAPE: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailSendState(StrEnum):
    """What one attempt at delivery produced.

    `TRANSIENT_FAILURE` is an invitation to retry; `PERMANENT_FAILURE` is an
    instruction to stop. The distinction is made by the provider adapter,
    which is the only layer that can read its own provider's error surface.
    """

    SENT = "sent"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True, slots=True)
class EmailSendResult:
    """The outcome of one send attempt, in internal vocabulary only.

    `error_message` is bounded and provider-sanitised before it gets here:
    it may reach a log line and an outbox row, so it must never carry a
    credential or an unbounded upstream body.
    """

    state: EmailSendState
    provider: str
    provider_message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def validate_address(address: str) -> str:
    """Refuse an address that could not be a safe recipient.

    Raises ValueError rather than a domain error: callers are internal, and a
    bad address in an outbox row is a permanent failure of that row, not
    something to explain to an HTTP caller.
    """
    candidate = address.strip()
    if not candidate:
        raise ValueError("email address is empty")
    if len(candidate) > MAX_ADDRESS_LENGTH:
        raise ValueError("email address is too long")
    if _CONTROL_CHARACTERS.search(candidate):
        raise ValueError("email address contains control characters")
    if not _ADDRESS_SHAPE.match(candidate):
        raise ValueError("email address is not a valid shape")
    return candidate


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One message, fully resolved and validated at construction.

    The sender is a field rather than provider state so a rendered message is
    complete on its own, and there is deliberately no headers field: the
    fields below are the entire vocabulary a caller has.
    """

    sender: str
    to: tuple[str, ...]
    subject: str
    text: str
    html: str | None = None
    reply_to: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sender", validate_address(self.sender))
        if not self.to:
            raise ValueError("email message has no recipients")
        if len(self.to) > MAX_RECIPIENTS:
            raise ValueError("email message has too many recipients")
        object.__setattr__(self, "to", tuple(validate_address(recipient) for recipient in self.to))
        if self.reply_to is not None:
            object.__setattr__(self, "reply_to", validate_address(self.reply_to))

        subject = self.subject.strip()
        if not subject:
            raise ValueError("email subject is empty")
        if len(subject) > MAX_SUBJECT_LENGTH:
            raise ValueError("email subject is too long")
        if _CONTROL_CHARACTERS.search(subject):
            raise ValueError("email subject contains control characters")
        object.__setattr__(self, "subject", subject)

        if not self.text.strip():
            raise ValueError("email message has no text body")
        if len(self.text.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("email text body is too large")
        if self.html is not None and len(self.html.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("email html body is too large")


class EmailProvider(Protocol):
    """What the outbox worker needs of a delivery service, and nothing more."""

    @property
    def name(self) -> str: ...

    async def send(
        self,
        message: EmailMessage,
        *,
        idempotency_key: str | None = None,
    ) -> EmailSendResult: ...
