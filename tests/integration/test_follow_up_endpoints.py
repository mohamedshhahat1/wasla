"""The follow-up endpoints.

The workspace dependency is overridden, but the real wiring runs underneath: a
request with no bearer token is refused by the actual token check, not a mock of
it.

These routes are open to any member on purpose. A follow-up is a message to a
customer someone is already handling, and requiring an administrator to stop one
would mean the person watching the conversation cannot cancel a nudge they can
see has become wrong.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_follow_up_service,
)
from app.core.exceptions import TenantIsolationError, ValidationError
from app.core.pagination import Page
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.lead import ActorKind

pytestmark = pytest.mark.integration

PATH = "/api/v1/follow-ups"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CONVERSATION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
FOLLOW_UP_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
MOMENT = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _follow_up(**overrides: Any) -> FollowUp:
    values = {
        "id": FOLLOW_UP_ID,
        "tenant_id": TENANT_ID,
        "conversation_id": CONVERSATION_ID,
        "lead_id": None,
        "scheduled_at": MOMENT + timedelta(minutes=30),
        "status": FollowUpStatus.PENDING,
        "body": "Still thinking it over?",
        "template_name": None,
        "template_language": None,
        "template_components": None,
        "reason": "The customer said they would think about it.",
        "created_by_id": USER_ID,
        "created_by_kind": ActorKind.USER,
        "attempts": 0,
        "last_error": None,
        "sent_at": None,
        "cancelled_at": None,
        "cancelled_reason": None,
        "message_id": None,
        "created_at": MOMENT,
        "updated_at": MOMENT,
    }
    values.update(overrides)
    return FollowUp(**values)


class StubFollowUps:
    def __init__(self) -> None:
        self.scheduled: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self.missing = False
        self.invalid = False
        self.follow_up = _follow_up()

    def _guard(self) -> None:
        if self.missing:
            raise TenantIsolationError()

    async def list_follow_ups(self, **kwargs: Any) -> Page[Any]:
        self.filters = kwargs
        return Page(items=[self.follow_up], next_cursor=None)

    async def get(self, follow_up_id: uuid.UUID) -> FollowUp:
        self._guard()
        return self.follow_up

    async def schedule(self, **kwargs: Any) -> FollowUp:
        if self.invalid:
            raise ValidationError("This conversation is closed.")
        self.scheduled.append(kwargs)
        return self.follow_up

    async def cancel(
        self,
        *,
        follow_up_id: uuid.UUID,
        reason: str | None = None,
    ) -> FollowUp:
        self._guard()
        self.cancelled.append({"follow_up_id": follow_up_id, "reason": reason})
        return _follow_up(
            status=FollowUpStatus.CANCELLED,
            cancelled_at=MOMENT,
            cancelled_reason=reason,
        )


def _workspace(role: TenantRole) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=USER_ID,
            tenant_id=TENANT_ID,
            role=role,
        ),
        tenant=Tenant(id=TENANT_ID, name="Acme", slug="acme", status=TenantStatus.ACTIVE),
    )


@pytest.fixture
def follow_ups(app: FastAPI) -> StubFollowUps:
    stub = StubFollowUps()
    app.dependency_overrides[get_follow_up_service] = lambda: stub
    return stub


def _as(app: FastAPI, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(role)


async def test_a_member_can_list_scheduled_follow_ups(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.get(PATH)

    assert response.status_code == 200
    assert response.json()["items"][0]["body"] == "Still thinking it over?"


async def test_a_member_can_schedule_a_follow_up(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.post(
        PATH,
        json={
            "conversation_id": str(CONVERSATION_ID),
            "delay_minutes": 30,
            "body": "Still thinking it over?",
        },
    )

    assert response.status_code == 201
    assert follow_ups.scheduled[0]["delay"] == timedelta(minutes=30)
    assert follow_ups.scheduled[0]["created_by_id"] == USER_ID


async def test_a_member_can_cancel_a_follow_up(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    """Whoever is watching the conversation must be able to stop the nudge."""
    _as(app, TenantRole.MEMBER)

    response = await client.post(
        f"{PATH}/{FOLLOW_UP_ID}/cancel",
        json={"reason": "The customer called instead."},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert follow_ups.cancelled[0]["reason"] == "The customer called instead."


async def test_an_absolute_time_is_accepted(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    _as(app, TenantRole.MEMBER)
    when = datetime.now(UTC) + timedelta(days=1)

    response = await client.post(
        PATH,
        json={
            "conversation_id": str(CONVERSATION_ID),
            "scheduled_at": when.isoformat(),
            "body": "Tomorrow then?",
        },
    )

    assert response.status_code == 201
    assert follow_ups.scheduled[0]["delay"] is None
    assert follow_ups.scheduled[0]["scheduled_at"] is not None


async def test_supplying_both_a_delay_and_a_time_is_refused(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    """Accepting both would leave the service picking a winner silently."""
    _as(app, TenantRole.MEMBER)

    response = await client.post(
        PATH,
        json={
            "conversation_id": str(CONVERSATION_ID),
            "delay_minutes": 30,
            "scheduled_at": datetime.now(UTC).isoformat(),
            "body": "Still there?",
        },
    )

    assert response.status_code == 422
    assert follow_ups.scheduled == []


async def test_a_follow_up_with_nothing_to_send_is_refused(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.post(
        PATH,
        json={"conversation_id": str(CONVERSATION_ID), "delay_minutes": 30},
    )

    assert response.status_code == 422
    assert follow_ups.scheduled == []


async def test_a_template_only_follow_up_is_accepted(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    """The only thing that works outside the 24-hour window."""
    _as(app, TenantRole.MEMBER)

    response = await client.post(
        PATH,
        json={
            "conversation_id": str(CONVERSATION_ID),
            "delay_minutes": 1440,
            "template_name": "gentle_nudge",
            "template_language": "ar",
        },
    )

    assert response.status_code == 201
    assert follow_ups.scheduled[0]["template_name"] == "gentle_nudge"


async def test_a_delay_beyond_the_maximum_is_refused(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.post(
        PATH,
        json={
            "conversation_id": str(CONVERSATION_ID),
            "delay_minutes": 60 * 24 * 400,
            "body": "Much later.",
        },
    )

    assert response.status_code == 422


async def test_another_workspaces_follow_up_answers_not_found(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    """Never 403: that would confirm it exists."""
    _as(app, TenantRole.MEMBER)
    follow_ups.missing = True

    response = await client.get(f"{PATH}/{FOLLOW_UP_ID}")

    assert response.status_code == 404


async def test_a_closed_conversation_answers_unprocessable(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    _as(app, TenantRole.MEMBER)
    follow_ups.invalid = True

    response = await client.post(
        PATH,
        json={
            "conversation_id": str(CONVERSATION_ID),
            "delay_minutes": 30,
            "body": "Still there?",
        },
    )

    assert response.status_code == 422


async def test_an_unknown_field_is_refused(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.post(
        PATH,
        json={
            "conversation_id": str(CONVERSATION_ID),
            "delay_minutes": 30,
            "body": "Still there?",
            "delay_minuets": 5,
        },
    )

    assert response.status_code == 422
    assert follow_ups.scheduled == []


async def test_a_status_filter_reaches_the_service(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.get(PATH, params={"status": ["pending", "sent"]})

    assert response.status_code == 200
    assert follow_ups.filters["statuses"] == (FollowUpStatus.PENDING, FollowUpStatus.SENT)


async def test_an_unknown_status_filter_is_refused(
    client: AsyncClient, app: FastAPI, follow_ups: StubFollowUps
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.get(PATH, params={"status": "snoozed"})

    assert response.status_code == 422


async def test_authentication_is_required(client: AsyncClient) -> None:
    """Deliberately takes no stub.

    Overriding the service would satisfy the route without ever resolving the
    workspace, so the token check would never run and this would pass against
    nothing.
    """
    response = await client.get(PATH)

    assert response.status_code == 401
