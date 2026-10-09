"""Migrations 0092-0098 against a database holding real 0091 rows (ADR-131).

Built from scratch to `0091` - the omnichannel foundation's head, before any
entitlement change - and seeded with the shapes it holds: the seeded catalogue
at version 1, a workspace subscribed to Pro with two WhatsApp numbers, a
WhatsApp-number top-up product and a granted number top-up, a contact who opted
out and one who resumed, an AI turn charged the old way. Then upgraded to head
and inspected, downgraded, upgraded again, and refused.

What is proved:

- **The data steps.** The number product is a channel slot typed WhatsApp
  (0093); the frozen number purchase is untouched and still counts as a
  WhatsApp slot; the person-level opt-out and resume are the contact's
  WhatsApp consent (0097); the subscriber stays on the version it bought.
- **The placeholder catalogue** (0098, ENT-20): Starter 1 / Pro 3 / Business 10
  connections and Enterprise unlimited, each with its channel types, the AI
  turns and every other limit carried over, Pro and Business still sold at
  their prices; and six channel top-ups, inactive and unpriced.
- **The keys are valid**: no constraint NOT VALID, no index INVALID.
- **The round trip is lossless** while nothing new-shaped exists.
- **A refused downgrade changes nothing**: with a change scheduled onto a
  placeholder version, the downgrade is refused and the database is still at
  head with the catalogue in place. A refusal further down - 0092's, with a
  Telegram connection - also rolls back everything above it: no downgrade in
  0092-0098 commits part-way, so 0094's indexes, the one-charge-per-turn
  unique among them, are still there and the stamp still says head.
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
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.repositories.entitlement_census import CAPACITY_CENSUS_SQL

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]

PLACEHOLDERS = {
    "starter": (1, ["whatsapp"]),
    "pro": (3, ["whatsapp", "instagram", "messenger"]),
    "business": (10, ["whatsapp", "instagram", "messenger", "telegram", "tiktok"]),
    "enterprise": (None, ["whatsapp", "instagram", "messenger", "telegram", "tiktok"]),
}
PLACEHOLDER_PRODUCTS = {
    "channel-connection-1": None,
    "whatsapp-connection-1": "whatsapp",
    "instagram-connection-1": "instagram",
    "messenger-connection-1": "messenger",
    "telegram-connection-1": "telegram",
    "tiktok-connection-1": "tiktok",
}


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


def _all(url: str, statement: str, params: dict[str, Any] | None = None) -> list[Any]:
    return asyncio.run(_rows(url, statement, params))


def _one(url: str, statement: str, params: dict[str, Any] | None = None) -> Any:
    return _all(url, statement, params)[0][0]


class Seed:
    """One workspace at 0091 with every shape the entitlement migrations map."""

    def __init__(self) -> None:
        self.tenant = uuid.uuid4()
        self.subscription = uuid.uuid4()
        self.numbers = [uuid.uuid4(), uuid.uuid4()]
        self.product = uuid.uuid4()
        self.grant = uuid.uuid4()
        self.opted_out, self.resumed = uuid.uuid4(), uuid.uuid4()
        self.tag = uuid.uuid4().hex[:8]
        self.now = datetime.now(UTC)

    def statements(self) -> list[tuple[str, dict[str, Any]]]:
        start, end = self.now - timedelta(days=5), self.now + timedelta(days=25)
        stopped = self.now - timedelta(days=2)
        pro_v1 = (
            "SELECT v.id FROM plan_versions v JOIN plans p ON p.id = v.plan_id"
            " WHERE p.code = 'pro' AND v.version = 1"
        )
        out: list[tuple[str, dict[str, Any]]] = [
            (
                "INSERT INTO tenants (id, name, slug, status) VALUES (:id, 'Mig', :slug, 'active')",
                {"id": self.tenant, "slug": f"ent-mig-{self.tag}"},
            ),
            (
                "INSERT INTO subscriptions (id, tenant_id, plan_id, plan_version_id,"  # noqa: S608
                " plan_price_id, status, current_period_start, current_period_end,"
                " cancel_at_period_end, usage_period_start, usage_period_end, billing_anchor_at)"
                " VALUES (:id, :tenant, (SELECT id FROM plans WHERE code = 'pro'),"
                f" ({pro_v1}), (SELECT id FROM plan_prices WHERE plan_version_id = ({pro_v1})),"
                " 'active', :start, :end, false, :start, :end, :start)",
                {"id": self.subscription, "tenant": self.tenant, "start": start, "end": end},
            ),
        ]
        for index, number in enumerate(self.numbers):
            out.append(
                (
                    "INSERT INTO whatsapp_accounts (id, tenant_id, phone_number_id, waba_id,"
                    " display_phone_number, status) VALUES (:id, :tenant, :phone, :waba,"
                    " '+201000000000', 'active')",
                    {
                        "id": number,
                        "tenant": self.tenant,
                        "phone": f"mig-{self.tag}-{index}",
                        "waba": f"waba-{self.tag}",
                    },
                )
            )
        out += [
            (
                "INSERT INTO topup_products (id, code, name, entitlement_key, quantity, price,"
                " currency, scope, is_active, is_public, validity_policy)"
                " VALUES (:id, :code, 'Extra number', 'whatsapp_numbers', 1, 200, 'EGP',"
                " 'global', true, true, 'current_period_end')",
                {"id": self.product, "code": f"numbers-{self.tag}"},
            ),
            (
                "INSERT INTO topup_purchases (id, tenant_id, subscription_id, source,"
                " product_name, entitlement_key, quantity, unit_price, total_amount, currency,"
                " billing_period_start, billing_period_end, expires_at, status, granted_at,"
                " reason) VALUES (:id, :tenant, :subscription, 'platform_grant',"
                " 'Platform grant', 'whatsapp_numbers', 1, 0, 0, 'EGP', :start, :end, :end,"
                " 'granted', :start, 'A goodwill number.')",
                {
                    "id": self.grant,
                    "tenant": self.tenant,
                    "subscription": self.subscription,
                    "start": start,
                    "end": end,
                },
            ),
            (
                "INSERT INTO contacts (id, tenant_id, wa_id, marketing_opt_out_at,"
                " opt_out_source, opt_out_via) VALUES (:id, :tenant, '201000000981', :at,"
                " 'customer', 'message')",
                {"id": self.opted_out, "tenant": self.tenant, "at": stopped},
            ),
            (
                "INSERT INTO contacts (id, tenant_id, wa_id, marketing_resumed_at)"
                " VALUES (:id, :tenant, '201000000982', :at)",
                {"id": self.resumed, "tenant": self.tenant, "at": stopped},
            ),
            (
                "INSERT INTO usage_events (id, tenant_id, event_type, quantity, unit,"
                " occurred_at) VALUES (gen_random_uuid(), :tenant, 'ai_turn', 1, 'count', :at)",
                {"tenant": self.tenant, "at": stopped},
            ),
        ]
        return out


def _assert_keys_valid(url: str) -> None:
    assert _one(url, "SELECT count(*) FROM pg_constraint WHERE NOT convalidated") == 0
    assert _one(url, "SELECT count(*) FROM pg_index WHERE NOT indisvalid") == 0


def _assert_upgraded(url: str, seed: Seed) -> None:
    assert _one(url, "SELECT version_num FROM alembic_version") == "0098"
    _assert_keys_valid(url)

    # 0093: the number product sells WhatsApp slots; the frozen grant is untouched.
    product = _all(
        url,
        "SELECT entitlement_key::text, channel_type::text, price FROM topup_products"
        " WHERE id = :id",
        {"id": seed.product},
    )[0]
    assert (product[0], product[1]) == ("channel_connections", "whatsapp")
    grant = _all(
        url,
        "SELECT entitlement_key::text, channel_type::text, status::text FROM topup_purchases"
        " WHERE id = :id",
        {"id": seed.grant},
    )[0]
    assert tuple(grant) == ("whatsapp_numbers", None, "granted")

    # 0097: the opt-out and the resume are the contacts' WhatsApp consents.
    consents = {
        row[0]: tuple(row[1:])
        for row in _all(
            url,
            "SELECT contact_id, channel::text, marketing_opt_out_at IS NOT NULL,"
            " opt_out_source::text, resumed_at IS NOT NULL FROM contact_channel_consents"
            " WHERE tenant_id = :t",
            {"t": seed.tenant},
        )
    }
    assert consents == {
        seed.opted_out: ("whatsapp", True, "customer", False),
        seed.resumed: ("whatsapp", False, None, True),
    }
    assert (
        _one(
            url,
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_name = 'contacts' AND column_name = 'marketing_opt_out_at'",
        )
        == 0
    )

    # 0098: the placeholder catalogue, as new versions beside the ones sold.
    for code, (connections, types) in PLACEHOLDERS.items():
        latest = _all(
            url,
            "SELECT v.version, v.limits, v.allowed_channel_types, v.price FROM plan_versions v"
            " JOIN plans p ON p.id = v.plan_id WHERE p.code = :code"
            " ORDER BY v.version DESC LIMIT 1",
            {"code": code},
        )[0]
        first = _all(
            url,
            "SELECT v.limits, v.price FROM plan_versions v JOIN plans p ON p.id = v.plan_id"
            " WHERE p.code = :code AND v.version = 1",
            {"code": code},
        )[0]
        assert latest[0] == 2, code
        assert latest[2] == types, code
        assert latest[1].get("channel_connections") == connections, code
        assert "whatsapp_numbers" not in latest[1], code
        carried = {k: v for k, v in first[0].items() if k != "whatsapp_numbers"}
        assert {k: v for k, v in latest[1].items() if k != "channel_connections"} == carried
        assert latest[3] == first[1], code
        mirrored = _all(
            url,
            "SELECT limits, allowed_channel_types FROM plans WHERE code = :code",
            {"code": code},
        )[0]
        assert (mirrored[0], mirrored[1]) == (latest[1], latest[2]), code
    prices = {
        row[0]: row[1]
        for row in _all(
            url,
            "SELECT p.code, pp.amount FROM plan_prices pp JOIN plan_versions v"
            " ON v.id = pp.plan_version_id JOIN plans p ON p.id = v.plan_id"
            " WHERE v.version = 2 AND pp.retired_at IS NULL",
        )
    }
    assert {code: str(amount) for code, amount in prices.items()} == {
        "pro": "99.00",
        "business": "299.00",
    }
    products = {
        row[0]: tuple(row[1:])
        for row in _all(
            url,
            "SELECT code, entitlement_key::text, channel_type::text, quantity, price, is_active"
            " FROM topup_products WHERE code = ANY(:codes)",
            {"codes": list(PLACEHOLDER_PRODUCTS)},
        )
    }
    assert products == {
        code: ("channel_connections", channel, 1, None, False)
        for code, channel in PLACEHOLDER_PRODUCTS.items()
    }

    # The subscriber stays on the version it bought (ENT-17), read through the
    # number alias with its WhatsApp slot: 3 + 1 typed, two numbers, fits.
    assert (
        _one(
            url,
            "SELECT v.version FROM subscriptions s JOIN plan_versions v"
            " ON v.id = s.plan_version_id WHERE s.id = :id",
            {"id": seed.subscription},
        )
        == 1
    )
    census = _all(
        url,
        f"SELECT base, general_extra, active, over_limit FROM ({CAPACITY_CENSUS_SQL}) c"  # noqa: S608
        " WHERE c.tenant_id = :t",
        {"t": seed.tenant, "now": datetime.now(UTC), "default_plan_code": "starter"},
    )[0]
    assert tuple(census) == (3, 0, 2, False)


def _assert_at_0091(url: str, seed: Seed) -> None:
    assert _one(url, "SELECT version_num FROM alembic_version") == "0091"
    _assert_keys_valid(url)
    assert _one(
        url, "SELECT entitlement_key::text FROM topup_products WHERE id = :id", {"id": seed.product}
    ) == ("whatsapp_numbers")
    restored = _all(
        url,
        "SELECT id, marketing_opt_out_at IS NOT NULL, opt_out_source::text,"
        " marketing_resumed_at IS NOT NULL FROM contacts WHERE tenant_id = :t",
        {"t": seed.tenant},
    )
    assert {row[0]: tuple(row[1:]) for row in restored} == {
        seed.opted_out: (True, "customer", False),
        seed.resumed: (False, None, True),
    }
    assert _one(url, "SELECT count(*) FROM plan_versions WHERE version > 1") == 0
    assert (
        _one(
            url,
            "SELECT count(*) FROM topup_products WHERE code = ANY(:c)",
            {"c": list(PLACEHOLDER_PRODUCTS)},
        )
        == 0
    )
    assert (
        _one(
            url,
            "SELECT count(*) FROM information_schema.columns"
            " WHERE table_name = 'plan_versions' AND column_name = 'allowed_channel_types'",
        )
        == 0
    )


def test_the_entitlement_migrations_carry_real_rows_forward_and_back(database_url: str) -> None:
    name = f"wasla_ent_mig_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    seed = Seed()
    try:
        _alembic(target, "upgrade", "0091")
        asyncio.run(_execute(target, seed.statements()))

        _alembic(target, "upgrade", "head")
        _assert_upgraded(target, seed)

        _alembic(target, "downgrade", "0091")
        _assert_at_0091(target, seed)

        _alembic(target, "upgrade", "head")
        _assert_upgraded(target, seed)

        # A change scheduled onto a placeholder version: removing it would lose it.
        asyncio.run(
            _execute(
                target,
                [
                    (
                        "UPDATE subscriptions SET scheduled_plan_version_id = v.id,"
                        " scheduled_plan_price_id = pp.id, scheduled_change_source = 'downgrade'"
                        " FROM plan_versions v JOIN plans p ON p.id = v.plan_id"
                        " JOIN plan_prices pp ON pp.plan_version_id = v.id"
                        " WHERE p.code = 'pro' AND v.version = 2 AND subscriptions.id = :id",
                        {"id": seed.subscription},
                    )
                ],
            )
        )
        with pytest.raises(RuntimeError, match="subscriptions on a placeholder version"):
            _alembic(target, "downgrade", "0097")
        assert _one(target, "SELECT version_num FROM alembic_version") == "0098"
        assert _one(
            target,
            "SELECT count(*) FROM topup_products WHERE code = ANY(:c)",
            {"c": list(PLACEHOLDER_PRODUCTS)},
        ) == len(PLACEHOLDER_PRODUCTS)
        assert _one(target, "SELECT count(*) FROM plan_versions WHERE version = 2") == 4
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


INDEXES_0094 = ("ix_agent_turns_held", "uq_usage_events_tenant_id_agent_turn_id")


def test_a_downgrade_refused_below_0094_rolls_back_the_whole_run(database_url: str) -> None:
    """0092 refuses a Telegram connection; 0098 to 0093 must not stay downgraded.

    0094's downgrade used to drop its indexes `CONCURRENTLY` in an autocommit
    block, which commits every step above it: the refusal then left the stamp at
    0094 with 0094's columns and without its indexes, and a following
    `upgrade head` never rebuilt them - the database at head without the
    unique index that keeps one `ai_turn` charge per turn (E05).
    """
    name = f"wasla_ent_mig_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    tenant = uuid.uuid4()
    try:
        _alembic(target, "upgrade", "head")
        asyncio.run(
            _execute(
                target,
                [
                    (
                        "INSERT INTO tenants (id, name, slug, status)"
                        " VALUES (:t, 'Bots', :slug, 'active')",
                        {"t": tenant, "slug": f"bots-{tenant.hex[:10]}"},
                    ),
                    (
                        "INSERT INTO channel_connections"
                        " (id, tenant_id, channel, external_account_id, status,"
                        " ownership_started_at)"
                        " VALUES (:id, :t, 'telegram', 'bot-1', 'active', now())",
                        {"id": uuid.uuid4(), "t": tenant},
                    ),
                ],
            )
        )

        with pytest.raises(RuntimeError, match="channel_connections: 1"):
            _alembic(target, "downgrade", "0091")

        assert _one(target, "SELECT version_num FROM alembic_version") == "0098"
        valid = _all(
            target,
            "SELECT c.relname, i.indisvalid FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY(:n)",
            {"n": list(INDEXES_0094)},
        )
        assert dict(valid) == dict.fromkeys(INDEXES_0094, True)
        assert _one(target, "SELECT to_regclass('channel_capacity_reductions') IS NOT NULL")
        assert _one(target, "SELECT to_regclass('contact_channel_consents') IS NOT NULL")
        assert _one(
            target,
            "SELECT count(*) FROM topup_products WHERE code = ANY(:c)",
            {"c": list(PLACEHOLDER_PRODUCTS)},
        ) == len(PLACEHOLDER_PRODUCTS)
        _assert_keys_valid(target)
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
