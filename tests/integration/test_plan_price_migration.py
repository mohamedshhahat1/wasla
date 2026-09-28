"""Migration 0081 against a database holding real 0080 billing rows (ADR-116).

Built to `0069`, seeded with the shapes production holds (the same seed the
0070/0071 suite uses: free, trialing, cancelled and paid subscriptions, a
yearly-interval plan, issued renewals and checkouts), carried to `0080`, then
given a scheduled change, a paid checkout for a version and a custom plan
offer - and upgraded.

Proved:

- every priced version gets exactly one price, its own published terms, and
  **no yearly price is invented** for a monthly plan;
- every paid subscription, scheduled change, priced invoice and offer is pinned
  to that price; free subscriptions and top-ups name none; every usage cycle
  is its billing period, so monthly behaviour is unchanged;
- history the backfill could only map by guessing stops the migration with
  nothing changed;
- the downgrade refuses a row naming a price a pre-0081 schema cannot hold,
  and succeeds - and re-upgrades - when there is none.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.engine import make_url

from tests.integration.test_billing_migrations import Seed, _admin, _alembic, _execute, _rows
from tests.payment_tokens import ENCRYPTION_KEY, FINGERPRINT_KEY

pytestmark = pytest.mark.integration

AT = datetime(2026, 9, 1, 10, tzinfo=UTC)


def _database(database_url: str) -> tuple[str, str, str]:
    name = f"wasla_price_mig_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    return name, target, admin


def _one(url: str, statement: str, params: dict[str, Any] | None = None) -> Any:
    return asyncio.run(_rows(url, statement, params))[0][0]


def _version(url: str, plan_id: uuid.UUID) -> uuid.UUID:
    found: uuid.UUID = _one(
        url,
        "SELECT id FROM plan_versions WHERE plan_id = :p ORDER BY version DESC LIMIT 1",
        {"p": plan_id},
    )
    return found


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEYS", ENCRYPTION_KEY)
    monkeypatch.setenv("PAYMENT_TOKEN_FINGERPRINT_KEY", FINGERPRINT_KEY)


def test_0081_pins_every_row_to_its_published_price_and_invents_none(
    database_url: str, keys: None
) -> None:
    name, url, admin = _database(database_url)
    seed = Seed()
    try:
        _alembic(url, "upgrade", "0069")
        asyncio.run(_execute(url, seed.statements()))
        _alembic(url, "upgrade", "0080")
        paid_v = _version(url, seed.paid_plan)
        year_v = _version(url, seed.yearly_plan)
        custom_plan, custom_v, offer, checkout = (uuid.uuid4() for _ in range(4))
        asyncio.run(
            _execute(
                url,
                [
                    # A scheduled change to the yearly-interval plan's version.
                    (
                        "UPDATE subscriptions SET scheduled_plan_version_id = :v,"
                        " scheduled_change_source = 'operator' WHERE id = :s",
                        {"v": year_v, "s": seed.month_end},
                    ),
                    # A paid checkout for the paid version, at its price.
                    (
                        "INSERT INTO invoices (id, tenant_id, subscription_id, status, purpose,"
                        " plan_code, plan_version_id, amount_due, amount_paid, currency,"
                        " period_start, period_end, lines, collection_attempts, issued_at)"
                        " VALUES (:id, :t, :t, 'open', 'checkout', :code, :v, 99, 0, 'EGP',"
                        " :at, :at, '[]', 0, NULL)",
                        {
                            "id": checkout,
                            "t": seed.legacy,
                            "code": f"paid-{seed.tag}",
                            "v": paid_v,
                            "at": AT,
                        },
                    ),
                    # A custom plan offered to one workspace.
                    (
                        "INSERT INTO plans (id, code, name, price, currency, interval, trial_days,"
                        " limits, is_public, is_active, sort_order, scope, tenant_id) VALUES"
                        " (:id, :code, 'Custom', 2500, 'EGP', 'monthly', 0, '{}', false, true, 0,"
                        " 'tenant', :t)",
                        {"id": custom_plan, "code": f"custom-{seed.tag}", "t": seed.legacy},
                    ),
                    (
                        "INSERT INTO plan_versions (id, plan_id, version, name, price, currency,"
                        " interval, trial_days, limits, effective_at, created_at) VALUES"
                        " (:id, :p, 1, 'Custom', 2500, 'EGP', 'monthly', 0, '{}', :at, :at)",
                        {"id": custom_v, "p": custom_plan, "at": AT},
                    ),
                    (
                        "INSERT INTO custom_plan_offers (id, tenant_id, plan_id, plan_version_id,"
                        " status, reason) VALUES (:id, :t, :p, :v, 'offered', 'Deal.')",
                        {"id": offer, "t": seed.legacy, "p": custom_plan, "v": custom_v},
                    ),
                ],
            )
        )

        _alembic(url, "upgrade", "head")

        prices = asyncio.run(
            _rows(
                url,
                "SELECT v.id, p.billing_interval::text, p.interval_count, p.amount, p.retired_at"
                " FROM plan_prices p JOIN plan_versions v ON v.id = p.plan_version_id",
            )
        )
        by_version: dict[uuid.UUID, list[Any]] = {}
        for version_id, *rest in prices:
            by_version.setdefault(version_id, []).append(tuple(rest))
        assert all(len(rows) == 1 for rows in by_version.values()), "one price per version"
        assert by_version[paid_v] == [("monthly", 1, 99, None)]
        assert by_version[year_v] == [("yearly", 1, 999, None)]
        assert by_version[custom_v] == [("monthly", 1, 2500, None)]
        free_v = _version(url, seed.free_plan)
        assert free_v not in by_version, "a free version has no price"
        yearly_prices_on_monthly_plans = _one(
            url,
            "SELECT count(*) FROM plan_prices p JOIN plan_versions v ON v.id = p.plan_version_id"
            " WHERE p.billing_interval::text <> v.interval::text",
        )
        assert yearly_prices_on_monthly_plans == 0, "no annual price invented"

        pinned = dict(
            asyncio.run(
                _rows(
                    url,
                    "SELECT s.id, p.amount FROM subscriptions s"
                    " LEFT JOIN plan_prices p ON p.id = s.plan_price_id",
                )
            )
        )
        assert pinned[seed.month_end] == 99 and pinned[seed.leap] == 999
        assert pinned[seed.trialing] is None, "a free subscription names no price"
        scheduled = _one(
            url,
            "SELECT p.amount FROM subscriptions s JOIN plan_prices p"
            " ON p.id = s.scheduled_plan_price_id WHERE s.id = :s",
            {"s": seed.month_end},
        )
        assert scheduled == 999
        unequal_cycles = _one(
            url,
            "SELECT count(*) FROM subscriptions WHERE usage_period_start <> current_period_start"
            " OR usage_period_end <> current_period_end",
        )
        assert unequal_cycles == 0, "every usage cycle is its billing period"
        invoice = asyncio.run(
            _rows(
                url,
                "SELECT p.amount, i.billing_interval::text, i.interval_count FROM invoices i"
                " JOIN plan_prices p ON p.id = i.plan_price_id WHERE i.id = :i",
                {"i": checkout},
            )
        )
        assert invoice == [(99, "monthly", 1)]
        assert (
            _one(
                url,
                "SELECT p.amount FROM custom_plan_offers o JOIN plan_prices p"
                " ON p.id = o.plan_price_id WHERE o.id = :o",
                {"o": offer},
            )
            == 2500
        )

        # Round trip: nothing names a price 0080 could not hold.
        _alembic(url, "downgrade", "0080")
        assert _one(url, "SELECT to_regclass('plan_prices') IS NULL")
        _alembic(url, "upgrade", "head")
        assert _one(url, "SELECT count(*) FROM plan_prices") == len(prices)

        # A yearly price added to a monthly version cannot be represented below
        # 0081; a subscriber on it makes the downgrade refuse.
        asyncio.run(
            _execute(
                url,
                [
                    (
                        "INSERT INTO plan_prices (id, plan_version_id, billing_interval,"
                        " interval_count, amount, currency, created_at) VALUES"
                        " (gen_random_uuid(), :v, 'yearly', 1, 990, 'EGP', now())",
                        {"v": paid_v},
                    ),
                    (
                        "UPDATE subscriptions SET plan_price_id = p.id FROM plan_prices p"
                        " WHERE p.plan_version_id = :v AND p.billing_interval = 'yearly'"
                        " AND subscriptions.id = :s",
                        {"v": paid_v, "s": seed.month_end},
                    ),
                ],
            )
        )
        with pytest.raises(RuntimeError, match="re-price"):
            _alembic(url, "downgrade", "0080")
        assert _one(url, "SELECT version_num FROM alembic_version") == "0081"
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE {name} WITH (FORCE)"))


def test_0081_refuses_history_it_could_only_map_by_guessing(database_url: str, keys: None) -> None:
    name, url, admin = _database(database_url)
    seed = Seed()
    try:
        _alembic(url, "upgrade", "0069")
        asyncio.run(_execute(url, seed.statements()))
        _alembic(url, "upgrade", "0080")
        paid_v = _version(url, seed.paid_plan)
        # A renewal of the 99 version that charged 120: which price was it?
        asyncio.run(
            _execute(
                url,
                [
                    (
                        "INSERT INTO invoices (id, tenant_id, subscription_id, status, purpose,"
                        " plan_code, plan_version_id, amount_due, amount_paid, currency,"
                        " period_start, period_end, lines, collection_attempts, issued_at)"
                        " VALUES (gen_random_uuid(), :t, :t, 'open', 'renewal', 'x', :v, 120, 0,"
                        " 'EGP', :at, :at, '[]', 0, :at)",
                        {"t": seed.legacy, "v": paid_v, "at": AT},
                    )
                ],
            )
        )
        with pytest.raises(RuntimeError, match="guessing"):
            _alembic(url, "upgrade", "head")
        assert _one(url, "SELECT version_num FROM alembic_version") == "0080"
        assert _one(url, "SELECT to_regclass('plan_prices') IS NULL"), "nothing changed"
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE {name} WITH (FORCE)"))
