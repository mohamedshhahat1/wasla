"""Proving that somebody can read the address on their account.

A six-digit code, mailed through the existing outbox, checked against the
account that asked for it. What this is *not* is stated first because it is the
easiest thing to get wrong: verification is not a second factor, not a login
mechanism, and not a permission. It sets one timestamp and grants nothing. See
docs/EMAIL_VERIFICATION.md.

The service owns no transaction. Issuing supersedes, creates and enqueues on
the caller's session, so the three land together or not at all - there is no
visible state where a challenge exists with no mail on its way, or mail is
queued for a challenge that was rolled back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import RateLimitedError, ValidationError
from app.core.logging import get_logger
from app.core.rate_limit import RateLimiter, RateLimitPolicy
from app.core.security import (
    generate_verification_code,
    normalise_verification_code,
    spend_code_verification_time,
    verify_verification_code,
)
from app.db.models import User
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.email_verification import (
    DEFAULT_MAX_VERIFICATION_ATTEMPTS,
    DEFAULT_VERIFICATION_TTL_SECONDS,
    EmailVerificationChallenge,
)
from app.repositories.email_verification_repository import EmailVerificationRepository
from app.services.audit_service import AuditTrail
from app.services.email_service import EmailOutbox
from app.services.email_templates import EmailTemplate

logger = get_logger(__name__)

# One answer for every rejected code. Which condition failed - wrong, expired,
# exhausted, superseded, malformed, never issued - is recorded in the audit
# trail and never in the response: an error that distinguishes "expired" from
# "wrong" tells somebody guessing whether it is worth continuing.
INVALID_CODE: Final = "That verification code is not valid or has expired."

# A message for the caller that reveals nothing about what happened. Returned
# whether a code was sent, suppressed, or skipped because the address is
# already verified.
VERIFICATION_SENT_MESSAGE: Final = (
    "If that address still needs verifying, a code has been sent to it."
)

# Bounds on the configured lifetime. Under a minute is unusable by a person
# reading mail on a phone; over an hour is not a short-lived code, and the
# whole security argument for six digits rests on the window being small.
#
# `Settings` enforces the same bounds on the field, so a deployment configured
# outside them fails to start rather than reaching this. These stay because the
# service is constructible without settings - a unit test, a future worker -
# and a bound that only one of two doors checks is a bound with a door around
# it.
MIN_TTL_SECONDS: Final = 60
MAX_TTL_SECONDS: Final = 3600

# Bounds on the configured attempt ceiling, mirroring the field for the same
# reason. Above ten, a million-value code space starts being worth searching.
MIN_MAX_ATTEMPTS: Final = 1
MAX_MAX_ATTEMPTS: Final = 10

# Two policies, because they defend different things: one bounds how much mail
# an account can cause, the other bounds guessing. Both carry `local_fallback`
# - they stand in front of a guessable secret, so a Redis outage must not mean
# unlimited attempts (ADR-040).
VERIFICATION_SEND_POLICY: Final = "auth:verification_send"
VERIFICATION_ATTEMPT_POLICY: Final = "auth:verification_attempt"

_SEND_LIMIT: Final = 3
_SEND_WINDOW_SECONDS: Final = 900
_ATTEMPT_LIMIT: Final = 10
_ATTEMPT_WINDOW_SECONDS: Final = 900


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """What a successful confirmation established."""

    verified_at: datetime


class EmailVerificationService:
    """Issues and spends email verification challenges."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        limiter: RateLimiter | None = None,
        ttl_seconds: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        """Build the service, refusing an unsafe lifetime or attempt ceiling.

        Both are read from `settings` when not given explicitly, so the
        deployment's `EMAIL_VERIFICATION_TTL_SECONDS` and
        `EMAIL_VERIFICATION_MAX_ATTEMPTS` are what a request actually uses. The
        arguments remain for tests, which need to drive an expiry or an
        exhausted challenge without reconstructing a whole `Settings`.

        Values outside the bounds are refused rather than clamped. Silently
        correcting configuration is how an operator ends up believing a code
        lives for a day when it lives for an hour, and a misconfiguration that
        starts cleanly is a misconfiguration nobody finds.

        `limiter` is optional for `AuthService`'s reason: a unit test can build
        this without Redis. The route always supplies one.
        """
        ttl = self._configured(
            explicit=ttl_seconds,
            settings=settings,
            attribute="email_verification_ttl_seconds",
            fallback=DEFAULT_VERIFICATION_TTL_SECONDS,
        )
        if not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
            raise ValidationError(
                "Email verification code lifetime must be between "
                f"{MIN_TTL_SECONDS} and {MAX_TTL_SECONDS} seconds.",
            )

        attempts = self._configured(
            explicit=max_attempts,
            settings=settings,
            attribute="email_verification_max_attempts",
            fallback=DEFAULT_MAX_VERIFICATION_ATTEMPTS,
        )
        if not MIN_MAX_ATTEMPTS <= attempts <= MAX_MAX_ATTEMPTS:
            raise ValidationError(
                "Email verification attempts must be between "
                f"{MIN_MAX_ATTEMPTS} and {MAX_MAX_ATTEMPTS}.",
            )

        self._session = session
        self._settings = settings
        self._limiter = limiter
        self._ttl_seconds = ttl
        self._max_attempts = attempts
        self._challenges = EmailVerificationRepository(session)
        self._outbox = EmailOutbox(session, settings)
        self._audit = AuditTrail(session)

    @staticmethod
    def _configured(
        *,
        explicit: int | None,
        settings: Settings,
        attribute: str,
        fallback: int,
    ) -> int:
        """An explicit argument, else the setting, else the module default.

        `getattr` rather than a direct read because several tests and the email
        worker pass a small stand-in object in place of `Settings` - the same
        arrangement `tests/integration/test_email_outbox.py` already uses for
        the outbox. Those objects carry the handful of fields their subject
        touches, and adding two more to every one of them would couple them to
        a feature they are not about.
        """
        if explicit is not None:
            return explicit
        value = getattr(settings, attribute, None)
        return fallback if value is None else int(value)

    async def request(self, *, user: User) -> str:
        """Issue a code for this account's own address, and queue the mail.

        The address comes from the account, never from request input. That is
        what makes this incapable of being an enumeration oracle or a way to
        make the platform mail a stranger: there is no address parameter to
        probe and no target id to tamper with.

        Returns a message that reads the same whether a code was sent, the
        recipient is suppressed, or the address was already verified.
        """
        await self._limit(VERIFICATION_SEND_POLICY, user, _SEND_LIMIT, _SEND_WINDOW_SECONDS)

        if user.email_verified_at is not None:
            # Nothing to prove. Not an error, and not distinguishable from a
            # send in the response - the caller already knows its own state, so
            # there is nothing to protect here, but there is also no reason to
            # send mail nobody needs.
            logger.info(
                "email_verification.already_verified",
                extra={
                    "event": "email_verification.already_verified",
                    "user_id": str(user.id),
                },
            )
            return VERIFICATION_SENT_MESSAGE

        now = datetime.now(UTC)
        # Supersede first, in this transaction. The partial unique index would
        # refuse a second live row anyway, which is what turns forgetting this
        # into a loud failure rather than two valid codes.
        superseded = await self._challenges.supersede_outstanding(user_id=user.id, now=now)

        code, code_hash = generate_verification_code()
        challenge = await self._challenges.create(
            user_id=user.id,
            email=user.email,
            code_hash=code_hash,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )

        # The plaintext code reaches the worker through the outbox context,
        # because the worker renders the message - the request does not talk to
        # the provider (ADR-042). It is never logged, exposed by no endpoint,
        # and the outbox clears context on terminal transition, so its persisted
        # life is bounded by delivery. This is documented rather than hidden:
        # see docs/EMAIL_VERIFICATION.md.
        await self._outbox.enqueue(
            template=EmailTemplate.EMAIL_VERIFICATION,
            recipient=user.email,
            idempotency_key=f"email-verification:{challenge.id}",
            context={
                "code": code,
                "expires_minutes": str(self._ttl_seconds // 60),
            },
            user_id=user.id,
        )

        self._audit.record(
            AuditAction.EMAIL_VERIFICATION_REQUESTED,
            actor=user,
            actor_kind=AuditActorKind.USER,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            # `superseded` records that a resend invalidated something, which is
            # the difference between a first request and a fourth. No code, and
            # no hash of one - an audit log of credentials is a second copy.
            meta={"challenge_id": str(challenge.id), "superseded": superseded},
        )
        logger.info(
            "email_verification.requested",
            extra={
                "event": "email_verification.requested",
                "user_id": str(user.id),
                "challenge_id": str(challenge.id),
                "superseded": superseded,
            },
        )
        return VERIFICATION_SENT_MESSAGE

    async def confirm(self, *, user: User, submitted: str) -> VerificationOutcome:
        """Spend a code and mark the address proven.

        Acts only on `user` - the authenticated caller. There is no path here
        that takes an account id, which is what makes cross-account
        verification impossible rather than merely forbidden.

        Every failure raises the same error with the same message. The reason is
        recorded for an operator and withheld from the caller.
        """
        await self._limit(
            VERIFICATION_ATTEMPT_POLICY,
            user,
            _ATTEMPT_LIMIT,
            _ATTEMPT_WINDOW_SECONDS,
        )

        code = normalise_verification_code(submitted)
        if code is None:
            # Malformed input costs the same as a real attempt, so "not six
            # digits" cannot be told from "wrong" by timing. It does not consume
            # an attempt against the challenge: a typo is not a guess, and
            # letting bad formatting burn the budget would let a broken client
            # lock a person out of their own verification.
            spend_code_verification_time(submitted)
            await self._reject(user, reason="malformed", challenge_id=None)
            raise ValidationError(INVALID_CODE)

        now = datetime.now(UTC)
        challenge = await self._challenges.get_active(user_id=user.id)
        if challenge is None:
            # No live challenge. Spend the time a real check would, so this is
            # not distinguishable from a wrong code by how fast it answers.
            spend_code_verification_time(code)
            await self._reject(user, reason="no_active_challenge", challenge_id=None)
            raise ValidationError(INVALID_CODE)

        if not challenge.is_usable(now=now, email=user.email, max_attempts=self._max_attempts):
            spend_code_verification_time(code)
            await self._reject(
                user,
                reason=self._dead_reason(challenge, now=now, email=user.email),
                challenge_id=challenge.id,
            )
            raise ValidationError(INVALID_CODE)

        # Counted before the comparison, so a guess costs an attempt whether or
        # not it happens to be right. Counting only failures would let somebody
        # fire many wrong codes concurrently and have most of them slip through
        # between the read and the increment.
        await self._challenges.register_failure(challenge_id=challenge.id)

        if not verify_verification_code(code=code, code_hash=challenge.code_hash):
            await self._reject(user, reason="wrong_code", challenge_id=challenge.id)
            raise ValidationError(INVALID_CODE)

        # The security boundary. One conditional UPDATE that re-checks
        # everything above, because the Argon2 comparison happened in Python and
        # the row may have moved underneath it. Exactly one concurrent request
        # can win this.
        if not await self._challenges.consume(
            challenge_id=challenge.id,
            email=user.email,
            now=now,
            max_attempts=self._max_attempts,
        ):
            await self._reject(user, reason="lost_race", challenge_id=challenge.id)
            raise ValidationError(INVALID_CODE)

        # Reached only by the winner, which is what makes this exactly-once
        # without needing a conditional write of its own.
        user.email_verified_at = now
        self._audit.record(
            AuditAction.EMAIL_VERIFIED,
            actor=user,
            actor_kind=AuditActorKind.USER,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            meta={"challenge_id": str(challenge.id)},
        )
        logger.info(
            "email_verification.verified",
            extra={
                "event": "email_verification.verified",
                "user_id": str(user.id),
                "challenge_id": str(challenge.id),
            },
        )
        return VerificationOutcome(verified_at=now)

    def _dead_reason(
        self,
        challenge: EmailVerificationChallenge,
        *,
        now: datetime,
        email: str,
    ) -> str:
        """Why a challenge that exists cannot be used.

        For the audit entry only. Never reaches a response - the caller gets
        `INVALID_CODE` whichever of these it is.

        Each branch tests the condition it names. An earlier version reported
        `attempts_exhausted` for any challenge with a single failed attempt
        behind it, which meant an address change on a challenge somebody had
        mistyped once was filed under brute force - and an operator reading the
        trail during an incident would have been looking at the wrong thing.
        """
        if challenge.expires_at <= now:
            return "expired"
        if challenge.attempts >= self._max_attempts:
            return "attempts_exhausted"
        if challenge.email != email.strip().lower():
            # The address moved since the code was issued, so the challenge is
            # bound to something that is no longer this account's address.
            return "address_changed"
        # `is_usable` also rejects a consumed or superseded challenge, but
        # `get_active` filters both out, so reaching here means the row changed
        # underneath this request.
        return "not_live"

    async def _reject(self, user: User, *, reason: str, challenge_id: object | None) -> None:
        """Record a rejected attempt, make it durable, and refuse.

        **The commit is the point of this method.** Every caller raises
        immediately afterwards, and an exception unwinds the request's
        transaction - so without it, both the audit entry and the attempt
        increment that preceded it are discarded by the very refusal they
        describe. That is not a missing log line: `attempts` is what ends a
        challenge, so a counter that rolls back is an attempt cap that does not
        exist, and a challenge could be guessed at until the rate limit alone
        stopped it. Seven wrong codes against a running container left
        `attempts` at zero before this existed.

        Everything written by this point is meant to survive: the increment
        from `register_failure`, and this entry. `email_verified_at` is set
        only on the success path, which no failure follows, so there is nothing
        half-finished to make durable.

        `AuthService._tear_down_after_reuse` commits for the same reason - a
        consequence that must outlive the request that triggered it.
        """
        meta: dict[str, object] = {"reason": reason}
        if challenge_id is not None:
            meta["challenge_id"] = str(challenge_id)
        self._audit.record(
            AuditAction.EMAIL_VERIFICATION_FAILED,
            actor=user,
            actor_kind=AuditActorKind.USER,
            target_type="user",
            target_id=user.id,
            target_label=user.email,
            meta=meta,
        )
        logger.info(
            "email_verification.failed",
            extra={
                "event": "email_verification.failed",
                "user_id": str(user.id),
                # The reason, never the submitted value. A log of guesses is a
                # log of near-misses, and a near-miss narrows the keyspace.
                "reason": reason,
            },
        )
        await self._session.commit()

    async def _limit(self, name: str, user: User, limit: int, window_seconds: int) -> None:
        """Apply one of the two policies to this account.

        Keyed by account id rather than client address: an address is rotatable,
        so a per-address budget is not a budget. Keying by account is safe here
        only because neither policy locks anything - they refuse for a window,
        while the cap that actually ends a challenge is per-challenge, so nobody
        can spend a stranger's ability to verify.

        A refusal is audited before it is re-raised. `RateLimiter.enforce`
        raises, so without catching it here a throttled attempt would leave a
        429, a log line and nothing in the trail - and "was this account being
        hammered" is exactly the question the trail is read for during an
        incident. It is recorded as an `EMAIL_VERIFICATION_FAILED` with a
        `rate_limited` reason rather than as an action of its own, following
        the vocabulary's rule that one question gets one action.

        The entry is **committed here** rather than left to the request, and
        that is the whole reason this method is shaped this way. The caller
        raises immediately afterwards, and an exception unwinds the request's
        transaction - so an entry staged the ordinary way would be discarded by
        the very refusal it describes, leaving a 429 and a log line and nothing
        in the trail. `AuthService._tear_down_after_reuse` commits for exactly
        this reason.

        Committing here is safe because of *where* here is: both public methods
        call this first, so the only thing in the session is this row and
        whatever loading the user read. Nothing half-written is being made
        durable.
        """
        if self._limiter is None or not self._settings.rate_limit_enabled:
            return
        policy = RateLimitPolicy(
            name=name,
            limit=limit,
            window_seconds=window_seconds,
            # In front of a guessable secret, so an outage must not mean
            # unlimited attempts (ADR-040).
            local_fallback=True,
        )
        try:
            await self._limiter.enforce(policy, str(user.id))
        except RateLimitedError:
            await self._reject(user, reason="rate_limited", challenge_id=None)
            raise
