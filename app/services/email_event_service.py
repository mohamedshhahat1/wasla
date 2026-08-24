"""What a provider's delivery events are allowed to change.

The trust boundary is narrow, and stating it once is the point of this module.
A verified webhook proves the *delivery* came from the provider. It does not
make the payload's contents true. So nothing here reads an address, a subject,
a tenant or a user from the event body: the only field taken from it is the
provider's message id, looked up against `email_messages.provider_message_id`.
An id matching no row of ours is dropped.

That lookup is the whole defence against the obvious attack. An address in a
bounce event could be anybody's - including a mailbox the attacker wants to
stop this platform writing to. The address actually suppressed is the one *our
own row* recorded as the recipient, so a forged event about a stranger's
mailbox suppresses nothing, because no row of ours ever addressed it.

Suppression is a mail-delivery fact and nothing more. It never touches
`users.is_active`, never bumps `token_version` and never denies a sign-in: an
unreachable mailbox says nothing about the person who owns it, and an account
that could be disabled by bouncing its mail would be an account anybody could
disable (ADR-042).

Opens and clicks are dropped rather than recorded. Neither is evidence that a
person read anything - an image proxy fetches pixels and a scanner follows
links - and the moment either is stored, something eventually treats it as
proof. There is nothing to treat as proof if there is nothing there.

Every transition is idempotent, because a webhook that is retried is normal
traffic rather than an anomaly: `mark_delivered` only ever upgrades `sent`,
`suppress` is an upsert, and re-applying a bounce writes the values it wrote
last time. Combined with the signature's timestamp window, that is the replay
story - there is no separate seen-id table to keep, because repetition is
harmless by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.email import EmailStatus
from app.repositories.email_repository import EmailOutboxRepository

logger = get_logger(__name__)

# Resend's event vocabulary, as sent in the payload's `type`.
DELIVERED: Final = "email.delivered"
BOUNCED: Final = "email.bounced"
COMPLAINED: Final = "email.complained"
FAILED: Final = "email.failed"

# Acted on. Everything else that arrives - `email.sent`, which the outbox
# already recorded synchronously, `email.delivery_delayed`, which resolves
# itself, and `email.opened` / `email.clicked`, which are not facts about
# anything - is acknowledged and dropped.
ACTIONABLE: Final[frozenset[str]] = frozenset({DELIVERED, BOUNCED, COMPLAINED, FAILED})

# Resend reports a bounce's permanence here. Only a permanent one suppresses:
# a transient bounce is a full mailbox or a greylist, and refusing to write to
# that address again would turn a temporary condition into a permanent one.
PERMANENT_BOUNCE: Final = "permanent"

MAX_MESSAGE_ID_LENGTH: Final = 200

# Outcomes, for the log line and for tests. Never returned to the caller: a
# webhook response that distinguished "unknown" from "applied" would be an
# oracle for which message ids this system has issued.
APPLIED: Final = "applied"
UNKNOWN: Final = "unknown"
IGNORED: Final = "ignored"


class EmailEventService:
    """Applies verified provider events to the outbox row they name.

    Owns no transaction. The request-scoped session commits when the request
    succeeds, so an event that cannot be applied leaves nothing behind.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = EmailOutboxRepository(session)

    async def record(self, payload: Mapping[str, Any]) -> str:
        """Apply one event. Returns an outcome label for logging only.

        Shape is validated rather than assumed. This payload arrived from the
        network, and a verified signature says who sent it, not that it is
        well-formed.
        """
        kind = payload.get("type")
        data = payload.get("data")
        if not isinstance(kind, str) or kind not in ACTIONABLE:
            return IGNORED
        if not isinstance(data, Mapping):
            return IGNORED

        message_id = data.get("email_id")
        if not isinstance(message_id, str) or not message_id.strip():
            return IGNORED

        email = await self._repository.get_by_provider_message_id(
            message_id.strip()[:MAX_MESSAGE_ID_LENGTH]
        )
        if email is None:
            # An id this system never issued, or one whose row is long gone.
            # Logged without echoing the id: it is caller-supplied, and a log
            # line is not the place to render unvalidated input.
            logger.info(
                "email.event_unmatched",
                extra={"event": "email.event_unmatched", "kind": kind},
            )
            return UNKNOWN

        now = datetime.now(UTC)
        if kind == DELIVERED:
            await self._repository.mark_delivered(email, now=now)
        elif kind == COMPLAINED:
            # The recipient pressed "this is spam". The message arrived, so the
            # row's status is left alone and only the address is suppressed -
            # continuing to write to a complainant is how a sending domain dies.
            await self._repository.suppress(email.recipient, reason="complaint")
        elif kind == BOUNCED:
            if not self._is_permanent(data):
                logger.info(
                    "email.transient_bounce",
                    extra={
                        "event": "email.transient_bounce",
                        "email_message_id": str(email.id),
                        "template": email.template,
                    },
                )
                return APPLIED
            await self._repository.suppress(email.recipient, reason="hard_bounce")
            await self._fail(email, now=now, code="bounced", message="the mailbox rejected it")
        else:  # FAILED
            await self._fail(
                email,
                now=now,
                code="provider_failed",
                message="the provider could not deliver it",
            )

        logger.info(
            "email.event_recorded",
            extra={
                "event": "email.event_recorded",
                "kind": kind,
                "email_message_id": str(email.id),
                "template": email.template,
                "tenant_id": str(email.tenant_id) if email.tenant_id else None,
            },
        )
        return APPLIED

    @staticmethod
    def _is_permanent(data: Mapping[str, Any]) -> bool:
        """Whether a bounce says the mailbox is gone rather than busy.

        Absent or unrecognised permanence is treated as *not* permanent. The
        cost of guessing wrong that way is a retry; guessing wrong the other
        way silently stops a real person receiving their password reset.
        """
        bounce = data.get("bounce")
        if not isinstance(bounce, Mapping):
            return False
        kind = bounce.get("type")
        return isinstance(kind, str) and kind.strip().lower() == PERMANENT_BOUNCE

    async def _fail(
        self,
        email: Any,
        *,
        now: datetime,
        code: str,
        message: str,
    ) -> None:
        """Record that a message the provider accepted did not arrive.

        `delivered` is left alone: a delivery already observed is not undone by
        a later event, and letting one do so would make the status depend on
        the order the webhooks happened to arrive in.
        """
        if email.status is EmailStatus.DELIVERED:
            return
        await self._repository.mark_failed(
            email,
            now=now,
            error_code=code,
            error_message=message,
        )


__all__ = [
    "ACTIONABLE",
    "APPLIED",
    "BOUNCED",
    "COMPLAINED",
    "DELIVERED",
    "FAILED",
    "IGNORED",
    "UNKNOWN",
    "EmailEventService",
]
