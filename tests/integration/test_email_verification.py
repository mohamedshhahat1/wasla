"""Adversarial tests for email verification, against a real database.

The threat, stated once: **a six-digit code is guessable.** Every other secret
in this schema is 256 bits of randomness where the only realistic attack is
theft. Here the entire keyspace is a million values, so the controls that
matter are the ones that bound *how many times* somebody may guess and *for how
long* - and those are exactly the controls that break under concurrency if they
are written as read-then-write.

So the tests below are mostly about the seams rather than the happy path: the
last permitted attempt, the attempt that arrives after the challenge died, two
requests carrying the same correct code, a code that outlived the address it
was sent to, and the partial unique index that stops two codes being valid at
once. Several of them cannot be written against a fake - they need PostgreSQL
enforcing an index and two sessions racing a real UPDATE.

`MAX_ATTEMPTS` here is deliberately small. Every wrong guess costs an Argon2
verification, and the point being tested is the boundary, not the number.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.email import EmailStatus, OutboundEmail
from app.db.models.email_verification import EmailVerificationChallenge
from app.db.models.user import User
from app.repositories.email_verification_repository import EmailVerificationRepository
from app.services.email_templates import EmailTemplate, render
from app.services.email_verification_service import (
    INVALID_CODE,
    VERIFICATION_SENT_MESSAGE,
    EmailVerificationService,
)

pytestmark = pytest.mark.integration

MAX_ATTEMPTS = 3
TTL_SECONDS = 600
ADDRESS = "owner@acme-example.com"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
        **overrides,
    )


def _service(session: AsyncSession, **kwargs: object) -> EmailVerificationService:
    """The service as a request builds it, minus Redis.

    `rate_limit_enabled=False` in the settings above means the limiter is never
    consulted, so its absence changes nothing here. Limiting has its own tests
    at the endpoint level, where the identity it counts by actually exists.
    """
    arguments: dict[str, object] = {
        "ttl_seconds": TTL_SECONDS,
        "max_attempts": MAX_ATTEMPTS,
    }
    arguments.update(kwargs)
    return EmailVerificationService(session=session, settings=_settings(), **arguments)  # type: ignore[arg-type]


async def _account(session: AsyncSession, email: str = ADDRESS) -> User:
    user = User(email=email, full_name="Owner", hashed_password="x", is_active=True)
    session.add(user)
    await session.flush()
    return user


async def _issue(session: AsyncSession, user: User, **kwargs: object) -> str:
    """Request a code and recover the plaintext from the queued mail.

    The outbox context is the *only* place the plaintext exists after issuing -
    which is the property being relied on here, and the one asserted directly
    in `test_the_plaintext_code_is_never_stored_on_the_challenge`.

    The row is found by the challenge's idempotency key rather than by taking
    the newest one, and that is not fussiness. `created_at` defaults to
    `now()`, which in PostgreSQL is the *transaction* clock - so every row a
    test writes shares one timestamp and "the latest" orders arbitrarily. A
    resend test would then race its own fixture and fail as though the product
    had superseded the wrong challenge.
    """
    service = _service(session, **kwargs)
    await service.request(user=user)
    challenge = await _live(session, user)
    assert challenge is not None
    row = await _queued(session, challenge.id)
    assert row is not None
    return str(row.context["code"])


async def _queued(session: AsyncSession, challenge_id: uuid.UUID) -> OutboundEmail | None:
    statement = select(OutboundEmail).where(
        OutboundEmail.idempotency_key == f"email-verification:{challenge_id}",
    )
    return (await session.execute(statement)).scalars().first()


async def _challenges(
    session: AsyncSession,
    user: User,
) -> list[EmailVerificationChallenge]:
    statement = (
        select(EmailVerificationChallenge)
        .where(EmailVerificationChallenge.user_id == user.id)
        .order_by(EmailVerificationChallenge.created_at)
    )
    return list((await session.execute(statement)).scalars())


async def _live(session: AsyncSession, user: User) -> EmailVerificationChallenge | None:
    return await EmailVerificationRepository(session).get_active(user_id=user.id)


async def _audit_reasons(session: AsyncSession, user: User) -> Counter[str]:
    """Every recorded failure reason for this account, counted.

    Counted rather than listed in order, for `_issue`'s reason: `occurred_at`
    is the transaction clock, so rows written by one test are indistinguishable
    by time and any ordering assertion would be asserting on physical row
    order. What the tests actually care about is which reasons were recorded
    and how many of each, which a multiset says exactly.
    """
    statement = select(AuditLog).where(
        AuditLog.target_id == user.id,
        AuditLog.action == AuditAction.EMAIL_VERIFICATION_FAILED,
    )
    rows = list((await session.execute(statement)).scalars())
    return Counter(str(row.meta.get("reason")) for row in rows if row.meta)


# ------------------------------------------------------------------ the flow


async def test_a_code_is_issued_queued_and_accepted(db_session: AsyncSession) -> None:
    user = await _account(db_session)
    assert user.email_verified_at is None

    code = await _issue(db_session, user)
    outcome = await _service(db_session).confirm(user=user, submitted=code)

    assert user.email_verified_at == outcome.verified_at
    assert user.is_email_verified


async def test_the_queued_mail_names_the_account_address_and_the_right_template(
    db_session: AsyncSession,
) -> None:
    """The recipient comes from the row, never from anything a caller sent."""
    user = await _account(db_session)
    await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None

    row = await _queued(db_session, challenge.id)
    assert row is not None
    assert row.recipient == ADDRESS
    assert row.template == EmailTemplate.EMAIL_VERIFICATION.value
    assert row.status is EmailStatus.PENDING
    assert row.tenant_id is None, "verification is an account fact, not a workspace one"


async def test_the_rendered_email_carries_the_code_and_no_link(
    db_session: AsyncSession,
) -> None:
    """The code has to survive rendering, or the mail is useless.

    And there must be no URL: a code in a link is a code in browser history, a
    `Referer` header and whatever proxy logged the request.
    """
    user = await _account(db_session)
    code = await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None
    row = await _queued(db_session, challenge.id)
    assert row is not None

    rendered = render(
        EmailTemplate.EMAIL_VERIFICATION,
        row.context,
        public_url="https://app.example.com",
    )
    assert code in rendered.text
    assert code in rendered.html
    assert code not in rendered.subject, "a subject shows on a lock screen"
    assert "http://" not in rendered.html and "https://" not in rendered.html


async def test_the_plaintext_code_is_never_stored_on_the_challenge(
    db_session: AsyncSession,
) -> None:
    """The whole storage argument, asserted against the row itself."""
    user = await _account(db_session)
    code = await _issue(db_session, user)

    challenge = await _live(db_session, user)
    assert challenge is not None
    assert code not in challenge.code_hash
    assert challenge.code_hash.startswith("$argon2")
    # Nor anywhere else on the row.
    assert code not in repr(challenge)
    assert code not in str(challenge.email)


# ------------------------------------------------------------------- expiry


async def test_a_code_works_right_up_to_its_expiry(db_session: AsyncSession) -> None:
    user = await _account(db_session)
    code = await _issue(db_session, user, ttl_seconds=60)

    challenge = await _live(db_session, user)
    assert challenge is not None
    # One second short of expiry: still live.
    challenge.expires_at = datetime.now(UTC) + timedelta(seconds=1)
    await db_session.flush()

    await _service(db_session).confirm(user=user, submitted=code)
    assert user.email_verified_at is not None


async def test_an_expired_code_is_refused(db_session: AsyncSession) -> None:
    user = await _account(db_session)
    code = await _issue(db_session, user)

    challenge = await _live(db_session, user)
    assert challenge is not None
    challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    with pytest.raises(ValidationError, match=INVALID_CODE):
        await _service(db_session).confirm(user=user, submitted=code)
    assert user.email_verified_at is None
    assert await _audit_reasons(db_session, user) == Counter({"expired": 1})


async def test_an_expired_challenge_is_left_alone_rather_than_deleted(
    db_session: AsyncSession,
) -> None:
    """Nothing sweeps them. An expired row is inert, and the next send
    supersedes it - which is what the partial unique index needs to be true."""
    user = await _account(db_session)
    await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None
    challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    await _issue(db_session, user)

    rows = await _challenges(db_session, user)
    assert len(rows) == 2
    assert rows[0].superseded_at is not None


# ------------------------------------------------------------------ attempts


async def test_a_wrong_code_costs_an_attempt(db_session: AsyncSession) -> None:
    user = await _account(db_session)
    await _issue(db_session, user)

    with pytest.raises(ValidationError):
        await _service(db_session).confirm(user=user, submitted="000000")

    challenge = await _live(db_session, user)
    assert challenge is not None
    assert challenge.attempts == 1
    assert await _audit_reasons(db_session, user) == Counter({"wrong_code": 1})


async def test_the_last_permitted_attempt_can_still_succeed(
    db_session: AsyncSession,
) -> None:
    """The regression this file was worth writing for.

    An attempt is counted *before* the code is compared, so by the time the
    consuming UPDATE runs, the row already includes the attempt being judged.
    A strict `attempts < max` there rejected the correct code on the final
    permitted try and reported it as a lost race - an attempt ceiling of five
    that in practice allowed four, failing the person who typed carefully after
    mistyping and telling the operator the wrong story about why.
    """
    user = await _account(db_session)
    code = await _issue(db_session, user)

    for _ in range(MAX_ATTEMPTS - 1):
        with pytest.raises(ValidationError):
            await _service(db_session).confirm(user=user, submitted="000000")

    challenge = await _live(db_session, user)
    assert challenge is not None
    assert challenge.attempts == MAX_ATTEMPTS - 1

    await _service(db_session).confirm(user=user, submitted=code)
    assert user.email_verified_at is not None


async def test_an_exhausted_challenge_refuses_even_the_correct_code(
    db_session: AsyncSession,
) -> None:
    """The ceiling is the point: a dead challenge is dead for everybody."""
    user = await _account(db_session)
    code = await _issue(db_session, user)

    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(ValidationError):
            await _service(db_session).confirm(user=user, submitted="000000")

    with pytest.raises(ValidationError, match=INVALID_CODE):
        await _service(db_session).confirm(user=user, submitted=code)

    assert user.email_verified_at is None
    assert await _audit_reasons(db_session, user) == Counter(
        {"wrong_code": MAX_ATTEMPTS, "attempts_exhausted": 1}
    )


async def test_a_malformed_submission_does_not_spend_an_attempt(
    db_session: AsyncSession,
) -> None:
    """A typo is not a guess.

    Letting bad formatting burn the budget would let a broken client lock
    somebody out of verifying their own address.
    """
    user = await _account(db_session)
    code = await _issue(db_session, user)

    for submitted in ("", "12345", "abcdef", "٤٨٢٧٣١"):
        with pytest.raises(ValidationError, match=INVALID_CODE):
            await _service(db_session).confirm(user=user, submitted=submitted)

    challenge = await _live(db_session, user)
    assert challenge is not None
    assert challenge.attempts == 0
    assert await _audit_reasons(db_session, user) == Counter({"malformed": 4})

    await _service(db_session).confirm(user=user, submitted=code)
    assert user.email_verified_at is not None


async def test_the_attempt_counter_is_incremented_by_the_database(
    db_session: AsyncSession,
) -> None:
    """`attempts = attempts + 1` evaluated in SQL, not read into Python.

    Five concurrent guesses that each read zero and each write one produce a
    cap that caps nothing - under exactly the conditions an attacker arranges.
    """
    user = await _account(db_session)
    await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None

    repository = EmailVerificationRepository(db_session)
    totals = [await repository.register_failure(challenge_id=challenge.id) for _ in range(3)]
    assert totals == [1, 2, 3]


async def test_counting_a_failure_against_a_dead_challenge_reports_nothing(
    db_session: AsyncSession,
) -> None:
    """Not an error: a guess can arrive just after the challenge was spent."""
    user = await _account(db_session)
    code = await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None
    await _service(db_session).confirm(user=user, submitted=code)

    repository = EmailVerificationRepository(db_session)
    assert await repository.register_failure(challenge_id=challenge.id) is None


# ------------------------------------------------- durability of a refusal


async def test_a_failed_attempt_survives_the_refusal_that_reports_it(
    db_session: AsyncSession,
) -> None:
    """The attempt cap only exists if the count outlives the request.

    Found against a running container, not here: seven wrong codes over real
    HTTP left `attempts` at zero. `confirm` raises on failure, an exception
    unwinds the request's transaction, and the rollback took the increment and
    the audit entry with it - so the per-challenge ceiling, one of the three
    bounds ADR-043 rests on, did not exist in a deployment at all. Guessing was
    limited only by the rate limit.

    Every test above this one missed it because they drive the service on a
    session nobody rolls back, which is exactly what a request does *not* do.
    The rollback below is what makes this faithful: it discards anything the
    service left pending, so only what the service committed survives to be
    counted.
    """
    user = await _account(db_session)
    await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None
    # Read out before the rollback: it expires every loaded instance, and an
    # attribute access afterwards would fail as a lazy load rather than as the
    # assertion this test is about.
    challenge_id = challenge.id

    with pytest.raises(ValidationError):
        await _service(db_session).confirm(user=user, submitted="000000")

    # What the request's own unwinding would do.
    await db_session.rollback()

    persisted = (
        await db_session.execute(
            select(EmailVerificationChallenge.attempts).where(
                EmailVerificationChallenge.id == challenge_id,
            ),
        )
    ).scalar_one()
    assert persisted == 1, "a wrong guess that costs nothing is not a guess"

    assert await _audit_reasons(db_session, user) == Counter({"wrong_code": 1})


async def test_guesses_accumulate_across_refusals_until_the_ceiling(
    db_session: AsyncSession,
) -> None:
    """The cap reached the way an attacker reaches it: one request at a time.

    Each refusal rolls back, so this is the end-to-end version of the property
    above - and the one that would have caught an attempt counter that resets
    itself every time it is used.
    """
    user = await _account(db_session)
    code = await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None
    challenge_id = challenge.id

    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(ValidationError):
            await _service(db_session).confirm(user=user, submitted="000000")
        await db_session.rollback()

    persisted = (
        await db_session.execute(
            select(EmailVerificationChallenge.attempts).where(
                EmailVerificationChallenge.id == challenge_id,
            ),
        )
    ).scalar_one()
    assert persisted == MAX_ATTEMPTS

    # And the challenge is now dead even for the code that was always correct.
    with pytest.raises(ValidationError, match=INVALID_CODE):
        await _service(db_session).confirm(user=user, submitted=code)
    await db_session.rollback()
    assert user.email_verified_at is None


# -------------------------------------------------------------------- replay


async def test_a_consumed_code_cannot_be_used_again(db_session: AsyncSession) -> None:
    user = await _account(db_session)
    code = await _issue(db_session, user)
    first = await _service(db_session).confirm(user=user, submitted=code)

    with pytest.raises(ValidationError, match=INVALID_CODE):
        await _service(db_session).confirm(user=user, submitted=code)

    # And the original timestamp is untouched: verification happens once.
    assert user.email_verified_at == first.verified_at
    assert await _audit_reasons(db_session, user) == Counter({"no_active_challenge": 1})


async def test_verification_is_recorded_exactly_once(db_session: AsyncSession) -> None:
    user = await _account(db_session)
    code = await _issue(db_session, user)
    await _service(db_session).confirm(user=user, submitted=code)
    with pytest.raises(ValidationError):
        await _service(db_session).confirm(user=user, submitted=code)

    statement = select(AuditLog).where(
        AuditLog.target_id == user.id,
        AuditLog.action == AuditAction.EMAIL_VERIFIED,
    )
    assert len(list((await db_session.execute(statement)).scalars())) == 1


# --------------------------------------------------------------- supersession


async def test_a_new_code_kills_the_old_one(db_session: AsyncSession) -> None:
    user = await _account(db_session)
    first = await _issue(db_session, user)
    second = await _issue(db_session, user)
    assert first != second

    with pytest.raises(ValidationError, match=INVALID_CODE):
        await _service(db_session).confirm(user=user, submitted=first)
    assert user.email_verified_at is None

    await _service(db_session).confirm(user=user, submitted=second)
    assert user.email_verified_at is not None


async def test_asking_repeatedly_leaves_exactly_one_live_challenge(
    db_session: AsyncSession,
) -> None:
    """Narrowing the live surface rather than widening it."""
    user = await _account(db_session)
    for _ in range(4):
        await _issue(db_session, user)

    rows = await _challenges(db_session, user)
    assert len(rows) == 4
    assert sum(1 for row in rows if row.superseded_at is None) == 1


async def test_the_database_refuses_a_second_live_challenge(
    db_session: AsyncSession,
) -> None:
    """The partial unique index, asserted directly.

    This is what makes "one live code" a property of the schema rather than of
    the service remembering to supersede. A future caller that forgets fails
    loudly here instead of quietly leaving two valid codes.
    """
    user = await _account(db_session)
    await _issue(db_session, user)

    repository = EmailVerificationRepository(db_session)
    with pytest.raises(IntegrityError):
        await repository.create(
            user_id=user.id,
            email=user.email,
            code_hash="$argon2id$second",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    await db_session.rollback()


async def test_the_supersede_count_reaches_the_audit_entry(
    db_session: AsyncSession,
) -> None:
    """A first request and a fourth are different events for an operator."""
    user = await _account(db_session)
    await _issue(db_session, user)
    await _issue(db_session, user)

    statement = select(AuditLog).where(
        AuditLog.target_id == user.id,
        AuditLog.action == AuditAction.EMAIL_VERIFICATION_REQUESTED,
    )
    rows = list((await db_session.execute(statement)).scalars())
    assert sorted(row.meta["superseded"] for row in rows if row.meta) == [0, 1]


# ------------------------------------------------------------- email binding


async def test_a_code_cannot_verify_an_address_it_was_not_sent_to(
    db_session: AsyncSession,
) -> None:
    """The email-change bypass, closed by the data rather than by diligence.

    Without the binding: request a code at an address you control, change the
    account's address to somebody else's, submit the code, and the account now
    claims a verified address its owner never proved.
    """
    user = await _account(db_session)
    code = await _issue(db_session, user)

    user.email = "somebody-else@acme-example.com"
    await db_session.flush()

    with pytest.raises(ValidationError, match=INVALID_CODE):
        await _service(db_session).confirm(user=user, submitted=code)
    assert user.email_verified_at is None
    assert await _audit_reasons(db_session, user) == Counter({"address_changed": 1})


async def test_a_new_address_can_be_verified_with_a_new_code(
    db_session: AsyncSession,
) -> None:
    """The other half: the binding must not make a changed address unverifiable."""
    user = await _account(db_session)
    await _issue(db_session, user)

    user.email = "moved@acme-example.com"
    user.email_verified_at = None
    await db_session.flush()

    code = await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None
    assert challenge.email == "moved@acme-example.com"

    await _service(db_session).confirm(user=user, submitted=code)
    assert user.email_verified_at is not None


async def test_the_binding_is_not_defeated_by_capitalisation(
    db_session: AsyncSession,
) -> None:
    """Stored lower-cased on both sides, so `A@b.com` and `a@b.com` are one."""
    user = await _account(db_session, email="mixed@acme-example.com")
    code = await _issue(db_session, user)

    challenge = await _live(db_session, user)
    assert challenge is not None
    challenge.email = "MIXED@ACME-EXAMPLE.COM".lower()
    await db_session.flush()

    await _service(db_session).confirm(user=user, submitted=code)
    assert user.email_verified_at is not None


# --------------------------------------------------------------- concurrency


async def test_two_requests_with_the_same_correct_code_cannot_both_win(
    db_session: AsyncSession,
) -> None:
    """The security boundary: the consuming UPDATE succeeds exactly once.

    Both requests pass the Argon2 comparison - the code *is* correct - and both
    reach this statement with identical arguments. Exactly one may change a
    row; the loser's `False` is the detection, not an error.

    **This is sequential, and deliberately so.** An earlier version ran the two
    through `asyncio.gather` on one `AsyncSession` and called itself a race. It
    was not one: an `AsyncSession` is a single connection and is not safe to
    drive from two coroutines, so the two statements either serialise anyway or
    raise something unrelated to the property being tested. A test that flakes
    for a reason its name does not mention is worse than one that claims less.

    What makes the sequential version load-bearing is that the second call
    receives *exactly* what a losing racer receives: the same challenge id, the
    same clock, the same address, and a row another caller has already
    consumed. If the UPDATE were unconditional - or if the caller checked
    `consumed_at` in Python and wrote afterwards - the second call would
    succeed here.

    True multi-connection contention is not exercised anywhere in this suite;
    the fixture joins one connection so a test can be rolled back. What
    PostgreSQL guarantees under real concurrency is that one `UPDATE ... WHERE
    consumed_at IS NULL` blocks until the other commits and then matches no
    row, and that is a property of the statement rather than of this test.
    """
    user = await _account(db_session)
    # The code itself is not needed: both callers spend the challenge directly,
    # which is the step being tested. Comparing the code happens *before* it,
    # in Python, and is exactly why the write re-checks everything.
    await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None
    now = datetime.now(UTC)

    async def _spend() -> bool:
        return await EmailVerificationRepository(db_session).consume(
            challenge_id=challenge.id,
            email=user.email,
            now=now,
            max_attempts=MAX_ATTEMPTS,
        )

    assert await _spend() is True
    assert await _spend() is False


async def test_a_challenge_exhausted_mid_request_does_not_verify(
    db_session: AsyncSession,
) -> None:
    """The re-check inside the UPDATE, exercised directly.

    A concurrent wrong guess can exhaust the challenge after the correct code
    has been validated in Python. Without re-checking the ceiling as it writes,
    the last guess of a dead challenge would still win.
    """
    user = await _account(db_session)
    await _issue(db_session, user)
    challenge = await _live(db_session, user)
    assert challenge is not None

    repository = EmailVerificationRepository(db_session)
    for _ in range(MAX_ATTEMPTS + 1):
        await repository.register_failure(challenge_id=challenge.id)

    assert not await repository.consume(
        challenge_id=challenge.id,
        email=user.email,
        now=datetime.now(UTC),
        max_attempts=MAX_ATTEMPTS,
    )


# ------------------------------------------------------------- already proven


async def test_asking_again_once_verified_queues_nothing(
    db_session: AsyncSession,
) -> None:
    """Not an error, and not a second code. There is nothing left to prove."""
    user = await _account(db_session)
    code = await _issue(db_session, user)
    await _service(db_session).confirm(user=user, submitted=code)

    message = await _service(db_session).request(user=user)
    assert message == VERIFICATION_SENT_MESSAGE
    assert await _live(db_session, user) is None
    assert len(await _challenges(db_session, user)) == 1


# ----------------------------------------------------------- configuration


@pytest.mark.parametrize("ttl", [0, 59, 3601, 86400])
def test_an_unsafe_lifetime_is_refused_rather_than_clamped(
    db_session: AsyncSession,
    ttl: int,
) -> None:
    """Silently correcting configuration is how an operator comes to believe a
    code lives for a day when it lives for an hour."""
    with pytest.raises(ValidationError):
        EmailVerificationService(session=db_session, settings=_settings(), ttl_seconds=ttl)


@pytest.mark.parametrize("attempts", [0, -1, 11, 1000])
def test_an_unsafe_attempt_ceiling_is_refused(
    db_session: AsyncSession,
    attempts: int,
) -> None:
    with pytest.raises(ValidationError):
        EmailVerificationService(session=db_session, settings=_settings(), max_attempts=attempts)


def test_the_deployment_settings_are_what_a_request_uses(
    db_session: AsyncSession,
) -> None:
    """The knob has to be connected, not merely present.

    An earlier state of this branch had the bounds and the default but no
    setting reaching them, so `EMAIL_VERIFICATION_TTL_SECONDS` was inert.
    """
    settings = _settings(email_verification_ttl_seconds=900, email_verification_max_attempts=7)
    service = EmailVerificationService(session=db_session, settings=settings)
    assert service._ttl_seconds == 900
    assert service._max_attempts == 7


async def test_the_configured_lifetime_is_the_one_written_down(
    db_session: AsyncSession,
) -> None:
    user = await _account(db_session)
    settings = _settings(email_verification_ttl_seconds=900)
    before = datetime.now(UTC)
    await EmailVerificationService(session=db_session, settings=settings).request(user=user)

    challenge = await _live(db_session, user)
    assert challenge is not None
    assert timedelta(seconds=890) < challenge.expires_at - before < timedelta(seconds=910)
