"""What the campaigns page reports after a send (PG-10, PG-11, PG-12, PG-14).

Replies and STOPs arrive through the real webhook ingestion; the copies they
answer are recipients marked sent at chosen moments.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.db.models.audit import AuditAction, AuditLog
from app.db.models.campaign import CampaignRecipient, RecipientStatus
from app.db.models.conversation import Contact
from app.db.models.enums import TenantRole
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.services.whatsapp_service import WhatsAppIngestionService
from tests.integration.campaign_api import API, client_for
from tests.integration.campaign_harness import (
    FakeMeta,
    Workspace,
    compose,
    erase,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    _, built = worker(prepared_database, FakeMeta(), name="results")
    await quiesce(built)
    try:
        yield built
    finally:
        await quiesce(built)
        await erase(built)
        await built.dispose()


def _inbound(phone_number_id: str, wa_id: str, text: str, at: datetime) -> dict[str, Any]:
    """A customer's text message as Meta's `messages` webhook carries it."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "201000000000",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Customer"}, "wa_id": wa_id}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": str(int(at.timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _say(
    database: Database, workspace: Workspace, contact_id: uuid.UUID, text: str, at: datetime
) -> None:
    async with database.session_factory() as session:
        account = await session.get(WhatsAppAccount, workspace.account_id)
        contact = await session.get(Contact, contact_id)
        assert account is not None and contact is not None and contact.wa_id is not None
        phone_number_id, wa_id = account.phone_number_id, contact.wa_id
    async with database.session_factory() as session:
        await WhatsAppIngestionService(session=session).ingest(
            _inbound(phone_number_id, wa_id, text, at)
        )
        await session.commit()


async def _sent(database: Database, campaign_id: uuid.UUID, *, at: datetime) -> None:
    """Every recipient of the campaign accepted by Meta at `at`."""
    async with database.session_factory() as session:
        await session.execute(
            update(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id)
            .values(status=RecipientStatus.SENT, sent_at=at)
        )
        await session.commit()


async def _row(
    database: Database, campaign_id: uuid.UUID, contact_id: uuid.UUID
) -> CampaignRecipient:
    async with database.session_factory() as session:
        found = await session.scalar(
            select(CampaignRecipient).where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.contact_id == contact_id,
            )
        )
        assert found is not None
        return found


# ---------------------------------------------------------------- replies


async def test_a_reply_is_recorded_once_and_only_inside_the_window(database: Database) -> None:
    workspace = await seed(database, recipients=2)
    near, far = workspace.contact_ids
    campaign = await compose(database, workspace, schedule=False)
    await _sent(database, campaign, at=datetime.now(UTC) - timedelta(minutes=10))
    async with database.session_factory() as session:
        await session.execute(
            update(CampaignRecipient)
            .where(CampaignRecipient.contact_id == far)
            .values(sent_at=datetime.now(UTC) - timedelta(hours=80))
        )
        await session.commit()

    first = datetime.now(UTC)
    await _say(database, workspace, near, "How much is it?", first)
    await _say(database, workspace, near, "Hello?", first + timedelta(seconds=30))
    await _say(database, workspace, far, "Late answer", first)

    replied = await _row(database, campaign, near)
    assert replied.replied_at is not None
    assert abs((replied.replied_at - first).total_seconds()) < 1  # the first, unmoved
    assert (await _row(database, campaign, far)).replied_at is None
    async with client_for(database, workspace) as client:
        stats = (await client.get(f"{API}/campaigns/{campaign}/statistics")).json()
    assert stats["replied"] == 1


async def test_a_reply_after_two_campaigns_counts_for_the_latest_only(database: Database) -> None:
    workspace = await seed(database, recipients=1)
    (person,) = workspace.contact_ids
    older = await compose(database, workspace, schedule=False)
    newer = await compose(database, workspace, schedule=False)
    await _sent(database, older, at=datetime.now(UTC) - timedelta(hours=30))
    await _sent(database, newer, at=datetime.now(UTC) - timedelta(hours=2))

    await _say(database, workspace, person, "Interested", datetime.now(UTC))

    assert (await _row(database, newer, person)).replied_at is not None
    assert (await _row(database, older, person)).replied_at is None


async def test_a_stop_is_a_reply_and_an_opt_out_after(database: Database) -> None:
    workspace = await seed(database, recipients=1)
    (person,) = workspace.contact_ids
    campaign = await compose(database, workspace, schedule=False)
    await _sent(database, campaign, at=datetime.now(UTC) - timedelta(hours=1))

    await _say(database, workspace, person, "STOP", datetime.now(UTC))

    row = await _row(database, campaign, person)
    assert row.replied_at is not None and row.opted_out_after_at is not None
    async with client_for(database, workspace) as client:
        stats = (await client.get(f"{API}/campaigns/{campaign}/statistics")).json()
    assert stats["replied"] == 1 and stats["opted_out_after"] == 1


async def test_the_reasons_breakdown_counts_every_recipient_once(database: Database) -> None:
    workspace = await seed(database, recipients=6)
    campaign = await compose(database, workspace, schedule=False)
    a, b, c, d, _e, _f = workspace.contact_ids
    async with database.session_factory() as session:
        for contact_id, status, reason in (
            (a, RecipientStatus.SENT, None),
            (b, RecipientStatus.SENT, None),
            (c, RecipientStatus.SKIPPED, "opted_out"),
            (d, RecipientStatus.FAILED, "marketing_limit_per_user"),
        ):
            await session.execute(
                update(CampaignRecipient)
                .where(
                    CampaignRecipient.campaign_id == campaign,
                    CampaignRecipient.contact_id == contact_id,
                )
                .values(status=status, reason_code=reason)
            )
        await session.commit()
    async with client_for(database, workspace) as client:
        stats = (await client.get(f"{API}/campaigns/{campaign}/statistics")).json()
    reasons = {(r["status"], r["reason_code"]): r["count"] for r in stats["reasons"]}
    assert reasons == {
        ("sent", "unrecorded"): 2,
        ("skipped", "opted_out"): 1,
        ("failed", "marketing_limit_per_user"): 1,
        ("pending", "unrecorded"): 2,
    }
    assert sum(r["count"] for r in stats["reasons"]) == stats["total"] == 6


# ------------------------------------------------------- summary, progress


async def test_the_summary_is_the_sum_of_every_campaign_not_a_page(database: Database) -> None:
    workspace = await seed(database, recipients=4)
    campaigns = [await compose(database, workspace, schedule=False) for _ in range(3)]
    await _sent(database, campaigns[0], at=datetime.now(UTC) - timedelta(minutes=5))
    await _say(database, workspace, workspace.contact_ids[0], "Yes", datetime.now(UTC))
    async with client_for(database, workspace, role=TenantRole.MEMBER) as client:
        page = (await client.get(f"{API}/campaigns", params={"limit": 1})).json()
        assert len(page["items"]) == 1 and page["next_cursor"]
        summary = await client.get(f"{API}/campaigns/summary")
        assert summary.status_code == 200, summary.text
        body = summary.json()
        expected: dict[str, int] = {}
        for campaign in campaigns:
            stats = (await client.get(f"{API}/campaigns/{campaign}/statistics")).json()
            for key in ("sent", "delivered", "read", "failed", "skipped", "replied", "pending"):
                expected[key] = expected.get(key, 0) + stats[key]
        naive = await client.get(f"{API}/campaigns/summary", params={"from": "2026-10-01T00:00:00"})
    assert {key: body["recipients"][key] for key in expected} == expected
    assert body["campaigns_by_status"] == {"draft": 3}
    assert body["recipients"]["sent"] == 4 and body["recipients"]["replied"] == 1
    assert body["rates"]["reply"] == 0.25
    assert body["window"]["timezone"] == "Africa/Cairo"
    assert naive.status_code == 422


async def test_progress_answers_twenty_campaigns_at_once_and_304_when_unchanged(
    database: Database,
) -> None:
    workspace = await seed(database, recipients=1)
    campaigns = [await compose(database, workspace, schedule=False) for _ in range(20)]
    async with client_for(database, workspace, role=TenantRole.MEMBER) as client:
        first = await client.get(
            f"{API}/campaigns/progress", params={"ids": [str(c) for c in campaigns]}
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert [item["id"] for item in body["items"]] == [str(c) for c in campaigns]
        assert body["poll_after_seconds"] == 60
        assert body["items"][0]["counts"]["pending"] == 1
        again = await client.get(
            f"{API}/campaigns/progress",
            params={"ids": [str(c) for c in campaigns]},
            headers={"If-None-Match": first.headers["ETag"]},
        )
        assert again.status_code == 304
        await _sent(database, campaigns[0], at=datetime.now(UTC))
        changed = await client.get(
            f"{API}/campaigns/progress",
            params={"ids": [str(c) for c in campaigns]},
            headers={"If-None-Match": first.headers["ETag"]},
        )
        assert changed.status_code == 200
        assert changed.json()["items"][0]["counts"]["sent"] == 1
        too_many = await client.get(
            f"{API}/campaigns/progress",
            params={"ids": [str(c) for c in campaigns] + [str(uuid.uuid4())]},
        )
        assert too_many.status_code == 422
        unknown = await client.get(f"{API}/campaigns/progress", params={"ids": [str(uuid.uuid4())]})
        assert unknown.status_code == 404


# ------------------------------------------------------------------ export


async def test_the_export_is_excel_safe_filtered_bounded_and_audited(database: Database) -> None:
    workspace = await seed(database, recipients=3)
    a, b, c = workspace.contact_ids
    async with database.session_factory() as session:
        for contact_id, name in ((a, "محمد علي"), (b, "=1+1"), (c, "@SUM(A1)")):
            await session.execute(
                update(Contact).where(Contact.id == contact_id).values(display_name=name)
            )
        await session.commit()
    campaign = await compose(database, workspace, schedule=False)
    await _sent(database, campaign, at=datetime.now(UTC) - timedelta(minutes=3))
    async with database.session_factory() as session:
        await session.execute(
            update(CampaignRecipient)
            .where(CampaignRecipient.contact_id == c)
            .values(status=RecipientStatus.SKIPPED, reason_code="opted_out", sent_at=None)
        )
        await session.commit()

    async with client_for(database, workspace) as client:
        whole = await client.get(f"{API}/campaigns/{campaign}/recipients/export.csv")
        assert whole.status_code == 200, whole.text
        assert whole.headers["content-type"].startswith("text/csv")
        raw = whole.content
        assert raw.startswith("﻿".encode())
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        assert rows[0][:3] == ["contact_name", "phone", "status"]
        names = {row[0] for row in rows[1:]}
        assert names == {"محمد علي", "'=1+1", "'@SUM(A1)"}
        sent_at = next(row[5] for row in rows[1:] if row[0] == "محمد علي")
        assert sent_at.endswith(("+02:00", "+03:00"))

        skipped = await client.get(
            f"{API}/campaigns/{campaign}/recipients/export.csv",
            params={"status": "skipped"},
        )
        skipped_rows = list(csv.reader(io.StringIO(skipped.content.decode("utf-8-sig"))))
        assert [row[3] for row in skipped_rows[1:]] == ["opted_out"]

        for _ in range(3):
            assert (
                await client.get(f"{API}/campaigns/{campaign}/recipients/export.csv")
            ).status_code == 200
        sixth = await client.get(f"{API}/campaigns/{campaign}/recipients/export.csv")
        assert sixth.status_code == 429
        assert sixth.json()["error"]["code"] == "export_limit"

    async with client_for(database, workspace, role=TenantRole.MEMBER) as member:
        refused = await member.get(f"{API}/campaigns/{campaign}/recipients/export.csv")
        assert refused.status_code == 403

    async with database.session_factory() as session:
        entries = list(
            (
                await session.execute(
                    select(AuditLog.meta).where(
                        AuditLog.target_id == campaign,
                        AuditLog.action == AuditAction.CAMPAIGN_RECIPIENTS_EXPORTED,
                    )
                )
            ).scalars()
        )
    assert len(entries) == 5
    assert sorted(entry["rows"] for entry in entries) == [1, 3, 3, 3, 3]
    for entry in entries:
        assert "محمد" not in str(entry)
        assert set(entry) == {"rows", "filters"}
