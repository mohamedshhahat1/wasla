"""Migration 0120 against a database holding the last day's sends (CPF-04).

Built from scratch to 0119, seeded through the models (0120 adds no column),
upgraded to 0120: every contact sent a marketing template in the last 24 hours
carries that send in `last_marketing_sent_at`, so the per-customer cap sees
what was sent just before a deploy; and every template accepted in the last 24
hours is in the ledger under its number's portfolio, so Meta's window is not
counted from zero - and nothing else is filled in.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.conversation import (
    Contact,
    Conversation,
    MessageDeliveryState,
    MessageKind,
    MessageOrigin,
)
from app.db.models.whatsapp_template import TemplateCategory
from app.repositories.conversation_repository import MessageRepository
from tests.integration.test_campaigns import _account, _customer, _template, _tenant

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _alembic(url: str, action: str, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["wasla_database_url"] = url
    getattr(command, action)(config, revision)


async def _admin(admin_url: str, statement: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


async def _seed(url: str) -> dict[str, tuple[uuid.UUID, datetime | None]]:
    """Contacts by case, each with the value 0120 should leave on it."""
    engine = create_async_engine(url)
    now = datetime.now(UTC)
    expected: dict[str, tuple[uuid.UUID, datetime | None]] = {}
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            tenant = await _tenant(session, slug=f"cpf04-{uuid.uuid4().hex[:6]}")
            account = await _account(session, tenant=tenant)
            await _template(session, tenant=tenant, account=account)
            utility = await _template(session, tenant=tenant, account=account, name="receipt")
            utility.category = TemplateCategory.UTILITY

            async def sent(
                case: str,
                *,
                ago: timedelta,
                origin: MessageOrigin = MessageOrigin.CAMPAIGN,
                name: str = "spring_offer",
                state: MessageDeliveryState = MessageDeliveryState.SENT,
            ) -> tuple[uuid.UUID, datetime]:
                contact = await _customer(
                    session,
                    tenant=tenant,
                    account=account,
                    wa_id=f"2010{abs(hash(case)) % 10**8:08d}",
                )
                conversation = await session.scalar(
                    select(Conversation).where(Conversation.contact_id == contact.id)
                )
                assert conversation is not None
                message = await MessageRepository(session, tenant_id=tenant.id).stage_outbound(
                    conversation_id=conversation.id,
                    kind=MessageKind.TEMPLATE,
                    body=None,
                    template_name=name,
                    template_language="ar_EG",
                    origin=origin,
                )
                await session.flush()
                message.sent_at = now - ago
                message.delivery_state = state
                return contact.id, now - ago

            recent, at = await sent("recent", ago=timedelta(hours=2))
            expected["recent"] = (recent, at)
            old, _ = await sent("old", ago=timedelta(hours=30))
            expected["old"] = (old, None)
            test, _ = await sent("test", ago=timedelta(hours=1), origin=MessageOrigin.CAMPAIGN_TEST)
            expected["test"] = (test, None)
            receipt, _ = await sent("utility", ago=timedelta(hours=1), name="receipt")
            expected["utility"] = (receipt, None)
            lost, _ = await sent(
                "undelivered", ago=timedelta(hours=1), state=MessageDeliveryState.UNDELIVERED
            )
            expected["undelivered"] = (lost, None)
            by_hand, at = await sent("human", ago=timedelta(hours=3), origin=MessageOrigin.HUMAN)
            expected["human"] = (by_hand, at)
            newer, _ = await sent("newer", ago=timedelta(hours=3))
            claimed = now - timedelta(minutes=10)
            contact = await session.get(Contact, newer)
            assert contact is not None
            contact.last_marketing_sent_at = claimed
            expected["newer"] = (newer, claimed)
            await session.commit()
    finally:
        await engine.dispose()
    return expected


async def _ledger(url: str) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session:
            rows = await session.execute(
                text(
                    "SELECT b.recipient, b.source, b.sent_at, b.message_id IS NOT NULL,"
                    " p.key_kind, p.checked_at IS NULL, a.portfolio_id = b.portfolio_id"
                    " FROM business_initiated_sends b"
                    " JOIN whatsapp_business_portfolios p ON p.id = b.portfolio_id"
                    " JOIN whatsapp_accounts a ON a.waba_id = p.meta_key"
                )
            )
            return [tuple(row) for row in rows.all()]
    finally:
        await engine.dispose()


async def _values(url: str) -> dict[uuid.UUID, datetime | None]:
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session:
            rows = await session.execute(text("SELECT id, last_marketing_sent_at FROM contacts"))
            return {row[0]: row[1] for row in rows.all()}
    finally:
        await engine.dispose()


def test_0120_fills_the_cap_from_the_last_day_of_marketing(database_url: str) -> None:
    name = f"wasla_cpf04_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    try:
        _alembic(target, "upgrade", "0119")
        expected = asyncio.run(_seed(target))
        before = asyncio.run(_values(target))
        assert before[expected["recent"][0]] is None
        assert asyncio.run(_ledger(target)) == []

        _alembic(target, "upgrade", "0120")
        after = asyncio.run(_values(target))
        for case, (contact_id, value) in expected.items():
            assert after[contact_id] == value, case

        # The ledger: every accepted template of the last day, whatever its
        # category or origin, one row each; the 30-hour-old and the provably
        # undelivered ones are not there. One unknown WABA-keyed portfolio,
        # the number linked to it.
        ledger = asyncio.run(_ledger(target))
        assert sorted(row[1] for row in ledger) == [
            "campaign",
            "campaign",
            "campaign",
            "campaign_test",
            "manual_template",
        ]
        assert all(row[3:] == (True, "waba", True, True) for row in ledger)
        assert len({row[0] for row in ledger}) == len(ledger)

        _alembic(target, "downgrade", "0119")
        assert asyncio.run(_values(target)) == after
        assert asyncio.run(_ledger(target)) == ledger
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
