"""The usage and analytics endpoints, and the line drawn between them.

The two are open to different people on purpose, and the real role guard runs
here rather than a mock of it: usage is what the workspace is spending, which is
the owner's business, while analytics is how the inbox is doing, which is the
business of everyone staffing it.

The services are stubbed. What each figure means is proved against real rows in
`test_tenant_metrics.py`; what is asserted here is the wiring - the window a
request asked for reaching the service, the shape coming back, and who is let
in.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_analytics_service,
    get_inbox_service,
    get_usage_service,
)
from app.core.exceptions import TenantIsolationError, ValidationError
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.analytics import AnalyticsEvent, AnalyticsEventType, AnalyticsSource
from app.db.models.conversation import Conversation
from app.db.models.usage import UsageEventType, UsageUnit
from app.repositories.analytics_repository import EventCount
from app.repositories.metrics_repository import (
    CampaignMetrics,
    ConversationMetrics,
    LeadMetrics,
    MessageMetrics,
    SentimentMetrics,
)
from app.repositories.usage_repository import UsagePoint, UsageTotal
from app.services.analytics_service import TenantAnalytics
from app.services.usage_service import UsageSummary, UsageWindow

pytestmark = pytest.mark.integration

USAGE_PATH = "/api/v1/usage"
ANALYTICS_PATH = "/api/v1/analytics"
TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CONVERSATION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
UNTIL = datetime(2026, 9, 1, tzinfo=UTC)
SINCE = UNTIL - timedelta(days=30)
WINDOW = UsageWindow(since=SINCE, until=UNTIL)


class StubUsage:
    """Remembers the window it was asked for."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.invalid = False

    async def summary(
        self, *, since: datetime | None = None, until: datetime | None = None
    ) -> UsageSummary:
        if self.invalid:
            raise ValidationError("The start of the window must be before its end.")
        self.asked.append({"since": since, "until": until})
        return UsageSummary(
            window=WINDOW,
            messages_received=12,
            messages_sent=30,
            ai_requests=7,
            input_tokens=900,
            output_tokens=100,
            totals=(
                UsageTotal(
                    event_type=UsageEventType.AI_INPUT_TOKEN,
                    unit=UsageUnit.TOKEN,
                    quantity=900,
                    events=7,
                ),
            ),
        )

    async def series(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        event_types: Iterable[UsageEventType] | None = None,
    ) -> tuple[Any, ...]:
        self.asked.append({"since": since, "until": until, "event_types": event_types})
        return WINDOW, [
            UsagePoint(
                day=datetime(2026, 8, 15, tzinfo=UTC),
                event_type=UsageEventType.WHATSAPP_MESSAGE_SENT,
                quantity=4,
            )
        ]


class StubAnalytics:
    """Returns a fixed report and a fixed history."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []

    async def report(
        self, *, since: datetime | None = None, until: datetime | None = None
    ) -> TenantAnalytics:
        self.asked.append({"since": since, "until": until})
        return TenantAnalytics(
            window=WINDOW,
            conversations=ConversationMetrics(created=10, handed_off=2, escalated=1),
            messages=MessageMetrics(
                received=40,
                sent=38,
                failed=1,
                average_response_seconds=95.5,
                unanswered=2,
            ),
            leads=LeadMetrics(created=8, qualified=3, won=2, lost=1),
            sentiment=SentimentMetrics(readings=25, unhappy_conversations=3),
            campaigns=CampaignMetrics(sent=100, delivered=88, failed=2, skipped=10),
            handoffs=(
                EventCount(
                    event_type=AnalyticsEventType.HANDOFF,
                    source=AnalyticsSource.AGENT,
                    count=2,
                ),
            ),
        )

    async def conversation_history(
        self, conversation_id: uuid.UUID, *, limit: int = 50
    ) -> list[Any]:
        return [
            AnalyticsEvent(
                id=uuid.uuid4(),
                tenant_id=TENANT_ID,
                event_type=AnalyticsEventType.HANDOFF,
                source=AnalyticsSource.SENTIMENT,
                conversation_id=conversation_id,
                actor_id=None,
                occurred_at=UNTIL,
                meta={"reason": "The customer sounds angry."},
            )
        ]


class StubInbox:
    """Only the resolution the analytics route performs before reading rows."""

    def __init__(self) -> None:
        self.missing = False
        self.resolved: list[uuid.UUID] = []

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation:
        if self.missing:
            raise TenantIsolationError()
        self.resolved.append(conversation_id)
        return Conversation(id=conversation_id, tenant_id=uuid.uuid4())


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


def _as(app: FastAPI, role: TenantRole) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: _workspace(role)


@pytest.fixture
def usage(app: FastAPI) -> StubUsage:
    stub = StubUsage()
    app.dependency_overrides[get_usage_service] = lambda: stub
    return stub


@pytest.fixture
def analytics(app: FastAPI) -> StubAnalytics:
    stub = StubAnalytics()
    app.dependency_overrides[get_analytics_service] = lambda: stub
    return stub


@pytest.fixture
def inbox(app: FastAPI) -> StubInbox:
    stub = StubInbox()
    app.dependency_overrides[get_inbox_service] = lambda: stub
    return stub


# ----------------------------------------------------------------------- usage


async def test_an_administrator_can_read_usage(
    client: AsyncClient, app: FastAPI, usage: StubUsage
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.get(USAGE_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["counters"]["messages_sent"] == 30
    # Derived rather than stored: two sums that disagree with their own total
    # is a support ticket nobody can answer.
    assert body["counters"]["total_tokens"] == 1000
    assert body["totals"][0]["unit"] == "token"


async def test_the_response_says_which_window_it_covers(
    client: AsyncClient, app: FastAPI, usage: StubUsage
) -> None:
    """A figure without its period is not quotable."""
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.get(USAGE_PATH)

    window = response.json()["window"]
    assert window["since"].startswith("2026-08-02")
    assert window["until"].startswith("2026-09-01")


async def test_a_member_cannot_read_usage(
    client: AsyncClient, app: FastAPI, usage: StubUsage
) -> None:
    """Usage is what the workspace is spending, which is the owner's business."""
    _as(app, TenantRole.MEMBER)

    response = await client.get(USAGE_PATH)

    assert response.status_code == 403
    assert usage.asked == []


async def test_a_window_is_passed_through_to_the_service(
    client: AsyncClient, app: FastAPI, usage: StubUsage
) -> None:
    _as(app, TenantRole.TENANT_OWNER)

    response = await client.get(
        USAGE_PATH,
        params={"since": "2026-08-01T00:00:00Z", "until": "2026-09-01T00:00:00Z"},
    )

    assert response.status_code == 200
    assert usage.asked[0]["since"] == SINCE.replace(day=1)
    assert usage.asked[0]["until"] == UNTIL


async def test_a_backwards_window_is_refused_with_the_usual_envelope(
    client: AsyncClient, app: FastAPI, usage: StubUsage
) -> None:
    _as(app, TenantRole.TENANT_OWNER)
    usage.invalid = True

    response = await client.get(USAGE_PATH)

    assert response.status_code == 422
    # The project's one error envelope, not FastAPI's `detail`.
    assert response.json()["error"]["code"] == "validation_error"


async def test_the_daily_series_can_be_narrowed_to_one_meter(
    client: AsyncClient, app: FastAPI, usage: StubUsage
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.get(
        f"{USAGE_PATH}/daily",
        params={"event_type": "whatsapp_message_sent"},
    )

    assert response.status_code == 200
    assert usage.asked[0]["event_types"] == [UsageEventType.WHATSAPP_MESSAGE_SENT]
    assert response.json()["points"][0]["quantity"] == 4


async def test_a_meter_that_does_not_exist_is_rejected(
    client: AsyncClient, app: FastAPI, usage: StubUsage
) -> None:
    _as(app, TenantRole.TENANT_ADMIN)

    response = await client.get(f"{USAGE_PATH}/daily", params={"event_type": "free_lunches"})

    assert response.status_code == 422


# ------------------------------------------------------------------- analytics


async def test_any_member_can_read_the_workspace_numbers(
    client: AsyncClient, app: FastAPI, analytics: StubAnalytics
) -> None:
    """These are how the inbox is doing, and the people staffing it are the
    people who need them."""
    _as(app, TenantRole.MEMBER)

    response = await client.get(ANALYTICS_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["conversations"]["created"] == 10
    assert body["messages"]["average_response_seconds"] == 95.5
    assert body["campaigns"]["delivered"] == 88


async def test_a_rate_arrives_beside_the_counts_it_came_from(
    client: AsyncClient, app: FastAPI, analytics: StubAnalytics
) -> None:
    """A rate on its own cannot be checked and hides the difference between
    nine of ten and nine hundred of a thousand."""
    _as(app, TenantRole.MEMBER)

    body = (await client.get(ANALYTICS_PATH)).json()

    assert body["conversations"]["ai_resolution_rate"] == 0.8
    assert body["conversations"]["ai_resolved"] == 8
    assert body["conversations"]["created"] == 10
    assert body["leads"]["conversion_rate"] == 0.25
    assert body["leads"]["won"] == 2


async def test_every_status_and_label_is_named_even_at_zero(
    client: AsyncClient, app: FastAPI, analytics: StubAnalytics
) -> None:
    """So a dashboard renders an empty column without knowing the vocabulary."""
    _as(app, TenantRole.MEMBER)

    body = (await client.get(ANALYTICS_PATH)).json()

    assert body["leads"]["by_status"]["proposal"] == 0
    assert body["sentiment"]["by_label"]["angry"] == 0


async def test_handoffs_come_back_split_by_who_decided(
    client: AsyncClient, app: FastAPI, analytics: StubAnalytics
) -> None:
    _as(app, TenantRole.MEMBER)

    body = (await client.get(ANALYTICS_PATH)).json()

    assert body["handoffs_by_source"] == [{"source": "agent", "count": 2}]


async def test_a_conversations_history_explains_itself(
    client: AsyncClient, app: FastAPI, analytics: StubAnalytics, inbox: StubInbox
) -> None:
    _as(app, TenantRole.MEMBER)

    response = await client.get(f"{ANALYTICS_PATH}/conversations/{CONVERSATION_ID}/events")

    assert response.status_code == 200
    event = response.json()[0]
    assert event["source"] == "sentiment"
    assert event["reason"] == "The customer sounds angry."
    assert inbox.resolved == [CONVERSATION_ID]


async def test_another_workspaces_conversation_is_not_found(
    client: AsyncClient, app: FastAPI, analytics: StubAnalytics, inbox: StubInbox
) -> None:
    """Resolved through the inbox first: an empty list would confirm the id
    exists somewhere else."""
    _as(app, TenantRole.MEMBER)
    inbox.missing = True

    response = await client.get(f"{ANALYTICS_PATH}/conversations/{CONVERSATION_ID}/events")

    assert response.status_code == 404


async def test_analytics_require_a_workspace(
    client: AsyncClient, app: FastAPI, analytics: StubAnalytics
) -> None:
    """No override: the real dependency runs and finds no credentials."""
    response = await client.get(ANALYTICS_PATH)
    assert response.status_code == 401
