"""Recovering opt-outs the live path missed, from retained evidence (OMNI-030).

Before OMNI-030 a "Stop promotions" tap was stored with no text and opted nobody
out. Raw deliveries are kept 30 days after processing, so for that long the taps
are still on record; `recover-button-opt-outs` replays them through the one
opt-out writer. Each case stands where production stands: the tap is ingested,
then the fixed live path's effects are undone, leaving exactly what the old code
left - the raw event and an un-opted-out contact.

Mutant this suite kills: M-O04 (the replay ignoring a newer resume).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.conversation import Contact, Message
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.services.campaign_service import CampaignService
from app.services.opt_out_recovery import recover_button_opt_outs
from app.services.whatsapp_service import WhatsAppIngestionService
from tests.integration.test_omnichannel_reply_actions import (
    CUSTOMER,
    _button,
    _delivery,
    _mark,
    _number,
)

pytestmark = pytest.mark.integration


async def _lost_tap(
    session: AsyncSession,
    account: WhatsAppAccount,
    *,
    text: str = "Stop promotions",
    payload: str = "STOP-PAYLOAD",
    tapped_at: datetime | None = None,
    phone: str = CUSTOMER,
) -> Contact:
    """A tap as the pre-OMNI-030 code stored it: evidence kept, nobody opted out."""
    at = tapped_at or datetime.now(UTC) - timedelta(days=3)
    delivery = _delivery(account, _button(text, payload))
    value = delivery["entry"][0]["changes"][0]["value"]
    value["contacts"][0]["wa_id"] = phone
    value["messages"][0].update({"from": phone, "timestamp": str(int(at.timestamp()))})
    await WhatsAppIngestionService(session=session).ingest(delivery)
    await session.flush()
    contact = await session.scalar(
        select(Contact).where(Contact.tenant_id == account.tenant_id, Contact.wa_id == phone)
    )
    assert contact is not None
    # Undo what the fixed live path did, to stand where production stands.
    contact.marketing_opt_out_at = None
    contact.opt_out_source = None
    contact.opt_out_via = None
    message = await session.scalar(
        select(Message).where(
            Message.tenant_id == account.tenant_id,
            Message.conversation_id.is_not(None),
            Message.action_payload == payload,
        )
    )
    assert message is not None
    message.body = None
    message.action_source = None
    message.action_payload = None
    message.action_title = None
    await session.flush()
    return contact


async def test_the_replay_counts_in_a_dry_run_and_writes_nothing(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    contact = await _lost_tap(db_session, account)

    report = await recover_button_opt_outs(db_session, apply=False)

    counts = report.by_workspace[account.tenant_id]
    assert (counts.candidates, counts.applied) == (1, 1)
    assert contact.marketing_opt_out_at is None


async def test_the_replay_applies_once_dated_by_the_tap(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    tapped_at = (datetime.now(UTC) - timedelta(days=5)).replace(microsecond=0)
    contact = await _lost_tap(db_session, account, tapped_at=tapped_at)

    first = await recover_button_opt_outs(db_session, apply=True)
    second = await recover_button_opt_outs(db_session, apply=True)

    assert first.by_workspace[account.tenant_id].applied == 1
    assert contact.marketing_opt_out_at == tapped_at
    assert contact.opt_out_source is OptOutSource.CUSTOMER
    assert contact.opt_out_via is OptOutVia.REPLAY
    # A second apply changes nothing.
    assert second.by_workspace[account.tenant_id].applied == 0
    assert second.by_workspace[account.tenant_id].already_opted_out == 1
    assert contact.marketing_opt_out_at == tapped_at


async def test_a_newer_resume_wins_over_older_evidence(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    contact = await _lost_tap(db_session, account, tapped_at=datetime.now(UTC) - timedelta(days=4))
    contact.marketing_resumed_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()

    report = await recover_button_opt_outs(db_session, apply=True)

    counts = report.by_workspace[account.tenant_id]
    assert (counts.applied, counts.skipped_newer_resume) == (0, 1)
    assert contact.marketing_opt_out_at is None


async def test_an_older_resume_does_not_protect_a_later_tap(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    contact = await _lost_tap(db_session, account, tapped_at=datetime.now(UTC) - timedelta(days=1))
    contact.marketing_resumed_at = datetime.now(UTC) - timedelta(days=4)
    await db_session.flush()

    report = await recover_button_opt_outs(db_session, apply=True)

    assert report.by_workspace[account.tenant_id].applied == 1
    assert contact.marketing_opt_out_at is not None


async def test_a_colleague_clearing_the_opt_out_is_a_resume_the_replay_respects(
    db_session: AsyncSession,
) -> None:
    account = await _number(db_session)
    contact = await _lost_tap(db_session, account, tapped_at=datetime.now(UTC) - timedelta(days=2))
    campaigns = CampaignService(session=db_session, tenant_id=account.tenant_id)
    await campaigns.set_opt_out(contact_id=contact.id, source=OptOutSource.CUSTOMER)
    await campaigns.clear_opt_out(contact.id)
    await db_session.flush()

    report = await recover_button_opt_outs(db_session, apply=True)

    assert report.by_workspace[account.tenant_id].skipped_newer_resume == 1
    assert contact.marketing_opt_out_at is None


async def test_the_replay_honours_the_workspaces_marked_payloads(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    await _mark(db_session, account, ["MKT_OPT_OUT"])
    marked = await _lost_tap(db_session, account, text="No thanks", payload="MKT_OPT_OUT")
    unmarked = await _lost_tap(
        db_session, account, text="Tell me more", payload="MORE", phone="201000000999"
    )

    report = await recover_button_opt_outs(db_session, apply=True)

    assert report.by_workspace[account.tenant_id].applied == 1
    assert marked.marketing_opt_out_at is not None
    assert unmarked.marketing_opt_out_at is None


async def test_the_command_prints_counts_and_no_personal_data(
    prepared_database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator entry point, end to end on committed rows: a dry run, two applies.

    Output is workspace ids and counts - never a phone, a business-scoped id or
    a word the customer tapped.
    """
    from scripts import omnichannel_invariants as tool

    database = Database(
        Settings(_env_file=None, environment="test", database_url=prepared_database)
    )
    try:
        async with database.session() as session:
            account = await _number(session)
            tenant_id = account.tenant_id
            contact = await _lost_tap(session, account)
            contact_id = contact.id
            await session.commit()
        monkeypatch.setenv("INVARIANTS_DATABASE_URL", prepared_database)

        assert await asyncio.to_thread(tool.main, ["recover-button-opt-outs"]) == 0
        dry = capsys.readouterr().out
        assert await asyncio.to_thread(tool.main, ["recover-button-opt-outs", "--apply"]) == 0
        applied = capsys.readouterr().out
        assert await asyncio.to_thread(tool.main, ["recover-button-opt-outs", "--apply"]) == 0
        again = capsys.readouterr().out

        assert f"workspace {tenant_id}: candidates 1, would_apply 1" in dry
        assert f"workspace {tenant_id}: candidates 1, applied 1" in applied
        assert f"workspace {tenant_id}: candidates 1, applied 0, already_opted_out 1" in again
        for output in (dry, applied, again):
            assert CUSTOMER not in output
            assert "Stop promotions" not in output
            assert "STOP-PAYLOAD" not in output
        async with database.session() as session:
            stored = await session.get(Contact, contact_id)
            assert stored is not None and stored.opt_out_via is OptOutVia.REPLAY
        assert tool.main(["recover-button-opt-outs", "--yes"]) == 64
    finally:
        async with database.session() as session:
            tenant = await session.get(Tenant, tenant_id)
            if tenant is not None:
                await session.delete(tenant)
            await session.commit()
        await database.dispose()
