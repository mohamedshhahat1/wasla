"""A workspace's time zone, and the public list it is chosen from (TZ-01, TZ-02).

Driven over HTTP against real rows, with the lifecycle suite's application:
the list a signup form reads, the routes that create a workspace and require a
zone from it, and the workspace route that shows and changes it.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezones import CANONICAL_TIMEZONES, SUGGESTED_TIMEZONES, TZDATA_VERSION
from app.db.models import Tenant, TenantRole
from app.db.models.audit import AuditAction, AuditLog
from app.schemas.workspace import WorkspaceCreateRequest
from tests.integration.test_workspace_lifecycle import (  # noqa: F401 - fixtures
    API,
    PASSWORD,
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

ALIASES = ("US/Eastern", "Egypt", "Etc/GMT+3", "Asia/Calcutta", "UTC", "GMT", "Cairo")


# ---------------------------------------------------------------- the list


async def test_the_list_is_public_and_suggests_the_markets_first(http: AsyncClient) -> None:
    response = await http.get(f"{API}/timezones")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tzdata_version"] == TZDATA_VERSION
    assert [entry["id"] for entry in body["suggested"]] == list(SUGGESTED_TIMEZONES)
    listed = [entry["id"] for entry in body["timezones"]]
    assert set(listed) == set(CANONICAL_TIMEZONES)
    assert len(listed) == len(set(listed))
    keys = [(entry["offset_minutes"], entry["id"]) for entry in body["timezones"]]
    assert keys == sorted(keys)
    cairo = next(entry for entry in body["timezones"] if entry["id"] == "Africa/Cairo")
    assert cairo["region"] == "Africa"
    assert cairo["label"] == "Cairo"
    assert cairo["utc_offset"] in ("+02:00", "+03:00")
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["etag"]


async def test_an_unchanged_list_answers_304(http: AsyncClient) -> None:
    first = await http.get(f"{API}/timezones")
    again = await http.get(f"{API}/timezones", headers={"If-None-Match": first.headers["etag"]})

    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == first.headers["etag"]


async def test_a_filter_matches_id_and_label_and_is_its_own_document(http: AsyncClient) -> None:
    whole = await http.get(f"{API}/timezones")
    filtered = await http.get(f"{API}/timezones", params={"q": "CAIRO"})

    assert filtered.status_code == 200
    assert [entry["id"] for entry in filtered.json()["timezones"]] == ["Africa/Cairo"]
    assert filtered.headers["etag"] != whole.headers["etag"]
    by_label = await http.get(f"{API}/timezones", params={"q": "buenos aires"})
    assert "America/Argentina/Buenos_Aires" in [e["id"] for e in by_label.json()["timezones"]]
    too_long = await http.get(f"{API}/timezones", params={"q": "x" * 65})
    assert too_long.status_code == 422


def test_every_listed_zone_is_accepted_and_no_alias_is() -> None:
    """The list and the validator are one list (TZ-02)."""
    for name in CANONICAL_TIMEZONES:
        assert WorkspaceCreateRequest(name="W", slug="w-ok", timezone=name).timezone == name
    from app.core.timezones import UnknownTimezoneError, validated

    for name in CANONICAL_TIMEZONES:
        assert validated(name) == name
    for alias in ALIASES:
        with pytest.raises(UnknownTimezoneError):
            validated(alias)


# --------------------------------------------------------------- creation


async def test_creating_a_workspace_requires_a_zone_from_the_list(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, email="tz-create@example.com")
    session = await _login(http, user.email)

    missing = await http.post(
        f"{API}/workspaces", json={"name": "No Zone", "slug": "no-zone"}, headers=_bearer(session)
    )
    assert missing.status_code == 422
    unknown = await http.post(
        f"{API}/workspaces",
        json={"name": "Bad Zone", "slug": "bad-zone", "timezone": "Egypt"},
        headers=_bearer(session),
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "timezone_unknown"
    assert await db_session.scalar(select(Tenant).where(Tenant.slug == "bad-zone")) is None

    created = await http.post(
        f"{API}/workspaces",
        json={"name": "Gulf Works", "slug": "gulf-works", "timezone": "Asia/Dubai"},
        headers=_bearer(session),
    )
    assert created.status_code == 201, created.text
    workspace = created.json()["workspace"]
    assert workspace["timezone"] == "Asia/Dubai"
    assert workspace["utc_offset"] == "+04:00"
    row = await db_session.scalar(select(Tenant).where(Tenant.slug == "gulf-works"))
    assert row is not None and row.timezone == "Asia/Dubai"


async def test_registration_requires_a_zone_from_the_list(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    payload = {
        "email": "tz-register@example.com",
        "password": PASSWORD,
        "workspace_name": "Registered",
        "workspace_slug": "tz-registered",
    }
    missing = await http.post(f"{API}/auth/register", json=payload)
    assert missing.status_code == 422
    unknown = await http.post(f"{API}/auth/register", json={**payload, "timezone": "US/Eastern"})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "timezone_unknown"
    assert await db_session.scalar(select(Tenant).where(Tenant.slug == "tz-registered")) is None

    accepted = await http.post(f"{API}/auth/register", json={**payload, "timezone": "Asia/Riyadh"})
    assert accepted.status_code == 202, accepted.text
    row = await db_session.scalar(select(Tenant).where(Tenant.slug == "tz-registered"))
    assert row is not None and row.timezone == "Asia/Riyadh"


# ---------------------------------------------------------------- reading and changing


async def test_the_workspace_shows_its_zone_and_an_admin_changes_it_with_a_trail(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session, email="tz-owner@example.com")
    admin = await _user(db_session, email="tz-admin@example.com")
    member = await _user(db_session, email="tz-member@example.com")
    tenant = await _workspace(db_session, slug="tz-change", owner=owner)
    await _join(db_session, tenant=tenant, user=admin, role=TenantRole.TENANT_ADMIN)
    await _join(db_session, tenant=tenant, user=member, role=TenantRole.MEMBER)

    as_member = await _select_workspace(http, await _login(http, member.email), "tz-change")
    shown = await http.get(f"{API}/workspace", headers=as_member)
    assert shown.status_code == 200
    assert shown.json()["timezone"] == TEST_TIMEZONE
    refused = await http.patch(
        f"{API}/workspace", json={"timezone": "Asia/Dubai"}, headers=as_member
    )
    assert refused.status_code == 403

    as_admin = await _select_workspace(http, await _login(http, admin.email), "tz-change")
    unknown = await http.patch(f"{API}/workspace", json={"timezone": "Mars/Base"}, headers=as_admin)
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "timezone_unknown"
    changed = await http.patch(
        f"{API}/workspace", json={"timezone": "Asia/Dubai"}, headers=as_admin
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["timezone"] == "Asia/Dubai"
    assert changed.json()["utc_offset"] == "+04:00"

    trail = list(
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.WORKSPACE_UPDATED,
                    AuditLog.tenant_id == tenant.id,
                )
            )
        ).scalars()
    )
    assert len(trail) == 1
    assert trail[0].meta == {"previous_timezone": TEST_TIMEZONE, "timezone": "Asia/Dubai"}
