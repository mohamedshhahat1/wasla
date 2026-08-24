"""Password reset: proving account ownership through the inbox.

The flow docs/SECURITY.md deferred until this repository could send email,
built to the list that document wrote down in advance: a one-time token
stored only as a hash, a short expiry, single-use invalidation, an identical
response whether or not the address is registered, a rate limit on requests,
a session bump on success, and the token never logged and never returned
through the API.

Delivery is the security control. The link goes through the outbox to the
address on the account row - never to an address, or towards an origin, that
the request supplied - and the raw token's entire life is the emailed link
plus the outbox row that carries it, which is cleared on completion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.core.security import (
    generate_reset_token,
    hash_password,
    hash_reset_token,
    validate_password_strength,
)
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.user import User
from app.repositories import UserRepository
from app.repositories.password_reset_repository import PasswordResetTokenRepository
from app.services.audit_service import AuditTrail
from app.services.email_service import EmailOutbox
from app.services.email_templates import EmailTemplate

logger = get_logger(__name__)

RESET_TOKEN_TTL_MINUTES: Final = 30
# The one answer the request endpoint ever gives. Registered, unknown,
# disabled and passwordless addresses all receive it, so the endpoint cannot
# be used to ask which addresses have accounts.
RESET_REQUESTED_MESSAGE: Final = (
    "If that address has an account, a password reset link has been sent."
)
# Unknown, expired, superseded, consumed and malformed tokens all answer this
# way, the invitation flow's rule applied here.
INVALID_RESET: Final = "That password reset link is not valid."


class PasswordResetService:
    """Issue reset tokens by email, and redeem them for a new password.

    Owns no transaction: the request-scoped session commits when the request
    succeeds, which is what puts the token row and its email in one fate and
    the password, the version bump, the audit entry and the notice in
    another.
    """

    def __init__(self, *, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._users = UserRepository(session)
        self._tokens = PasswordResetTokenRepository(session)
        self._outbox = EmailOutbox(session, settings)

    async def request(self, *, email: str) -> None:
        """Issue a reset token if the address has an account. Say nothing.

        No return value by design: the endpoint's answer is a constant. The
        token-generation work is spent on every path so a miss does not
        answer faster than a hit; what remains distinguishable is the
        database write on a hit, which is bounded by the client-address rate
        limit rather than pretended away.

        A new request supersedes every outstanding token first, so asking
        repeatedly narrows the live surface to one token rather than
        extending it - the reset window ends when the newest token expires,
        whatever an attacker does to the endpoint.
        """
        raw_token, token_hash = generate_reset_token()
        user = await self._users.get_by_email(email)
        if user is None or not user.is_active or user.hashed_password is None:
            # Unknown address, suspended account, or an invitation-created
            # account that never chose a password - a reset link would either
            # do nothing or bypass the invitation flow's own proof. The
            # address itself is deliberately not logged.
            logger.info(
                "password_reset.request_unmatched",
                extra={"event": "password_reset.request_unmatched"},
            )
            return

        now = datetime.now(UTC)
        await self._tokens.supersede_outstanding(user_id=user.id, now=now)
        token = await self._tokens.create(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=now + timedelta(minutes=RESET_TOKEN_TTL_MINUTES),
        )
        await self._outbox.enqueue(
            template=EmailTemplate.PASSWORD_RESET,
            recipient=user.email,
            idempotency_key=f"password-reset:{token.id}",
            context={"token": raw_token},
            user_id=user.id,
        )
        logger.info(
            "password_reset.requested",
            extra={"event": "password_reset.requested", "user_id": str(user.id)},
        )

    async def confirm(self, *, raw_token: str, new_password: str) -> User:
        """Redeem a token for a new password, ending every session.

        The order is deliberate. Strength is validated *before* the token is
        spent, so a typo does not cost the one usable link. The spend itself
        is atomic - racing confirmations produce one winner. Success bumps
        `token_version` (ADR-036), supersedes any other outstanding token,
        audits the completion, and queues the password-changed notice in the
        same transaction, keyed to the new version so the notice cannot
        multiply however delivery retries.
        """
        now = datetime.now(UTC)
        token = await self._tokens.get_by_token_hash(hash_reset_token(raw_token))
        if token is None or not token.is_usable(now=now):
            raise AuthenticationError(INVALID_RESET)

        user = await self._users.get_by_id(token.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError(INVALID_RESET)

        validate_password_strength(new_password)

        if not await self._tokens.consume(token_id=token.id, now=now):
            # Somebody else spent it between our read and this write. To the
            # caller that is indistinguishable from any other dead token,
            # and must stay so.
            raise AuthenticationError(INVALID_RESET)
        await self._tokens.supersede_outstanding(user_id=user.id, now=now)

        user.hashed_password = hash_password(new_password)
        # Every access and refresh token dies with the old password - the
        # account-service rule (ADR-036), applied by the same single bump.
        user.token_version += 1

        AuditTrail(self._session).record(
            AuditAction.PASSWORD_RESET_COMPLETED,
            actor=user,
            actor_kind=AuditActorKind.USER,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            meta={"token_version": user.token_version},
        )
        await self._outbox.enqueue(
            template=EmailTemplate.PASSWORD_CHANGED,
            recipient=user.email,
            idempotency_key=f"security-password-changed:{user.id}:{user.token_version}",
            user_id=user.id,
        )
        await self._session.flush()

        logger.info(
            "password_reset.completed",
            extra={"event": "password_reset.completed", "user_id": str(user.id)},
        )
        return user
