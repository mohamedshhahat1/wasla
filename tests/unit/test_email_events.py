"""What a verified provider event is allowed to change.

A verified signature says the delivery came from the provider. It does not
make the body true, and these tests are the executable form of that
distinction - above all that an address in a payload is never an address this
system acts on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.db.models.email import EmailStatus
from app.services.email_event_service import (
    APPLIED,
    IGNORED,
    UNKNOWN,
    EmailEventService,
)

OURS = "member@customer.example"
VICTIM = "someone-else@another-company.example"
PROVIDER_ID = "re_2abcDEF"


@dataclass
class _Row:
    """The fields of an outbox row this service reads."""

    recipient: str = OURS
    status: EmailStatus = EmailStatus.SENT
    template: str = "password_reset"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID | None = None


@dataclass
class _StubRepository:
    """Records what the service asked for rather than performing it."""

    row: _Row | None = None
    delivered: list[uuid.UUID] = field(default_factory=list)
    failed: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    suppressed: list[tuple[str, str]] = field(default_factory=list)
    looked_up: list[str] = field(default_factory=list)

    async def get_by_provider_message_id(self, provider_message_id: str) -> _Row | None:
        self.looked_up.append(provider_message_id)
        return self.row

    async def mark_delivered(self, email: _Row, *, now: Any) -> None:
        self.delivered.append(email.id)
        email.status = EmailStatus.DELIVERED

    async def mark_failed(
        self,
        email: _Row,
        *,
        now: Any,
        error_code: str,
        error_message: str,
    ) -> None:
        self.failed.append((email.id, error_code))
        email.status = EmailStatus.FAILED

    async def suppress(self, recipient: str, *, reason: str) -> None:
        self.suppressed.append((recipient, reason))


def _service(repository: _StubRepository) -> EmailEventService:
    """The service with its repository replaced.

    No session: every read and write in `record` goes through the repository,
    so a stub is enough to exercise each decision without a database.
    """
    service = EmailEventService(None)  # type: ignore[arg-type]
    service._repository = repository  # type: ignore[assignment]
    return service


def _event(kind: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "data": {"email_id": PROVIDER_ID, **data}}


@pytest.mark.asyncio
async def test_a_delivered_event_records_the_delivery():
    repository = _StubRepository(row=_Row())

    outcome = await _service(repository).record(_event("email.delivered"))

    assert outcome == APPLIED
    assert repository.delivered == [repository.row.id]
    assert repository.suppressed == []


@pytest.mark.asyncio
async def test_an_event_naming_a_message_we_never_sent_changes_nothing():
    # The lookup is the trust boundary. Without it, every field below would be
    # attacker-chosen input to a write.
    repository = _StubRepository(row=None)

    outcome = await _service(repository).record(_event("email.bounced", bounce={"type": "Permanent"}))

    assert outcome == UNKNOWN
    assert repository.suppressed == []
    assert repository.failed == []
    assert repository.delivered == []


@pytest.mark.asyncio
async def test_a_forged_bounce_cannot_suppress_an_address_we_never_wrote_to():
    # The attack this design exists to refuse: a bounce naming somebody else's
    # mailbox, to stop this platform ever mailing it. The address suppressed is
    # the one on our own row, so the payload's address is inert.
    repository = _StubRepository(row=_Row(recipient=OURS))

    await _service(repository).record(
        _event(
            "email.bounced",
            bounce={"type": "Permanent"},
            to=[VICTIM],
            **{"from": VICTIM},
        )
    )

    assert repository.suppressed == [(OURS, "hard_bounce")]
    assert VICTIM not in [address for address, _ in repository.suppressed]


@pytest.mark.asyncio
async def test_a_permanent_bounce_suppresses_the_address_and_fails_the_message():
    repository = _StubRepository(row=_Row())

    outcome = await _service(repository).record(
        _event("email.bounced", bounce={"type": "Permanent"})
    )

    assert outcome == APPLIED
    assert repository.suppressed == [(OURS, "hard_bounce")]
    assert repository.failed == [(repository.row.id, "bounced")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bounce",
    [{"type": "Transient"}, {"type": "Undetermined"}, {}, None],
)
async def test_a_bounce_that_is_not_permanent_suppresses_nothing(bounce):
    # A full mailbox or a greylist. Refusing to write to the address again
    # would turn a temporary condition into a permanent one - and the address
    # in question is where somebody's password reset goes.
    repository = _StubRepository(row=_Row())

    await _service(repository).record(_event("email.bounced", bounce=bounce))

    assert repository.suppressed == []
    assert repository.failed == []


@pytest.mark.asyncio
async def test_a_complaint_suppresses_without_failing_a_message_that_arrived():
    repository = _StubRepository(row=_Row())

    await _service(repository).record(_event("email.complained"))

    assert repository.suppressed == [(OURS, "complaint")]
    assert repository.failed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["email.opened", "email.clicked", "email.sent", "email.delivery_delayed"],
)
async def test_events_that_prove_nothing_are_dropped(kind):
    # Opens and clicks are an image proxy and a link scanner. Nothing is stored,
    # so nothing can later be mistaken for evidence that a person read anything.
    repository = _StubRepository(row=_Row())

    outcome = await _service(repository).record(_event(kind))

    assert outcome == IGNORED
    assert repository.looked_up == []
    assert repository.suppressed == []
    assert repository.failed == []


@pytest.mark.asyncio
async def test_replaying_an_event_lands_on_the_same_state():
    # The replay story: repetition is harmless by construction, which is why
    # there is no table of seen delivery ids to keep.
    repository = _StubRepository(row=_Row())
    service = _service(repository)

    await service.record(_event("email.delivered"))
    await service.record(_event("email.delivered"))

    assert repository.row.status is EmailStatus.DELIVERED
    assert repository.failed == []


@pytest.mark.asyncio
async def test_a_late_failure_does_not_undo_an_observed_delivery():
    # Otherwise the final status would depend on the order the webhooks
    # happened to arrive in.
    repository = _StubRepository(row=_Row(status=EmailStatus.DELIVERED))

    await _service(repository).record(_event("email.failed"))

    assert repository.failed == []
    assert repository.row.status is EmailStatus.DELIVERED


@pytest.mark.asyncio
async def test_a_failure_event_fails_a_sent_message():
    repository = _StubRepository(row=_Row())

    await _service(repository).record(_event("email.failed"))

    assert repository.failed == [(repository.row.id, "provider_failed")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"type": "email.delivered"},
        {"type": "email.delivered", "data": None},
        {"type": "email.delivered", "data": "not-a-mapping"},
        {"type": "email.delivered", "data": {}},
        {"type": "email.delivered", "data": {"email_id": ""}},
        {"type": "email.delivered", "data": {"email_id": 12345}},
        {"type": None, "data": {"email_id": PROVIDER_ID}},
        {"type": "something.invented", "data": {"email_id": PROVIDER_ID}},
    ],
)
async def test_a_malformed_payload_is_dropped_not_guessed_at(payload):
    # A verified signature says who sent it, not that it is well-formed.
    repository = _StubRepository(row=_Row())

    outcome = await _service(repository).record(payload)

    assert outcome == IGNORED
    assert repository.suppressed == []
    assert repository.failed == []
    assert repository.delivered == []
