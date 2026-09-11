"""A phone number changing hands, and where the traffic in flight ends up.

Meta retries an undelivered webhook for up to seven days. So for a week after a
number moves between workspaces, a delivery can arrive carrying a message the
*previous* owner's customer sent - and resolving the workspace from whoever
holds the number now puts that message, the customer's phone number and their
profile name into a stranger's inbox, readable through `GET /conversations`.
Both businesses proved ownership of the number to Meta, so neither did anything
wrong, which makes it worse rather than better: there is no misuse to detect
(MSG-01).

The same resolution dropped the releasing workspace's delivery statuses, so
every message it had in flight at release time stayed `sent` with a null
`delivered_at` for ever (MSG-04).

The matrix below is the contract, stated once:

    before A claimed          -> nobody; dropped and counted
    during A's tenure         -> A
    after A released,
      before B claimed        -> nobody; dropped and counted
    during B's tenure         -> B
    status for A's message,
      arriving during B's     -> A's message, by provider id

The property being protected is cross-workspace disclosure, not chronology.
That distinction is what `test_a_slightly_early_message_on_a_number_that_never_moved`
pins: a provider timestamp can legitimately precede the claim it belongs to,
and refusing those would drop real customer messages on a number nobody has
ever handed over.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import (
    Contact,
    Message,
    MessageDirection,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import (
    WhatsAppAccount,
    WhatsAppAccountStatus,
    WhatsAppEvent,
)
from app.services.whatsapp_service import WhatsAppIngestionService

pytestmark = pytest.mark.integration

NUMBER = "PN-MOVES"
CUSTOMER = "201777000001"

# A timeline with a gap in it, because the gap is one of the cases.
NOW = datetime.now(UTC)
A_CLAIMED = NOW - timedelta(days=30)
A_RELEASED = NOW - timedelta(days=10)
B_CLAIMED = NOW - timedelta(days=5)

BEFORE_ANYBODY = A_CLAIMED - timedelta(days=1)
DURING_A = A_CLAIMED + timedelta(days=1)
IN_THE_GAP = A_RELEASED + timedelta(days=1)
DURING_B = B_CLAIMED + timedelta(days=1)


def _inbound(wamid: str, *, at: datetime, body: str) -> dict[str, object]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": NUMBER},
                            "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Yara"}}],
                            "messages": [
                                {
                                    "from": CUSTOMER,
                                    "id": wamid,
                                    "type": "text",
                                    "timestamp": str(int(at.timestamp())),
                                    "text": {"body": body},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


def _status(wamid: str, *, status: str, at: datetime) -> dict[str, object]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": NUMBER},
                            "statuses": [
                                {
                                    "id": wamid,
                                    "status": status,
                                    "timestamp": str(int(at.timestamp())),
                                    "recipient_id": CUSTOMER,
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


async def _tenant(session: AsyncSession, slug: str) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=f"{slug}-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _claim(
    session: AsyncSession,
    tenant: Tenant,
    *,
    started_at: datetime,
    released_at: datetime | None,
    phone_number_id: str = NUMBER,
) -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=phone_number_id,
        waba_id=f"waba-{tenant.slug}",
        display_phone_number="+20 100 000 0002",
        status=(
            WhatsAppAccountStatus.RELEASED
            if released_at is not None
            else WhatsAppAccountStatus.ACTIVE
        ),
        ownership_started_at=started_at,
        ownership_verified_at=started_at,
        released_at=released_at,
    )
    session.add(account)
    await session.flush()
    return account


@pytest.fixture
async def handover(db_session: AsyncSession) -> tuple[Tenant, Tenant]:
    """A owned the number, released it; B claimed it five days later."""
    first = await _tenant(db_session, "alpha")
    second = await _tenant(db_session, "beta")
    await _claim(db_session, first, started_at=A_CLAIMED, released_at=A_RELEASED)
    await _claim(db_session, second, started_at=B_CLAIMED, released_at=None)
    return first, second


async def _ingest(session: AsyncSession, payload: dict[str, object]) -> object:
    outcome = await WhatsAppIngestionService(session=session).ingest(payload)
    await session.flush()
    return outcome


async def _owner_of(session: AsyncSession, wamid: str) -> uuid.UUID | None:
    row = (
        await session.execute(select(Message).where(Message.wa_message_id == wamid))
    ).scalar_one_or_none()
    return None if row is None else row.tenant_id


async def test_a_message_sent_during_the_previous_owners_tenure_never_enters_the_new_one(
    db_session: AsyncSession,
    handover: tuple[Tenant, Tenant],
) -> None:
    """The finding, reproduced and then closed.

    The message arrives long after B has taken the number over. Before the fix
    it became a `Message`, a `Contact` and a `Conversation` inside B, and B's
    agent was queued to answer it.
    """
    first, second = handover
    wamid = f"wamid.{uuid.uuid4().hex}"

    await _ingest(db_session, _inbound(wamid, at=DURING_A, body="sent while A held it"))

    owner = await _owner_of(db_session, wamid)
    assert owner != second.id
    # And better than merely "not B": it goes where it belongs, so the business
    # that was talking to this customer keeps the conversation.
    assert owner == first.id

    # Nothing of the customer's reached B by any route.
    contacts = (
        (await db_session.execute(select(Contact).where(Contact.tenant_id == second.id)))
        .scalars()
        .all()
    )
    assert contacts == []


async def test_a_message_sent_after_the_handover_belongs_to_the_new_owner(
    db_session: AsyncSession,
    handover: tuple[Tenant, Tenant],
) -> None:
    """The negative control. Current traffic must still route currently.

    Without this, "never route to B" would be satisfiable by dropping
    everything, and the number would be useless to the workspace that owns it.
    """
    first, second = handover
    wamid = f"wamid.{uuid.uuid4().hex}"

    await _ingest(db_session, _inbound(wamid, at=DURING_B, body="sent while B holds it"))

    assert await _owner_of(db_session, wamid) == second.id
    contacts = (
        (await db_session.execute(select(Contact).where(Contact.tenant_id == first.id)))
        .scalars()
        .all()
    )
    assert contacts == []


@pytest.mark.parametrize(
    ("label", "at"),
    [("before any claim", BEFORE_ANYBODY), ("in the gap between claims", IN_THE_GAP)],
)
async def test_a_message_nobody_owned_is_dropped_rather_than_guessed_at(
    db_session: AsyncSession,
    handover: tuple[Tenant, Tenant],
    label: str,
    at: datetime,
) -> None:
    """Two workspaces have held this number and neither held it then.

    Dropped and counted, not attributed. Losing a stray message costs one
    conversation; handing it to a stranger cannot be undone, and there is no
    evidence available to this system that would tell the two apart.
    """
    first, second = handover
    wamid = f"wamid.{uuid.uuid4().hex}"

    outcome = await _ingest(db_session, _inbound(wamid, at=at, body=label))

    assert outcome.stored == 0  # type: ignore[attr-defined]
    assert outcome.unowned == 1  # type: ignore[attr-defined]
    # Counted apart from "not our number", which means something else entirely.
    assert outcome.unknown_accounts == 0  # type: ignore[attr-defined]
    assert await _owner_of(db_session, wamid) is None
    for tenant in (first, second):
        contacts = (
            (await db_session.execute(select(Contact).where(Contact.tenant_id == tenant.id)))
            .scalars()
            .all()
        )
        assert contacts == []


async def test_a_slightly_early_message_on_a_number_that_never_moved_is_still_delivered(
    db_session: AsyncSession,
) -> None:
    """Chronology is not the property; disclosure is.

    A provider timestamp can precede the claim it belongs to - clocks drift,
    and Meta can hold a message sent moments before a claim committed. On a
    number only one workspace has ever held there is no other workspace the
    message could belong to, so refusing it would lose a real customer message
    to protect nobody.
    """
    only = await _tenant(db_session, "solo")
    await _claim(
        db_session,
        only,
        started_at=NOW - timedelta(minutes=5),
        released_at=None,
        phone_number_id="PN-NEVER-MOVED",
    )
    wamid = f"wamid.{uuid.uuid4().hex}"
    payload = _inbound(wamid, at=NOW - timedelta(hours=2), body="sent just before the claim")
    entry = payload["entry"][0]  # type: ignore[index]
    entry["changes"][0]["value"]["metadata"]["phone_number_id"] = "PN-NEVER-MOVED"  # type: ignore[index]

    outcome = await _ingest(db_session, payload)

    assert outcome.stored == 1  # type: ignore[attr-defined]
    assert await _owner_of(db_session, wamid) == only.id


async def test_a_late_status_reconciles_the_message_its_old_owner_sent(
    db_session: AsyncSession,
    handover: tuple[Tenant, Tenant],
) -> None:
    """A status names a message, and that message carries its own workspace.

    Resolving the workspace from the number and *then* looking the message up
    inside it is what dropped these: after the handover the lookup ran in B,
    found nothing, logged, and A's delivery reporting stayed wrong for ever
    (MSG-04).
    """
    first, second = handover
    wamid = f"wamid.{uuid.uuid4().hex}"

    # A message A sent while it still held the number, staged as `_dispatch`
    # leaves it after Meta acknowledges.
    conversation_wamid = f"wamid.{uuid.uuid4().hex}"
    await _ingest(
        db_session, _inbound(conversation_wamid, at=DURING_A, body="customer wrote first")
    )
    inbound = (
        await db_session.execute(select(Message).where(Message.wa_message_id == conversation_wamid))
    ).scalar_one()
    outbound = Message(
        tenant_id=first.id,
        conversation_id=inbound.conversation_id,
        direction=MessageDirection.OUTBOUND,
        wa_message_id=wamid,
        status=MessageStatus.SENT,
        sent_at=DURING_A,
        origin=MessageOrigin.AGENT,
    )
    db_session.add(outbound)
    await db_session.flush()

    # The status turns up now, long after B took the number over.
    await _ingest(db_session, _status(wamid, status="delivered", at=DURING_B))
    await db_session.refresh(outbound)

    assert outbound.status is MessageStatus.DELIVERED
    assert outbound.delivered_at is not None
    assert outbound.tenant_id == first.id

    # And the raw event is filed with the workspace it concerns, rather than
    # with whoever happens to hold the number.
    event = (
        await db_session.execute(
            select(WhatsAppEvent).where(WhatsAppEvent.event_id == f"{wamid}:delivered")
        )
    ).scalar_one()
    assert event.tenant_id == first.id
    assert event.tenant_id != second.id
