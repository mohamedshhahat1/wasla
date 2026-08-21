"""The follow-up worker sweeping a real database.

What matters here is the loop's failure containment and its use of the claim,
not the sending — that is covered against the service in `test_follow_ups.py`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.workers.follow_up_worker import FollowUpWorker

pytestmark = pytest.mark.integration


class SessionHandle:
    """Hands the worker the test's own session.

    The worker opens `database.session()` per sweep; here that yields the
    transaction the fixture already owns, so everything the sweep writes is
    rolled back with the test.
    """

    def __init__(self, session) -> None:
        self._session = session
        self.opened = 0

    @asynccontextmanager
    async def session(self):
        self.opened += 1
        yield self._session


async def _due_follow_up(session, *, slug: str, wa_id: str, minutes_late: int = 5) -> FollowUp:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{slug}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id=wa_id)
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        status=ConversationStatus.OPEN,
        mode=ConversationMode.AI,
        # Inside the service window, so free text is allowed.
        last_inbound_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(conversation)
    await session.flush()

    follow_up = FollowUp(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=minutes_late),
        body="Still there?",
    )
    session.add(follow_up)
    await session.flush()
    return follow_up


class StubMessaging:
    """Sends nothing and says it worked.

    Injected through the worker's `messaging_factory`, so the sweep exercises
    the real claim, the real service and the real status transitions without a
    WhatsApp account or a network call.
    """

    def __init__(self, session, tenant_id) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def window_open(self, conversation) -> bool:
        return True

    async def send_text(self, *, conversation_id, body, **kwargs):
        message = Message(
            tenant_id=self._tenant_id,
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            kind=MessageKind.TEXT,
            status=MessageStatus.SENT,
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def send_template(self, **kwargs):
        raise AssertionError("These follow-ups are inside the window.")


def _worker(session, **kwargs) -> FollowUpWorker:
    return FollowUpWorker(
        database=SessionHandle(session),  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]
        messaging_factory=lambda db, tenant_id: StubMessaging(db, tenant_id),  # type: ignore[arg-type,return-value]
        **kwargs,
    )


async def test_a_sweep_with_nothing_due_does_no_work(db_session):
    handled = await _worker(db_session).run_once()

    assert handled == 0


async def test_a_sweep_handles_a_due_follow_up(db_session):
    follow_up = await _due_follow_up(db_session, slug="acme", wa_id="201000000001")

    handled = await _worker(db_session).run_once()

    assert handled == 1
    assert follow_up.status is FollowUpStatus.SENT
    assert follow_up.sent_at is not None
    assert follow_up.message_id is not None


async def test_a_sweep_crosses_workspaces(db_session):
    """One worker serves the whole platform, unlike everything on the request path."""
    await _due_follow_up(db_session, slug="acme", wa_id="201000000002")
    await _due_follow_up(db_session, slug="rival", wa_id="201000000003")

    handled = await _worker(db_session).run_once()

    assert handled == 2


async def test_a_sweep_is_bounded_by_its_claim_limit(db_session):
    for index in range(4):
        await _due_follow_up(db_session, slug=f"tenant{index}", wa_id=f"20100000001{index}")

    handled = await _worker(db_session, claim_limit=2).run_once()

    assert handled == 2


async def test_a_follow_up_not_yet_due_is_left_alone(db_session):
    follow_up = await _due_follow_up(
        db_session,
        slug="acme",
        wa_id="201000000004",
        minutes_late=-120,
    )

    handled = await _worker(db_session).run_once()

    assert handled == 0
    assert follow_up.status is FollowUpStatus.PENDING


async def test_one_broken_follow_up_does_not_strand_the_others(db_session, monkeypatch):
    """A single bad row must not block every other workspace's nudges."""
    await _due_follow_up(db_session, slug="acme", wa_id="201000000005")
    await _due_follow_up(db_session, slug="rival", wa_id="201000000006")

    import app.workers.follow_up_worker as module

    calls = {"count": 0}
    original = module.FollowUpService

    class Exploding(original):  # type: ignore[misc, valid-type]
        async def dispatch(self, follow_up):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("Something unexpected.")
            return await super().dispatch(follow_up)

    monkeypatch.setattr(module, "FollowUpService", Exploding)

    handled = await _worker(db_session).run_once()

    # Two attempted, one survived the explosion.
    assert calls["count"] == 2
    assert handled == 1


async def test_a_failing_sweep_does_not_kill_the_loop(db_session, monkeypatch):
    """Otherwise every later follow-up goes unsent and nothing says why."""
    worker = _worker(db_session)
    sweeps = {"count": 0}

    async def explode(**kwargs):
        sweeps["count"] += 1
        worker.stop()
        raise RuntimeError("The database went away.")

    monkeypatch.setattr(worker, "run_once", explode)

    # Returns rather than propagating: the loop caught it and then stopped
    # because the sweep asked it to.
    await worker.run_forever()

    assert sweeps["count"] == 1


async def test_stopping_wakes_a_sleeping_worker_immediately(db_session):
    """Shutdown must not wait out a full poll interval."""
    import asyncio

    worker = _worker(db_session, poll_seconds=3600)
    task = asyncio.create_task(worker.run_forever())
    # Let it complete one sweep and enter its sleep.
    await asyncio.sleep(0.1)

    worker.stop()
    await asyncio.wait_for(task, timeout=5)

    assert True


async def test_the_worker_opens_one_session_per_sweep(db_session):
    handle = SessionHandle(db_session)
    worker = FollowUpWorker(
        database=handle,  # type: ignore[arg-type]
        settings=object(),  # type: ignore[arg-type]
        messaging_factory=lambda db, tenant_id: StubMessaging(db, tenant_id),  # type: ignore[arg-type,return-value]
    )

    await worker.run_once()
    await worker.run_once()

    assert handle.opened == 2


async def test_a_claimed_follow_up_belongs_to_its_own_workspace(db_session):
    """The sweep crosses tenants; the service it hands each row to must not."""
    follow_up = await _due_follow_up(db_session, slug="acme", wa_id="201000000007")
    tenant_id = follow_up.tenant_id

    await _worker(db_session).run_once()

    assert follow_up.tenant_id == tenant_id
    assert isinstance(tenant_id, uuid.UUID)
