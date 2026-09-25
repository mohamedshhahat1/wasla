"""Migrations 0070 and 0071 against a database holding real 0069 billing rows.

Built from scratch to `0069`, seeded with the shapes production holds - a free
workspace mid-trial, one its trial expired, one its owner cancelled, paid
subscriptions whose periods began on the 31st and on a leap day, an issued
renewal, an unissued checkout invoice and a payment with a refund outstanding -
then upgraded and inspected, downgraded, and upgraded again.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.payment_tokens import ENCRYPTION_KEY, FINGERPRINT_KEY

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]

JAN_31 = datetime(2026, 1, 31, 10, tzinfo=UTC)
FEB_28 = datetime(2026, 2, 28, 10, tzinfo=UTC)
MAR_31 = datetime(2026, 3, 31, 10, tzinfo=UTC)
LEAP_DAY = datetime(2028, 2, 29, 10, tzinfo=UTC)
LEAP_DAY_NEXT = datetime(2029, 2, 28, 10, tzinfo=UTC)


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


async def _execute(url: str, statements: list[tuple[str, dict[str, Any]]]) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for statement, params in statements:
                await connection.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _rows(url: str, statement: str, params: dict[str, Any] | None = None) -> list[Any]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return list((await connection.execute(text(statement), params or {})).all())
    finally:
        await engine.dispose()


class Seed:
    def __init__(self) -> None:
        self.free_plan, self.paid_plan, self.yearly_plan = (uuid.uuid4() for _ in range(3))
        self.trialing, self.expired, self.cancelled = (uuid.uuid4() for _ in range(3))
        self.month_end, self.legacy, self.leap = (uuid.uuid4() for _ in range(3))
        self.renewal, self.checkout, self.payment = (uuid.uuid4() for _ in range(3))
        self.tag = uuid.uuid4().hex[:8]

    def statements(self) -> list[tuple[str, dict[str, Any]]]:
        plan = (
            "INSERT INTO plans (id, code, name, price, currency, interval, trial_days, limits,"
            " is_public, is_active, sort_order) VALUES (:id, :code, :name, :price, 'EGP',"
            " :interval, 14, '{\"agents\": 1}', true, true, 0)"
        )
        tenant = "INSERT INTO tenants (id, name, slug, status) VALUES (:id, 'M', :slug, 'active')"
        subscription = (
            "INSERT INTO subscriptions (id, tenant_id, plan_id, status, current_period_start,"
            " current_period_end, cancel_at_period_end, trial_ends_at, cancelled_at, ended_at)"
            " VALUES (:id, :id, :plan, :status, :start, :end, false, :trial, :cancelled, :ended)"
        )
        out: list[tuple[str, dict[str, Any]]] = [
            (
                plan,
                {
                    "id": self.free_plan,
                    "code": f"free-{self.tag}",
                    "name": "Free",
                    "price": Decimal("0.00"),
                    "interval": "monthly",
                },
            ),
            (
                plan,
                {
                    "id": self.paid_plan,
                    "code": f"paid-{self.tag}",
                    "name": "Paid",
                    "price": Decimal("99.00"),
                    "interval": "monthly",
                },
            ),
            (
                plan,
                {
                    "id": self.yearly_plan,
                    "code": f"year-{self.tag}",
                    "name": "Year",
                    "price": Decimal("999.00"),
                    "interval": "yearly",
                },
            ),
        ]
        cases = [
            (self.trialing, self.free_plan, "trialing", FEB_28, MAR_31, MAR_31, None, None),
            (self.expired, self.free_plan, "expired", JAN_31, FEB_28, FEB_28, None, FEB_28),
            (self.cancelled, self.free_plan, "cancelled", JAN_31, FEB_28, None, JAN_31, FEB_28),
            (self.month_end, self.paid_plan, "active", JAN_31, FEB_28, None, None, None),
            (self.legacy, self.paid_plan, "active", FEB_28, MAR_31, None, None, None),
            (self.leap, self.yearly_plan, "active", LEAP_DAY, LEAP_DAY_NEXT, None, None, None),
        ]
        for sid, plan_id, status, start, end, trial, cancelled, ended in cases:
            out.append((tenant, {"id": sid, "slug": f"m-{sid.hex[:12]}"}))
            out.append(
                (
                    subscription,
                    {
                        "id": sid,
                        "plan": plan_id,
                        "status": status,
                        "start": start,
                        "end": end,
                        "trial": trial,
                        "cancelled": cancelled,
                        "ended": ended,
                    },
                )
            )
        invoice = (
            "INSERT INTO invoices (id, tenant_id, subscription_id, status, plan_code, amount_due,"
            " amount_paid, currency, period_start, period_end, lines, collection_attempts,"
            " issued_at) VALUES (:id, :tenant, :tenant, 'open', :code, 99, 0, 'EGP', :start,"
            " :end, '[]', 0, :issued)"
        )
        out += [
            (
                invoice,
                {
                    "id": self.renewal,
                    "tenant": self.month_end,
                    "code": f"paid-{self.tag}",
                    "start": JAN_31,
                    "end": FEB_28,
                    "issued": JAN_31,
                },
            ),
            (
                invoice,
                {
                    "id": self.checkout,
                    "tenant": self.legacy,
                    "code": f"paid-{self.tag}",
                    "start": FEB_28,
                    "end": MAR_31,
                    "issued": None,
                },
            ),
            (
                "INSERT INTO payments (id, tenant_id, invoice_id, status, amount, currency,"
                " provider, refunded_amount, is_automatic, refund_requested_at) VALUES (:id,"
                " :tenant, :invoice, 'succeeded', 99, 'EGP', 'manual', 0, false, :at)",
                {
                    "id": self.payment,
                    "tenant": self.month_end,
                    "invoice": self.renewal,
                    "at": FEB_28,
                },
            ),
        ]
        return out


async def _assert_upgraded(url: str, seed: Seed) -> None:
    trial_days = await _rows(url, "SELECT count(*) FROM plans WHERE trial_days <> 0")
    assert trial_days[0][0] == 0

    versions = await _rows(
        url,
        "SELECT plan_id, version, price, trial_days FROM plan_versions WHERE plan_id IN"
        " (:a, :b, :c)",
        {"a": seed.free_plan, "b": seed.paid_plan, "c": seed.yearly_plan},
    )
    assert sorted((row[1], row[2]) for row in versions) == [
        (1, Decimal("0.00")),
        (1, Decimal("99.00")),
        (1, Decimal("999.00")),
    ]

    subs = {
        row[0]: row[1:]
        for row in await _rows(
            url,
            "SELECT s.id, s.status::text, s.billing_anchor_at, s.trial_ends_at,"
            " s.current_period_start, v.version FROM subscriptions s"
            " JOIN plan_versions v ON v.id = s.plan_version_id WHERE s.id IN"
            " (:a, :b, :c, :d, :e, :f)",
            {
                "a": seed.trialing,
                "b": seed.expired,
                "c": seed.cancelled,
                "d": seed.month_end,
                "e": seed.legacy,
                "f": seed.leap,
            },
        )
    }
    assert len(subs) == 6, "every subscription is pinned to a version"
    # BILL-01: a free trial becomes plain active; an expired free trial revives.
    assert subs[seed.trialing][0] == "active" and subs[seed.trialing][2] is None
    assert subs[seed.expired][0] == "active"
    assert subs[seed.expired][3] > FEB_28, "revived with a fresh period"
    # A customer's own cancellation is theirs, not the defect's.
    assert subs[seed.cancelled][0] == "cancelled"
    # BILL-18: anchors.
    assert subs[seed.month_end][1] == JAN_31
    assert subs[seed.legacy][1] == MAR_31, "an irregular legacy period anchors on its end"
    assert subs[seed.leap][1] == LEAP_DAY

    invoices = {
        row[0]: row[1:]
        for row in await _rows(
            url,
            "SELECT id, purpose::text, plan_version_id FROM invoices WHERE id IN (:a, :b)",
            {"a": seed.renewal, "b": seed.checkout},
        )
    }
    assert invoices[seed.renewal][0] == "renewal" and invoices[seed.renewal][1] is not None
    assert invoices[seed.checkout] == ("checkout", None)

    payment = await _rows(
        url,
        "SELECT refund_requested_amount FROM payments WHERE id = :id",
        {"id": seed.payment},
    )
    assert payment[0][0] == Decimal("99.00")

    with pytest.raises(DBAPIError):
        await _execute(url, [("UPDATE plan_versions SET price = 1", {})])


def test_billing_migrations_carry_real_rows_forward_and_back(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", ENCRYPTION_KEY)
    monkeypatch.setenv("PAYMENT_TOKEN_FINGERPRINT_KEY", FINGERPRINT_KEY)
    name = f"wasla_bill_mig_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    seed = Seed()
    try:
        _alembic(target, "upgrade", "0069")
        asyncio.run(_execute(target, seed.statements()))

        _alembic(target, "upgrade", "head")
        asyncio.run(_assert_upgraded(target, seed))

        _alembic(target, "downgrade", "0069")
        columns = asyncio.run(
            _rows(
                target,
                "SELECT column_name FROM information_schema.columns WHERE table_name ="
                " 'invoices' AND column_name = 'purpose'",
            )
        )
        assert columns == []
        survivors = asyncio.run(
            _rows(
                target,
                "SELECT count(*) FROM invoices WHERE id IN (:a, :b)",
                {"a": seed.renewal, "b": seed.checkout},
            )
        )
        assert survivors[0][0] == 2

        _alembic(target, "upgrade", "head")
        asyncio.run(_assert_upgraded(target, seed))
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE {name} WITH (FORCE)"))
