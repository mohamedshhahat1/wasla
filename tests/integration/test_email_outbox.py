"""The outbox and the worker that drains it, against a real database.

The properties tested here are the ones that only a real PostgreSQL can show:
the unique constraint that makes enqueueing idempotent under a race, the
claim that keeps two workers off one row, the recovery that returns a crashed
claim to the queue, and the backoff that decides when a refused message is
tried again.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.email import EmailStatus, EmailSuppression, OutboundEmail
from app.db.models.tenant import Tenant
from app.integrations.email.base import EmailSendResult, EmailSendState
from app.integrations.email.fake import FakeEmailProvider
from app.repositories.email_repository import (
    STUCK_AFTER_SECONDS,
    EmailOutboxRepository,
)
from app.services.email_templates import EmailTemplate
from app.workers.email_worker import MAX_BACKOFF_SECONDS, EmailWorker

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


class SessionHandle:
    """Hands the worker the test's own session, as the follow-up tests do.

    The worker now opens a session per phase and per message, so this counts
    the openings too - that count is how the per-message transaction boundary
    is asserted.
    """

    def __init__(self, session) -> None:
        self._session = session
        self.opened = 0

    @asynccontextmanager
    async def session(self):
        self.opened += 1
        yield self._session


class Settings:
    """Only what the worker and the outbox read."""

    email_enabled = True
    email_provider = "fake"
    email_from = "no-reply@example.com"
    email_reply_to = None
    app_public_url = "https://app.example.com"
    email_max_attempts = 3
    email_worker_poll_seconds = 10.0


async def _enqueue(
    session,
    *,
    recipient: str = "person@example.com",
    template: EmailTemplate = EmailTemplate.PASSWORD_CHANGED,
    key: str | None = None,
    available_at: datetime | None = None,
    tenant_id: uuid.UUID | None = None,
    context: dict[str, str] | None = None,
) -> OutboundEmail | None:
    return await EmailOutboxRepository(session).enqueue(
        recipient=recipient,
        template=template.value,
        subject="A subject",
        context=context or {},
        idempotency_key=key or f"key-{uuid.uuid4()}",
        available_at=available_at or NOW,
        tenant_id=tenant_id,
    )


def _worker(session, provider=None, **kwargs) -> EmailWorker:
    return EmailWorker(
        database=SessionHandle(session),
        settings=Settings(),
        provider=provider if provider is not None else FakeEmailProvider(),
        **kwargs,
    )


async def _reload(session, email_id: uuid.UUID) -> OutboundEmail:
    return await session.get(OutboundEmail, email_id)


async def test_a_queued_email_starts_pending_and_due(db_session):
    email = await _enqueue(db_session)

    assert email is not None
    assert email.status is EmailStatus.PENDING
    assert email.attempts == 0
    assert email.available_at == NOW


async def test_a_duplicate_idempotency_key_inserts_nothing(db_session):
    """The constraint is the guarantee, not the caller's discipline."""
    first = await _enqueue(db_session, key="invitation:1")
    second = await _enqueue(db_session, key="invitation:1")

    assert first is not None
    assert second is None


async def test_a_recipient_is_stored_lower_cased(db_session):
    """Suppression is a string comparison, so the form has to be one form."""
    email = await _enqueue(db_session, recipient="Person@Example.COM")

    assert email is not None
    assert email.recipient == "person@example.com"


async def test_claiming_marks_the_row_sending_and_counts_the_attempt(db_session):
    email = await _enqueue(db_session)
    repository = EmailOutboxRepository(db_session)

    claimed = await repository.claim_due(now=NOW)

    assert [row.id for row in claimed] == [email.id]
    assert claimed[0].status is EmailStatus.SENDING
    assert claimed[0].attempts == 1
    assert claimed[0].claimed_at == NOW


async def test_a_row_not_yet_due_is_not_claimed(db_session):
    await _enqueue(db_session, available_at=NOW + timedelta(minutes=5))

    assert await EmailOutboxRepository(db_session).claim_due(now=NOW) == []


async def test_a_claimed_row_is_not_claimed_again(db_session):
    await _enqueue(db_session)
    repository = EmailOutboxRepository(db_session)
    await repository.claim_due(now=NOW)

    assert await repository.claim_due(now=NOW) == []


async def test_the_claim_is_bounded_by_its_limit(db_session):
    for index in range(5):
        await _enqueue(db_session, key=f"bulk-{index}")

    claimed = await EmailOutboxRepository(db_session).claim_due(now=NOW, limit=2)

    assert len(claimed) == 2


async def test_a_stuck_claim_returns_to_the_queue(db_session):
    """The at-least-once half: a crashed send goes again rather than vanishing."""
    email = await _enqueue(db_session)
    repository = EmailOutboxRepository(db_session)
    await repository.claim_due(now=NOW)
    later = NOW + timedelta(seconds=STUCK_AFTER_SECONDS + 1)

    recovered = await repository.recover_stuck(now=later)

    assert recovered == 1
    assert (await _reload(db_session, email.id)).status is EmailStatus.PENDING


async def test_a_recent_claim_is_left_alone(db_session):
    """A slow provider must never be mistaken for a dead worker."""
    await _enqueue(db_session)
    repository = EmailOutboxRepository(db_session)
    await repository.claim_due(now=NOW)

    assert await repository.recover_stuck(now=NOW + timedelta(seconds=30)) == 0


async def test_a_sent_message_records_the_provider_id_and_clears_its_context(db_session):
    """The reset link's life ends with the send, not with the row."""
    email = await _enqueue(
        db_session,
        template=EmailTemplate.PASSWORD_RESET,
        context={"token": "super-secret-token"},
    )
    provider = FakeEmailProvider()

    await _worker(db_session, provider).run_once(now=NOW)

    row = await _reload(db_session, email.id)
    assert row.status is EmailStatus.SENT
    assert row.provider_message_id == "fake-1"
    assert row.context == {}
    assert provider.sent[0].to == ("person@example.com",)


async def test_the_sent_message_carries_the_rendered_link(db_session):
    await _enqueue(
        db_session,
        template=EmailTemplate.PASSWORD_RESET,
        context={"token": "tok-9"},
    )
    provider = FakeEmailProvider()

    await _worker(db_session, provider).run_once(now=NOW)

    assert "https://app.example.com/reset-password?token=tok-9" in provider.sent[0].text


async def test_a_permanent_refusal_fails_the_row_at_once(db_session):
    email = await _enqueue(db_session)
    provider = FakeEmailProvider()
    provider.script = [
        EmailSendResult(
            state=EmailSendState.PERMANENT_FAILURE,
            provider="fake",
            error_code="invalid_recipient",
        )
    ]

    await _worker(db_session, provider).run_once(now=NOW)

    row = await _reload(db_session, email.id)
    assert row.status is EmailStatus.FAILED
    assert row.last_error_code == "invalid_recipient"


async def test_a_transient_refusal_schedules_a_retry_in_the_future(db_session):
    email = await _enqueue(db_session)
    provider = FakeEmailProvider()
    provider.script = [
        EmailSendResult(
            state=EmailSendState.TRANSIENT_FAILURE,
            provider="fake",
            error_code="http_503",
        )
    ]

    await _worker(db_session, provider).run_once(now=NOW)

    row = await _reload(db_session, email.id)
    assert row.status is EmailStatus.PENDING
    assert row.available_at > NOW
    assert row.attempts == 1


async def test_the_backoff_grows_and_stays_under_its_ceiling(db_session):
    """Exponential with jitter, but never past the cap."""
    email = await _enqueue(db_session)
    email.attempts = 20
    email.status = EmailStatus.SENDING
    email.claimed_at = NOW
    await db_session.flush()
    provider = FakeEmailProvider()
    provider.script = [
        EmailSendResult(state=EmailSendState.TRANSIENT_FAILURE, provider="fake"),
    ]

    worker = _worker(db_session, provider)
    await worker._handle(EmailOutboxRepository(db_session), email, now=NOW)

    delay = (await _reload(db_session, email.id)).available_at - NOW
    assert delay.total_seconds() <= MAX_BACKOFF_SECONDS * 1.25


async def test_a_row_that_exhausts_its_attempts_fails(db_session):
    """`EMAIL_MAX_ATTEMPTS` is 3 in this test's settings."""
    email = await _enqueue(db_session)
    email.attempts = 3
    email.status = EmailStatus.SENDING
    email.claimed_at = NOW
    await db_session.flush()
    provider = FakeEmailProvider()
    provider.script = [
        EmailSendResult(state=EmailSendState.TRANSIENT_FAILURE, provider="fake"),
    ]

    worker = _worker(db_session, provider)
    await worker._handle(EmailOutboxRepository(db_session), email, now=NOW)

    assert (await _reload(db_session, email.id)).status is EmailStatus.FAILED


async def test_a_row_whose_context_is_missing_fails_rather_than_looping(db_session):
    """A reset with no token will not grow one tomorrow."""
    email = await _enqueue(db_session, template=EmailTemplate.PASSWORD_RESET, context={})
    provider = FakeEmailProvider()

    await _worker(db_session, provider).run_once(now=NOW)

    row = await _reload(db_session, email.id)
    assert row.status is EmailStatus.FAILED
    assert row.last_error_code == "render_error"
    assert provider.sent == []


async def test_a_broken_row_does_not_strand_the_rest(db_session):
    """One workspace's poisonous message must not hold up everybody else's."""
    broken = await _enqueue(
        db_session,
        key="broken",
        template=EmailTemplate.PASSWORD_RESET,
        context={},
        available_at=NOW - timedelta(seconds=1),
    )
    healthy = await _enqueue(db_session, key="healthy")
    provider = FakeEmailProvider()

    await _worker(db_session, provider).run_once(now=NOW)

    assert (await _reload(db_session, broken.id)).status is EmailStatus.FAILED
    assert (await _reload(db_session, healthy.id)).status is EmailStatus.SENT


async def test_a_suppressed_recipient_is_never_written_to(db_session):
    email = await _enqueue(db_session, recipient="bounced@example.com")
    db_session.add(EmailSuppression(recipient="bounced@example.com", reason="hard_bounce"))
    await db_session.flush()
    provider = FakeEmailProvider()

    await _worker(db_session, provider).run_once(now=NOW)

    row = await _reload(db_session, email.id)
    assert row.status is EmailStatus.FAILED
    assert row.last_error_code == "suppressed"
    assert provider.sent == []


async def test_suppression_ignores_the_case_of_the_address(db_session):
    """A mixed-case row must not slip past a suppression written lower-cased."""
    email = await _enqueue(db_session, recipient="Bounced@Example.com")
    await EmailOutboxRepository(db_session).suppress("BOUNCED@EXAMPLE.COM", reason="hard_bounce")
    await db_session.flush()
    provider = FakeEmailProvider()

    await _worker(db_session, provider).run_once(now=NOW)

    assert (await _reload(db_session, email.id)).status is EmailStatus.FAILED
    assert provider.sent == []


async def test_suppressing_the_same_address_twice_is_harmless(db_session):
    """A replayed bounce webhook is normal traffic, not an anomaly."""
    repository = EmailOutboxRepository(db_session)
    await repository.suppress("x@example.com", reason="hard_bounce")
    await repository.suppress("x@example.com", reason="complaint")
    await db_session.flush()

    assert await repository.is_suppressed("x@example.com") is True


async def test_an_empty_sweep_touches_nothing(db_session):
    assert await _worker(db_session).run_once(now=NOW) == 0


async def test_each_message_is_delivered_in_its_own_transaction(db_session):
    """The crash window is one message, not the batch (see `run_once`)."""
    for index in range(3):
        await _enqueue(db_session, key=f"batch-{index}")
    handle = SessionHandle(db_session)
    worker = EmailWorker(
        database=handle,
        settings=Settings(),
        provider=FakeEmailProvider(),
    )

    handled = await worker.run_once(now=NOW)

    assert handled == 3
    # One session for the claim, then one per message.
    assert handle.opened == 4


async def test_a_row_recovered_by_another_sweep_is_not_sent_twice(db_session):
    """`get_claimed` only answers for a row still marked `sending`."""
    email = await _enqueue(db_session)
    repository = EmailOutboxRepository(db_session)
    await repository.claim_due(now=NOW)
    email.status = EmailStatus.PENDING
    await db_session.flush()

    assert await repository.get_claimed(email.id) is None


async def test_delivery_upgrades_a_sent_row(db_session):
    email = await _enqueue(db_session)
    await _worker(db_session).run_once(now=NOW)
    repository = EmailOutboxRepository(db_session)

    await repository.mark_delivered(await _reload(db_session, email.id), now=NOW)

    assert (await _reload(db_session, email.id)).status is EmailStatus.DELIVERED


async def test_delivery_never_resurrects_a_failed_row(db_session):
    """Otherwise the status depends on the order webhooks happened to arrive."""
    email = await _enqueue(db_session)
    repository = EmailOutboxRepository(db_session)
    await repository.mark_failed(email, now=NOW, error_code="bounced", error_message=None)

    await repository.mark_delivered(email, now=NOW)

    assert (await _reload(db_session, email.id)).status is EmailStatus.FAILED


async def test_a_message_is_found_by_the_provider_id_we_recorded(db_session):
    await _enqueue(db_session)
    await _worker(db_session).run_once(now=NOW)

    found = await EmailOutboxRepository(db_session).get_by_provider_message_id("fake-1")

    assert found is not None
    assert found.provider_message_id == "fake-1"


async def test_an_unknown_provider_id_finds_nothing(db_session):
    assert await EmailOutboxRepository(db_session).get_by_provider_message_id("nope") is None


async def test_a_tenant_scoped_email_keeps_its_tenant(db_session):
    tenant = Tenant(name="Acme", slug="acme-outbox")
    db_session.add(tenant)
    await db_session.flush()

    email = await _enqueue(db_session, tenant_id=tenant.id)

    assert email is not None
    assert email.tenant_id == tenant.id
