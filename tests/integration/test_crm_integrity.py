"""The CRM's relational integrity, input safety and ownership trail, over HTTP.

Each test is one finding of the CRM audit (crm-c4h7) driven through the real
application against real rows: the route, the schema, the service, the
repository and the database constraint all take part, so a fix that lives in
any one layer is proved where a caller meets it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models import Membership, Tenant, TenantRole, User
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.conversation import Contact, Conversation, ConversationMode
from app.db.models.enums import MembershipStatus, TenantStatus
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind, Lead, LeadActivity, LeadSource, LeadStatus
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.main import create_app
from app.services.lead_service import LeadService
from tests.conftest import AllowingEntitlements, FakeDependency

pytestmark = pytest.mark.integration

API = "/api/v1"
NUL = "bad\x00value"


@dataclass(frozen=True, slots=True)
class Desk:
    """A workspace seen by one of its members."""

    tenant: Tenant
    owner: User
    member: User
    colleague: User
    conversation: Conversation
    other_conversation: Conversation
    lead: Lead
    other_lead: Lead
    headers: dict[str, str]
    member_headers: dict[str, str]


async def _desk(session: AsyncSession, settings: Settings, *, slug: str) -> Desk:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    users = [
        User(
            email=f"{name}@{slug}.example",
            hashed_password="x",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        for name in ("owner", "member", "colleague")
    ]
    session.add(tenant)
    session.add_all(users)
    await session.flush()
    owner, member, colleague = users
    session.add_all(
        [
            Membership(tenant_id=tenant.id, user_id=owner.id, role=TenantRole.TENANT_OWNER),
            Membership(tenant_id=tenant.id, user_id=member.id, role=TenantRole.MEMBER),
            Membership(tenant_id=tenant.id, user_id=colleague.id, role=TenantRole.MEMBER),
        ]
    )
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"pn-{slug}",
        waba_id=f"waba-{slug}",
        display_phone_number="+201000000000",
        status=WhatsAppAccountStatus.ACTIVE,
    )
    x = Contact(tenant_id=tenant.id, wa_id=f"x-{slug}")
    y = Contact(tenant_id=tenant.id, wa_id=f"y-{slug}")
    session.add_all([account, x, y])
    await session.flush()
    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=x.id,
        account_id=account.id,
        last_inbound_at=datetime.now(UTC),
    )
    other = Conversation(
        tenant_id=tenant.id,
        contact_id=y.id,
        account_id=account.id,
        last_inbound_at=datetime.now(UTC),
    )
    session.add_all([conversation, other])
    await session.flush()
    lead = Lead(tenant_id=tenant.id, contact_id=x.id, conversation_id=conversation.id)
    other_lead = Lead(tenant_id=tenant.id, contact_id=y.id, conversation_id=other.id)
    session.add_all([lead, other_lead])
    await session.flush()

    def headers(user: User) -> dict[str, str]:
        token, _ = create_access_token(
            settings=settings,
            subject=user.id,
            tenant_id=tenant.id,
            token_version=user.token_version,
        )
        return {"Authorization": f"Bearer {token}"}

    return Desk(
        tenant=tenant,
        owner=owner,
        member=member,
        colleague=colleague,
        conversation=conversation,
        other_conversation=other,
        lead=lead,
        other_lead=other_lead,
        headers=headers(owner),
        member_headers=headers(member),
    )


@pytest.fixture
def crm_app(
    settings: Settings,
    db_session: AsyncSession,
    fake_redis: FakeDependency,
) -> Iterator[FastAPI]:
    application = create_app(settings)
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = fake_redis

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(crm_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=crm_app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def desk(db_session: AsyncSession, settings: Settings) -> Desk:
    return await _desk(db_session, settings, slug=f"desk-{uuid.uuid4().hex[:8]}")


@pytest_asyncio.fixture
async def rival(db_session: AsyncSession, settings: Settings) -> Desk:
    return await _desk(db_session, settings, slug=f"rival-{uuid.uuid4().hex[:8]}")


def _follow_up(conversation_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {"conversation_id": str(conversation_id), "delay_minutes": 30, "body": "hi", **extra}


async def _count(session: AsyncSession, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


# ---------------------------------------------------------------- CRM-01


async def test_another_workspaces_lead_and_a_nonexistent_one_are_the_same_404(
    http: AsyncClient, desk: Desk, rival: Desk, db_session: AsyncSession
) -> None:
    """CRM-01: the foreign id used to answer 201, and a random one 409."""
    foreign = await http.post(
        f"{API}/follow-ups",
        json=_follow_up(desk.conversation.id, lead_id=str(rival.lead.id)),
        headers=desk.headers,
    )
    missing = await http.post(
        f"{API}/follow-ups",
        json=_follow_up(desk.conversation.id, lead_id=str(uuid.uuid4())),
        headers=desk.headers,
    )

    assert foreign.status_code == missing.status_code == 404
    for key in ("code", "message"):
        assert foreign.json()["error"][key] == missing.json()["error"][key]
    assert str(rival.lead.id) not in foreign.text
    assert await _count(db_session, FollowUp, FollowUp.tenant_id == desk.tenant.id) == 0


async def test_a_lead_of_a_different_customer_is_refused(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    """CRM-14: same workspace, conversation with Y, lead of X."""
    response = await http.post(
        f"{API}/follow-ups",
        json=_follow_up(desk.other_conversation.id, lead_id=str(desk.lead.id)),
        headers=desk.headers,
    )
    assert response.status_code == 422
    assert await _count(db_session, FollowUp, FollowUp.tenant_id == desk.tenant.id) == 0


async def test_the_customers_own_lead_is_accepted(http: AsyncClient, desk: Desk) -> None:
    response = await http.post(
        f"{API}/follow-ups",
        json=_follow_up(desk.conversation.id, lead_id=str(desk.lead.id)),
        headers=desk.headers,
    )
    assert response.status_code == 201
    assert response.json()["lead_id"] == str(desk.lead.id)


async def test_the_database_refuses_a_cross_tenant_follow_up_lead(
    db_session: AsyncSession, desk: Desk, rival: Desk
) -> None:
    """CRM-01's constraint: no writer, current or future, can build the row."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(FollowUp).values(
                    id=uuid.uuid4(),
                    tenant_id=desk.tenant.id,
                    conversation_id=desk.conversation.id,
                    lead_id=rival.lead.id,
                    scheduled_at=datetime.now(UTC) + timedelta(hours=1),
                    status=FollowUpStatus.PENDING,
                    body="x",
                    created_by_kind=ActorKind.USER,
                    attempts=0,
                )
            )


async def test_the_database_refuses_a_lead_whose_conversation_is_another_customers(
    db_session: AsyncSession, desk: Desk
) -> None:
    """CRM-14's constraint: the three-column key on `leads`."""
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Lead(
                    tenant_id=desk.tenant.id,
                    contact_id=desk.lead.contact_id,
                    conversation_id=desk.other_conversation.id,
                    status=LeadStatus.LOST,
                    source=LeadSource.MANUAL,
                )
            )
            await db_session.flush()


async def test_the_database_refuses_a_cross_tenant_lead_contact(
    db_session: AsyncSession, desk: Desk, rival: Desk
) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Lead(
                    tenant_id=desk.tenant.id,
                    contact_id=rival.lead.contact_id,
                    status=LeadStatus.NEW,
                    source=LeadSource.MANUAL,
                )
            )
            await db_session.flush()


async def test_a_manual_lead_naming_one_customer_and_anothers_conversation_is_refused(
    http: AsyncClient, desk: Desk
) -> None:
    """CRM-14 at the API: 422, not a stored contradiction."""
    await http.post(
        f"{API}/leads/{desk.lead.id}/status", json={"status": "lost"}, headers=desk.headers
    )
    response = await http.post(
        f"{API}/leads",
        json={
            "contact_id": str(desk.lead.contact_id),
            "conversation_id": str(desk.other_conversation.id),
            "name": "Mixed up",
        },
        headers=desk.headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- CRM-12


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/conversations/{conversation}/mode", {"mode": "human", "handoff_reason": NUL}),
        ("PATCH", "/leads/{lead}", {"name": NUL}),
        ("PATCH", "/leads/{lead}", {"interest": NUL}),
        ("PATCH", "/leads/{lead}", {"tags": [NUL]}),
        ("PATCH", "/leads/{lead}", {"custom_fields": {"k": NUL}}),
        ("PATCH", "/leads/{lead}", {"custom_fields": {NUL: "v"}}),
        ("POST", "/leads/{lead}/notes", {"body": NUL}),
        ("POST", "/leads/{lead}/status", {"status": "contacted", "reason": NUL}),
        (
            "POST",
            "/follow-ups",
            {"conversation_id": "{conversation}", "delay_minutes": 30, "body": NUL},
        ),
        (
            "POST",
            "/follow-ups",
            {"conversation_id": "{conversation}", "delay_minutes": 30, "body": "hi", "reason": NUL},
        ),
        ("POST", "/leads", {"name": NUL}),
    ],
)
async def test_nul_in_human_crm_text_is_a_422_not_a_500(
    http: AsyncClient,
    desk: Desk,
    method: str,
    path: str,
    body: dict[str, Any],
) -> None:
    ids = {"conversation": str(desk.conversation.id), "lead": str(desk.lead.id)}
    url = API + path.format(**ids)
    payload = {
        key: (value.format(**ids) if isinstance(value, str) and "{" in value else value)
        for key, value in body.items()
    }
    response = await http.request(method, url, json=payload, headers=desk.headers)
    assert response.status_code == 422, response.text


async def test_arabic_and_emoji_at_the_limit_are_kept(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    reason = ("عميل مهم 🙂" * 40)[:200]
    response = await http.post(
        f"{API}/conversations/{desk.conversation.id}/mode",
        json={"mode": "human", "handoff_reason": reason},
        headers=desk.member_headers,
    )
    assert response.status_code == 200
    assert response.json()["handoff_reason"] == reason
    note = await http.post(
        f"{API}/leads/{desk.lead.id}/notes",
        json={"body": "ملاحظة ✅"},
        headers=desk.headers,
    )
    assert note.status_code == 201


# ---------------------------------------------------------------- CRM-13


async def test_reopening_a_lost_lead_behind_a_newer_open_one_is_a_409(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    lead_id, contact_id = desk.lead.id, desk.lead.contact_id
    lost = await http.post(
        f"{API}/leads/{lead_id}/status", json={"status": "lost"}, headers=desk.headers
    )
    newer = await http.post(
        f"{API}/leads",
        json={"contact_id": str(contact_id), "name": "Second try"},
        headers=desk.headers,
    )
    reopen = await http.post(
        f"{API}/leads/{lead_id}/status", json={"status": "new"}, headers=desk.headers
    )

    assert (lost.status_code, newer.status_code, reopen.status_code) == (200, 201, 409)
    assert reopen.json()["error"]["code"] == "open_lead_exists"
    status = await db_session.scalar(select(Lead.status).where(Lead.id == lead_id))
    assert status is LeadStatus.LOST


async def test_the_reopen_collision_is_a_409_even_when_the_precheck_is_raced(
    db_session: AsyncSession, desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-check is a fast path; the unique index is the guarantee."""
    service = LeadService(session=db_session, tenant_id=desk.tenant.id)
    await service.change_status(lead_id=desk.lead.id, status=LeadStatus.LOST, actor_id=None)
    db_session.add(
        Lead(tenant_id=desk.tenant.id, contact_id=desk.lead.contact_id, status=LeadStatus.NEW)
    )
    await db_session.flush()

    async def nothing(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(service._leads, "get_active_for_contact", nothing)
    from app.core.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await service.change_status(lead_id=desk.lead.id, status=LeadStatus.NEW, actor_id=None)


# ---------------------------------------------------------------- CRM-15


@pytest.mark.parametrize("offset", ["Z", "+03:00", "+02:00"])
async def test_a_scheduled_time_with_an_offset_is_stored_as_that_instant(
    http: AsyncClient, desk: Desk, offset: str
) -> None:
    local = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
    hours = 0 if offset == "Z" else int(offset[1:3])
    wall = (local + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S") + offset
    response = await http.post(
        f"{API}/follow-ups",
        json={"conversation_id": str(desk.conversation.id), "scheduled_at": wall, "body": "hi"},
        headers=desk.headers,
    )
    assert response.status_code == 201
    stored = datetime.fromisoformat(response.json()["scheduled_at"])
    assert stored == local


async def test_a_naive_scheduled_time_is_refused(http: AsyncClient, desk: Desk) -> None:
    naive = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    response = await http.post(
        f"{API}/follow-ups",
        json={"conversation_id": str(desk.conversation.id), "scheduled_at": naive, "body": "hi"},
        headers=desk.headers,
    )
    assert response.status_code == 422


async def test_the_service_refuses_a_naive_time_too(db_session: AsyncSession, desk: Desk) -> None:
    from app.core.exceptions import ValidationError
    from app.services.follow_up_service import FollowUpService

    with pytest.raises(ValidationError):
        await FollowUpService(session=db_session, tenant_id=desk.tenant.id).schedule(
            conversation_id=desk.conversation.id,
            scheduled_at=datetime.now() + timedelta(days=1),
            body="hi",
        )


# ------------------------------------------------------ ownership (PD-CRM-3)


async def test_a_takeover_makes_the_colleague_the_owner_and_is_audited(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    response = await http.post(
        f"{API}/conversations/{desk.conversation.id}/mode",
        json={"mode": "human", "handoff_reason": "Pricing question"},
        headers=desk.member_headers,
    )
    assert response.status_code == 200
    assert response.json()["assigned_to_id"] == str(desk.member.id)

    [entry] = (
        await db_session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == desk.tenant.id,
                AuditLog.action == AuditAction.CONVERSATION_TAKEN_OVER,
            )
        )
    ).all()
    assert entry.actor_id == desk.member.id
    assert entry.meta is not None
    assert entry.meta["previous_mode"] == "ai" and entry.meta["new_mode"] == "human"
    assert entry.meta["new_assignee_id"] == str(desk.member.id)
    assert entry.meta["reason_supplied"] is True
    # The trail names the fact, never the sentence.
    assert "Pricing" not in str(entry.meta)


async def test_release_to_ai_clears_the_reason_and_is_audited(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    """TG-1 / M08: documented in CRM.md and, until now, pinned by nothing."""
    await http.post(
        f"{API}/conversations/{desk.conversation.id}/mode",
        json={"mode": "human", "handoff_reason": "Pricing question"},
        headers=desk.member_headers,
    )
    released = await http.post(
        f"{API}/conversations/{desk.conversation.id}/mode",
        json={"mode": "ai"},
        headers=desk.member_headers,
    )

    assert released.status_code == 200
    assert released.json()["mode"] == "ai"
    assert released.json()["handoff_reason"] is None
    await db_session.flush()
    reason = await db_session.scalar(
        select(Conversation.handoff_reason).where(Conversation.id == desk.conversation.id)
    )
    assert reason is None
    assert (
        await _count(
            db_session,
            AuditLog,
            AuditLog.tenant_id == desk.tenant.id,
            AuditLog.action == AuditAction.CONVERSATION_RELEASED_TO_AI,
        )
        == 1
    )


async def test_a_no_op_writes_no_audit_row(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    """R23: releasing an AI conversation, closing twice, assigning the owner it has."""
    await http.post(
        f"{API}/conversations/{desk.conversation.id}/mode",
        json={"mode": "ai"},
        headers=desk.headers,
    )
    for _ in range(2):
        await http.post(f"{API}/conversations/{desk.conversation.id}/close", headers=desk.headers)
    assigned = await http.post(
        f"{API}/conversations/{desk.conversation.id}/assignment",
        json={"assigned_to_id": None, "expected_assigned_to_id": None},
        headers=desk.headers,
    )
    assert assigned.status_code == 200

    actions = (
        await db_session.scalars(
            select(AuditLog.action).where(AuditLog.tenant_id == desk.tenant.id)
        )
    ).all()
    assert actions == [AuditAction.CONVERSATION_CLOSED]


async def test_assignment_needs_the_expected_owner_and_a_stale_one_is_409(
    http: AsyncClient, desk: Desk
) -> None:
    path = f"{API}/conversations/{desk.conversation.id}/assignment"
    missing = await http.post(
        path, json={"assigned_to_id": str(desk.member.id)}, headers=desk.headers
    )
    first = await http.post(
        path,
        json={"assigned_to_id": str(desk.member.id), "expected_assigned_to_id": None},
        headers=desk.headers,
    )
    stale = await http.post(
        path,
        json={"assigned_to_id": str(desk.colleague.id), "expected_assigned_to_id": None},
        headers=desk.member_headers,
    )
    fresh = await http.post(
        path,
        json={
            "assigned_to_id": str(desk.colleague.id),
            "expected_assigned_to_id": str(desk.member.id),
        },
        headers=desk.member_headers,
    )

    assert missing.status_code == 422
    assert first.status_code == 200
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_assignment"
    assert fresh.status_code == 200 and fresh.json()["assigned_to_id"] == str(desk.colleague.id)


async def test_close_and_reopen_are_audited_once_each(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    await http.post(f"{API}/conversations/{desk.conversation.id}/close", headers=desk.headers)
    await http.post(f"{API}/conversations/{desk.conversation.id}/reopen", headers=desk.headers)
    actions = (
        await db_session.scalars(
            select(AuditLog.action)
            .where(AuditLog.tenant_id == desk.tenant.id)
            .order_by(AuditLog.occurred_at)
        )
    ).all()
    assert actions == [AuditAction.CONVERSATION_CLOSED, AuditAction.CONVERSATION_REOPENED]


# ------------------------------------------------- member removal (PD-CRM-2/8)


async def test_removing_a_member_releases_their_open_work_and_keeps_their_history(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    member = desk.member
    await http.post(
        f"{API}/conversations/{desk.conversation.id}/assignment",
        json={"assigned_to_id": str(member.id), "expected_assigned_to_id": None},
        headers=desk.headers,
    )
    await http.post(
        f"{API}/leads/{desk.lead.id}/assignment",
        json={"assigned_to_id": str(member.id), "expected_assigned_to_id": None},
        headers=desk.headers,
    )
    reminder = await http.post(
        f"{API}/follow-ups", json=_follow_up(desk.conversation.id), headers=desk.member_headers
    )
    sent = FollowUp(
        tenant_id=desk.tenant.id,
        conversation_id=desk.other_conversation.id,
        scheduled_at=datetime.now(UTC) - timedelta(days=1),
        status=FollowUpStatus.SENT,
        body="earlier",
        created_by_id=member.id,
        created_by_kind=ActorKind.USER,
        sent_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(sent)
    note = await http.post(
        f"{API}/leads/{desk.lead.id}/notes", json={"body": "called"}, headers=desk.member_headers
    )
    assert (reminder.status_code, note.status_code) == (201, 201)

    removed = await http.delete(f"{API}/workspace/members/{member.id}", headers=desk.headers)
    assert removed.status_code in (200, 204), removed.text
    await db_session.flush()

    await db_session.refresh(desk.conversation)
    await db_session.refresh(desk.lead)
    pending = await db_session.get(FollowUp, uuid.UUID(reminder.json()["id"]))
    await db_session.refresh(sent)
    assert pending is not None
    await db_session.refresh(pending)
    assert desk.conversation.assigned_to_id is None
    assert desk.lead.assigned_to_id is None
    assert (pending.status, pending.cancelled_reason) == (
        FollowUpStatus.CANCELLED,
        "member_revoked",
    )
    assert sent.status is FollowUpStatus.SENT
    membership = await db_session.scalar(
        select(Membership.status).where(
            Membership.tenant_id == desk.tenant.id, Membership.user_id == member.id
        )
    )
    assert membership is MembershipStatus.REVOKED
    # History stays attributed to them.
    assert await _count(db_session, LeadActivity, LeadActivity.actor_id == member.id) >= 1


# ---------------------------------------------------------------- TG-2


async def test_extraction_cannot_reach_a_field_outside_the_agent_allowlist(
    db_session: AsyncSession, desk: Desk
) -> None:
    """M17: the application-layer filter, proved on its own.

    `ExtractedLead` today carries only the six writable fields, which is why
    removing the filter survived every test. A payload that names more is how
    a schema change would reach it, and the filter must hold on its own.
    """

    class Overreaching:
        def as_fields(self) -> dict[str, Any]:
            return {"interest": "finishing", "source": "import", "score": 99, "status": "won"}

    before = (desk.lead.source, desk.lead.score, desk.lead.status)
    capture = await LeadService(
        session=db_session, tenant_id=desk.tenant.id
    ).capture_from_conversation(
        conversation_id=desk.conversation.id,
        extracted=Overreaching(),  # type: ignore[arg-type]
    )
    await db_session.flush()
    await db_session.refresh(desk.lead)

    assert capture.changed_fields == frozenset({"interest"})
    assert (desk.lead.source, desk.lead.score, desk.lead.status) == before
    assert desk.lead.interest == "finishing"


# ---------------------------------------------------------------- CRM-08


async def test_a_colleague_can_schedule_on_a_conversation_a_person_owns(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    desk.conversation.mode = ConversationMode.HUMAN
    await db_session.flush()
    response = await http.post(
        f"{API}/follow-ups", json=_follow_up(desk.conversation.id), headers=desk.member_headers
    )
    assert response.status_code == 201
    assert response.json()["created_by_kind"] == "user"
