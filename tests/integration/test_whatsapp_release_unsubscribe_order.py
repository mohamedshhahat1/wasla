"""The release route unsubscribes at Meta only after the release has committed (RL-02).

M-R26 found the order untested: the service tests prove a failed unsubscribe
never undoes a release, but not that Meta is told only once the release is
durable - so a commit that fails afterwards can never leave Meta unsubscribed
from a number Wasla still holds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_whatsapp_account_service,
    get_whatsapp_onboarding_service,
)
from app.core.dependencies import get_session
from app.db.models import (
    Membership,
    MembershipStatus,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
    WhatsAppAccount,
    WhatsAppAccountStatus,
)
from app.services.whatsapp_onboarding_service import ReleaseFollowUp
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

TENANT_ID = uuid.uuid4()
ACCOUNT_ID = uuid.uuid4()


def _workspace() -> ActiveWorkspace:
    user_id = uuid.uuid4()
    return ActiveWorkspace(
        user=User(id=user_id, email="owner@example.com", is_active=True),
        membership=Membership(
            id=uuid.uuid4(),
            user_id=user_id,
            tenant_id=TENANT_ID,
            role=TenantRole.TENANT_OWNER,
            status=MembershipStatus.ACTIVE,
        ),
        tenant=Tenant(
            timezone=TEST_TIMEZONE,
            id=TENANT_ID,
            name="Acme",
            slug="acme",
            status=TenantStatus.ACTIVE,
        ),
    )


class Journal:
    def __init__(self) -> None:
        self.steps: list[str] = []


class RecordingSession:
    """Stands in for the request's session: only the order of commits matters here."""

    def __init__(self, journal: Journal) -> None:
        self._journal = journal

    async def commit(self) -> None:
        self._journal.steps.append("commit")

    async def rollback(self) -> None:  # pragma: no cover - nothing fails here
        self._journal.steps.append("rollback")

    async def close(self) -> None:
        return None


class ReleasingAccounts:
    def __init__(self, journal: Journal, *, owes: bool) -> None:
        self._journal = journal
        self.release_follow_up: tuple[str, str] | None = None
        self._owes = owes

    async def release(self, **_: Any) -> WhatsAppAccount:
        self._journal.steps.append("release")
        if self._owes:
            self.release_follow_up = ("es-business-token", "555000111")
        return WhatsAppAccount(
            id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            phone_number_id="109876543210",
            waba_id="555000111",
            display_phone_number="+20 100 000 0000",
            status=WhatsAppAccountStatus.RELEASED,
            released_at=datetime(2026, 11, 21, tzinfo=UTC),
            created_at=datetime(2026, 11, 1, tzinfo=UTC),
            updated_at=datetime(2026, 11, 21, tzinfo=UTC),
        )


class Unsubscribing:
    def __init__(self, journal: Journal) -> None:
        self._journal = journal
        self.follow_ups: list[ReleaseFollowUp] = []

    async def unsubscribe(self, follow_up: ReleaseFollowUp) -> bool:
        self._journal.steps.append("unsubscribe")
        self.follow_ups.append(follow_up)
        return False


def _wire(app: FastAPI, *, owes: bool) -> tuple[Journal, Unsubscribing]:
    journal = Journal()
    onboarding = Unsubscribing(journal)
    app.dependency_overrides[get_active_workspace] = _workspace
    app.dependency_overrides[get_session] = lambda: RecordingSession(journal)
    app.dependency_overrides[get_whatsapp_account_service] = lambda: ReleasingAccounts(
        journal, owes=owes
    )
    app.dependency_overrides[get_whatsapp_onboarding_service] = lambda: onboarding
    return journal, onboarding


async def test_rl02_meta_is_told_only_after_the_release_commits(
    app: FastAPI, client: AsyncClient
) -> None:
    journal, onboarding = _wire(app, owes=True)
    response = await client.post(f"/api/v1/whatsapp/accounts/{ACCOUNT_ID}/release")
    assert response.status_code == 200
    assert response.json()["status"] == "released"
    assert journal.steps[:3] == ["release", "commit", "unsubscribe"]
    assert [follow.messaging_account_id for follow in onboarding.follow_ups] == ["555000111"]
    assert "es-business-token" not in response.text


async def test_rl07_an_assisted_release_calls_meta_not_at_all(
    app: FastAPI, client: AsyncClient
) -> None:
    journal, onboarding = _wire(app, owes=False)
    response = await client.post(f"/api/v1/whatsapp/accounts/{ACCOUNT_ID}/release")
    assert response.status_code == 200
    assert "unsubscribe" not in journal.steps
    assert onboarding.follow_ups == []
