# ruff: noqa: F811 - the workspace lifecycle fixtures are imported by name.
"""The owner's follow-up card over HTTP (FUS-01): read by any member, changed by an admin.

"إعدادات المتابعة": a toggle, the platforms, and up to five reminders of five
minutes to 23 hours. A workspace created through the API opens on - WhatsApp,
one reminder after five minutes - with no PUT ever made; an edit names the
version it read (If-Match) and is audited, old and new.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TenantRole
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.follow_up_settings import FollowUpSettings
from app.services.follow_up_settings_service import default_settings_row
from tests.integration.test_workspace_lifecycle import (  # noqa: F401 - fixtures
    API,
    _bearer,
    _join,
    _login,
    _select_workspace,
    _user,
    _workspace,
    app,
    http,
    lifecycle_settings,
)
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

URL = f"{API}/workspace/follow-up-settings"
CARD = {
    "enabled": True,
    "channel_types": ["whatsapp"],
    "steps": [{"delay_minutes": 5, "guidance": None}],
}


async def _owned(
    http: AsyncClient, db_session: AsyncSession, *, slug: str, defaults: bool = True
) -> tuple[Any, dict[str, str], dict[str, str], dict[str, str]]:
    owner = await _user(db_session, email=f"{slug}-owner@example.com")
    admin = await _user(db_session, email=f"{slug}-admin@example.com")
    member = await _user(db_session, email=f"{slug}-member@example.com")
    tenant = await _workspace(db_session, slug=slug, owner=owner)
    if defaults:
        db_session.add(default_settings_row(tenant.id))
        await db_session.flush()
    await _join(db_session, tenant=tenant, user=admin, role=TenantRole.TENANT_ADMIN)
    await _join(db_session, tenant=tenant, user=member, role=TenantRole.MEMBER)
    as_owner = await _select_workspace(http, await _login(http, owner.email), slug)
    as_admin = await _select_workspace(http, await _login(http, admin.email), slug)
    as_member = await _select_workspace(http, await _login(http, member.email), slug)
    return tenant, as_owner, as_admin, as_member


async def _put(
    http: AsyncClient, headers: dict[str, str], body: dict[str, Any], version: int | str
) -> Any:
    return await http.put(URL, json=body, headers={**headers, "If-Match": f'"{version}"'})


async def test_a_new_workspace_opens_the_card_on_with_one_five_minute_reminder(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """Created through the API, never PUT: "مفعّل · خطوة واحدة", "بعد 5 دقائق"."""
    user = await _user(db_session, email="card-new@example.com")
    session = await _login(http, user.email)
    created = await http.post(
        f"{API}/workspaces",
        json={"timezone": TEST_TIMEZONE, "name": "Card New", "slug": "card-new"},
        headers=_bearer(session),
    )
    assert created.status_code == 201, created.text
    headers = await _select_workspace(http, await _login(http, user.email), "card-new")

    shown = await http.get(URL, headers=headers)

    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert body["enabled"] is True
    assert body["channel_types"] == ["whatsapp"]
    assert body["available_channel_types"] == ["whatsapp"]
    assert body["steps"] == [{"delay_minutes": 5, "guidance": None}]
    assert body["summary"] == {"enabled": True, "steps_count": 1}
    assert body["version"] == 1
    assert shown.headers["ETag"] == '"1"'


async def test_a_member_reads_an_admin_changes_with_a_trail(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, as_admin, as_member = await _owned(http, db_session, slug="card-roles")

    assert (await http.get(URL, headers=as_member)).status_code == 200
    wanted = {
        "enabled": True,
        "channel_types": ["whatsapp"],
        "steps": [
            {"delay_minutes": 5, "guidance": None},
            {"delay_minutes": 1380, "guidance": "Ask if they need help with the order"},
        ],
    }
    assert (await _put(http, as_member, wanted, 1)).status_code == 403

    changed = await _put(http, as_admin, wanted, 1)

    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["version"] == 2
    assert body["summary"] == {"enabled": True, "steps_count": 2}
    assert body["updated_by"] is not None
    trail = list(
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.FOLLOW_UP_SETTINGS_UPDATED,
                    AuditLog.tenant_id == tenant.id,
                )
            )
        ).scalars()
    )
    assert len(trail) == 1
    assert trail[0].meta["previous"] == CARD
    assert trail[0].meta["current"] == wanted


async def test_the_toggle_off_keeps_the_reminders(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    _, as_owner, _, _ = await _owned(http, db_session, slug="card-toggle")
    off = await _put(http, as_owner, {**CARD, "enabled": False}, 1)
    assert off.status_code == 200, off.text
    assert off.json()["summary"] == {"enabled": False, "steps_count": 1}
    on = await _put(http, as_owner, CARD, 2)
    assert on.status_code == 200 and on.json()["enabled"] is True


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"steps": [{"delay_minutes": 4}]}, "follow_up_delay_out_of_range"),
        ({"steps": [{"delay_minutes": 1381}]}, "follow_up_delay_out_of_range"),
        ({"steps": [{"delay_minutes": 0}]}, "follow_up_delay_out_of_range"),
        ({"steps": [{"delay_minutes": 5}] * 6}, "follow_up_too_many_steps"),
        ({"steps": []}, "follow_up_steps_required"),
        ({"channel_types": []}, "follow_up_channels_required"),
        ({"channel_types": ["messenger"]}, "channel_type_unavailable"),
        ({"channel_types": ["whatsapp", "instagram"]}, "channel_type_unavailable"),
        ({"steps": [{"delay_minutes": 5, "guidance": "x" * 501}]}, "follow_up_guidance_too_long"),
    ],
)
async def test_the_card_is_validated(
    http: AsyncClient, db_session: AsyncSession, change: dict[str, Any], code: str
) -> None:
    _, as_owner, _, _ = await _owned(http, db_session, slug="card-valid")
    refused = await _put(http, as_owner, {**CARD, **change}, 1)
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == code
    assert (await http.get(URL, headers=as_owner)).json()["version"] == 1


async def test_the_bounds_are_inclusive_and_hours_are_minutes(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """5 minutes and 23 hours (1380) are both accepted."""
    _, as_owner, _, _ = await _owned(http, db_session, slug="card-bounds")
    steps = [{"delay_minutes": 5}, {"delay_minutes": 23 * 60}]
    answer = await _put(http, as_owner, {**CARD, "steps": steps}, 1)
    assert answer.status_code == 200, answer.text
    assert [step["delay_minutes"] for step in answer.json()["steps"]] == [5, 1380]


async def test_a_stale_version_is_a_conflict_and_a_missing_one_is_refused(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    _, as_owner, as_admin, _ = await _owned(http, db_session, slug="card-stale")
    assert (await _put(http, as_owner, CARD, 1)).status_code == 200
    stale = await _put(http, as_admin, CARD, 1)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "follow_up_settings_changed"
    missing = await http.put(URL, json=CARD, headers=as_owner)
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "follow_up_settings_version_required"


async def test_guidance_is_one_line_without_control_characters(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    _, as_owner, _, _ = await _owned(http, db_session, slug="card-guidance")
    steps = [{"delay_minutes": 30, "guidance": "  Ask\tif they\nneed\x07 help  "}]
    answer = await _put(http, as_owner, {**CARD, "steps": steps}, 1)
    assert answer.status_code == 200, answer.text
    assert answer.json()["steps"][0]["guidance"] == "Ask if they need help"


async def test_another_workspace_s_settings_are_never_reached(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    mine, as_mine, _, _ = await _owned(http, db_session, slug="card-mine")
    theirs, as_theirs, _, _ = await _owned(http, db_session, slug="card-theirs")
    assert (await _put(http, as_mine, {**CARD, "enabled": False}, 1)).status_code == 200

    assert (await http.get(URL, headers=as_theirs)).json()["enabled"] is True
    rows = {
        row.tenant_id: row
        for row in (
            await db_session.execute(
                select(FollowUpSettings).where(FollowUpSettings.tenant_id.in_([mine.id, theirs.id]))
            )
        ).scalars()
    }
    assert rows[mine.id].enabled is False and rows[theirs.id].enabled is True


async def test_a_workspace_without_a_row_reads_as_off(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """Not configured - a row never written - runs nothing; the first PUT names version 0."""
    _, as_owner, _, _ = await _owned(http, db_session, slug="card-none", defaults=False)
    body = (await http.get(URL, headers=as_owner)).json()
    assert body["enabled"] is False and body["version"] == 0
    assert (await _put(http, as_owner, CARD, 0)).json()["version"] == 1
