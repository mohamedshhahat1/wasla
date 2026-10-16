# ruff: noqa: F811 - the workspace lifecycle fixtures are imported by name.
"""A workspace's quiet hours over HTTP (TZ-03): any member reads them, an admin changes them."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TenantRole
from app.db.models.audit import AuditAction, AuditLog
from tests.integration.test_workspace_lifecycle import (  # noqa: F401 - fixtures
    API,
    _join,
    _login,
    _select_workspace,
    _user,
    _workspace,
    app,
    http,
    lifecycle_settings,
)

pytestmark = pytest.mark.integration

URL = f"{API}/workspace/campaign-settings"


async def test_quiet_hours_are_on_from_the_start_and_an_admin_changes_them_with_a_trail(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session, email="qh-owner@example.com")
    admin = await _user(db_session, email="qh-admin@example.com")
    member = await _user(db_session, email="qh-member@example.com")
    tenant = await _workspace(db_session, slug="qh-settings", owner=owner)
    await _join(db_session, tenant=tenant, user=admin, role=TenantRole.TENANT_ADMIN)
    await _join(db_session, tenant=tenant, user=member, role=TenantRole.MEMBER)
    as_member = await _select_workspace(http, await _login(http, member.email), "qh-settings")
    as_admin = await _select_workspace(http, await _login(http, admin.email), "qh-settings")

    shown = await http.get(URL, headers=as_member)
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert body["quiet_hours_enabled"] is True
    assert (body["quiet_hours_start"], body["quiet_hours_end"]) == ("22:00", "09:00")
    assert body["timezone"] == "Africa/Cairo"
    assert body["utc_offset"] in ("+02:00", "+03:00")

    wanted = {"quiet_hours_enabled": True, "quiet_hours_start": "21:30", "quiet_hours_end": "08:00"}
    assert (await http.put(URL, json=wanted, headers=as_member)).status_code == 403

    empty = await http.put(URL, json={**wanted, "quiet_hours_end": "21:30"}, headers=as_admin)
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "quiet_hours_empty_window"
    seconds = await http.put(
        URL, json={**wanted, "quiet_hours_start": "21:30:15"}, headers=as_admin
    )
    assert seconds.status_code == 422
    partial = await http.put(URL, json={"quiet_hours_enabled": False}, headers=as_admin)
    assert partial.status_code == 422

    changed = await http.put(URL, json=wanted, headers=as_admin)
    assert changed.status_code == 200, changed.text
    assert (changed.json()["quiet_hours_start"], changed.json()["quiet_hours_end"]) == (
        "21:30",
        "08:00",
    )
    again = await http.put(URL, json=wanted, headers=as_admin)
    assert again.status_code == 200

    trail = list(
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.CAMPAIGN_SETTINGS_UPDATED,
                    AuditLog.tenant_id == tenant.id,
                )
            )
        ).scalars()
    )
    assert len(trail) == 1
    assert trail[0].meta == {
        "previous": {
            "quiet_hours_enabled": True,
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "09:00",
        },
        "current": {
            "quiet_hours_enabled": True,
            "quiet_hours_start": "21:30",
            "quiet_hours_end": "08:00",
        },
    }
