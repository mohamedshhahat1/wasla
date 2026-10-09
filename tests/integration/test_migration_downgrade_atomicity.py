"""A refused downgrade changes nothing, wherever below head it is refused (MIG-0091).

Alembic runs a whole `downgrade` in one transaction, and every refusal is a
`RuntimeError` raised before its migration changes anything. That only rolls
the run back if no step above the refusal committed on its own. A
`DROP INDEX CONCURRENTLY` inside `autocommit_block()` does exactly that: it
commits every step above it, then drops the index outside any transaction. A
refusal further down then left the database **stamped at the migration whose
block ran last, without that migration's indexes**, and `alembic upgrade head`
never rebuilt them, because the stamp said they were there.

Measured on the unfixed migrations:

- 0090 refusing (a connection with its own sending allowance) left the stamp at
  0091 without `ix_conversations_tenant_id_channel_last_message_at`;
- 0069 refusing (a stored card) left the stamp at 0075 without 0075's four
  purge indexes, and 0081's, 0079's and 0078's gone with the run - among them
  `uq_users_email_lower`, the unique index behind one account per address.

The downgrades of 0075, 0078, 0079, 0081 and 0091 now drop their indexes in
the run's transaction, as 0094's has since 9695ec0. Each test downgrades
through alembic's command API against a database `alembic upgrade head` built,
asserts the refusal's own text, and then reads the stamp and `pg_index`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.engine import make_url

from tests.integration.test_entitlement_migrations import (
    HEAD,
    INDEXES_0094,
    PLACEHOLDER_PRODUCTS,
    _admin,
    _alembic,
    _all,
    _execute,
    _one,
)

pytestmark = pytest.mark.integration

INDEX_0091 = "ix_conversations_tenant_id_channel_last_message_at"
INDEXES_0081 = (
    "ix_subscriptions_usage_period_end",
    "ix_subscriptions_plan_price_id",
    "ix_subscriptions_scheduled_plan_price_id",
    "ix_invoices_plan_price_id",
    "ix_custom_plan_offers_plan_price_id",
)
INDEXES_0079 = ("ix_whatsapp_events_redactable",)
INDEXES_0078 = ("uq_payment_methods_one_active_default", "uq_users_email_lower")
INDEXES_0075 = (
    "ix_campaign_recipients_message_id",
    "ix_campaign_recipients_conversation_id",
    "ix_follow_ups_message_id",
    "ix_agent_turns_conversation_id",
)


@pytest.fixture
def fresh(database_url: str) -> Iterator[str]:
    """A database of its own, built by `alembic upgrade head` from empty."""
    name = f"wasla_mig_atomic_{uuid.uuid4().hex[:12]}"
    source = make_url(database_url)
    target = source.set(database=name).render_as_string(hide_password=False)
    admin = source.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f"CREATE DATABASE {name}"))
    try:
        _alembic(target, "upgrade", "head")
        assert _one(target, "SELECT version_num FROM alembic_version") == HEAD
        yield target
    finally:
        asyncio.run(_admin(admin, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


def _validity(url: str, names: tuple[str, ...]) -> dict[str, bool]:
    rows = _all(
        url,
        "SELECT c.relname, i.indisvalid AND i.indisready FROM pg_index i"
        " JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY(:n)",
        {"n": list(names)},
    )
    return {row[0]: row[1] for row in rows}


def _assert_present(url: str, names: tuple[str, ...]) -> None:
    assert _validity(url, names) == dict.fromkeys(names, True)


INDEX_0100 = "ix_audit_logs_target_type_target_id_occurred_at"
COLUMNS_0099 = (
    ("topup_purchases", "withdrawn_at"),
    ("topup_purchases", "withdrawn_by"),
    ("topup_purchases", "withdrawal_reason"),
    ("channel_capacity_reductions", "topup_purchase_id"),
)


def _columns_0099(url: str) -> int:
    return int(
        _one(
            url,
            "SELECT count(*) FROM information_schema.columns"  # noqa: S608 - module constants
            " WHERE (table_name, column_name) IN ("
            + ", ".join(f"('{table}', '{column}')" for table, column in COLUMNS_0099)
            + ")",
        )
    )


def _assert_whole_head(url: str) -> None:
    """Stamped at head, and every object 0075..0100 added still there and valid."""
    assert _one(url, "SELECT version_num FROM alembic_version") == HEAD
    for names in (INDEXES_0075, INDEXES_0078, INDEXES_0079, INDEXES_0081, (INDEX_0091,)):
        _assert_present(url, names)
    _assert_present(url, INDEXES_0094)
    _assert_present(url, (INDEX_0100,))
    assert _columns_0099(url) == len(COLUMNS_0099)
    assert _one(url, "SELECT to_regclass('channel_capacity_reductions') IS NOT NULL")
    assert _one(url, "SELECT to_regclass('contact_channel_consents') IS NOT NULL")
    assert _one(
        url,
        "SELECT count(*) FROM topup_products WHERE code = ANY(:c)",
        {"c": list(PLACEHOLDER_PRODUCTS)},
    ) == len(PLACEHOLDER_PRODUCTS)
    assert _one(url, "SELECT count(*) FROM pg_index WHERE NOT indisvalid") == 0


def _tenant(tenant: uuid.UUID) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO tenants (id, name, slug, status) VALUES (:t, 'Atomic', :slug, 'active')",
        {"t": tenant, "slug": f"atomic-{tenant.hex[:12]}"},
    )


def test_a_downgrade_refused_below_0091_rolls_back_the_whole_run(fresh: str) -> None:
    """0090 refuses a connection with its own sending allowance; head stays head."""
    tenant = uuid.uuid4()
    asyncio.run(
        _execute(
            fresh,
            [
                _tenant(tenant),
                (
                    "INSERT INTO channel_connections (id, tenant_id, channel,"
                    " external_account_id, status, ownership_started_at, sends_per_minute)"
                    " VALUES (:id, :t, 'whatsapp', :ext, 'active', now(), 10)",
                    {"id": uuid.uuid4(), "t": tenant, "ext": f"atomic-{tenant.hex[:8]}"},
                ),
            ],
        )
    )
    _assert_present(fresh, (INDEX_0091,))

    with pytest.raises(RuntimeError, match="carry their own sending allowance"):
        _alembic(fresh, "downgrade", "0089")

    _assert_whole_head(fresh)


def test_a_downgrade_refused_at_0081_also_rolls_back(fresh: str) -> None:
    """0081 refuses a subscriber on a price its version never published."""
    tenant, subscription = uuid.uuid4(), uuid.uuid4()
    pro_v1 = (
        "SELECT v.id FROM plan_versions v JOIN plans p ON p.id = v.plan_id"
        " WHERE p.code = 'pro' AND v.version = 1"
    )
    asyncio.run(
        _execute(
            fresh,
            [
                _tenant(tenant),
                (
                    "INSERT INTO plan_prices (id, plan_version_id, billing_interval,"  # noqa: S608
                    " interval_count, amount, currency, created_at) VALUES"
                    f" (gen_random_uuid(), ({pro_v1}), 'yearly', 1, 990, 'EGP', now())",
                    {},
                ),
                (
                    "INSERT INTO subscriptions (id, tenant_id, plan_id, plan_version_id,"  # noqa: S608
                    " plan_price_id, status, current_period_start, current_period_end,"
                    " cancel_at_period_end, usage_period_start, usage_period_end,"
                    " billing_anchor_at) VALUES (:id, :t, (SELECT id FROM plans WHERE"
                    f" code = 'pro'), ({pro_v1}), (SELECT id FROM plan_prices WHERE"
                    f" plan_version_id = ({pro_v1}) AND billing_interval = 'yearly'),"
                    " 'active', now(), now() + interval '1 year', false, now(),"
                    " now() + interval '1 month', now())",
                    {"id": subscription, "t": tenant},
                ),
            ],
        )
    )

    with pytest.raises(RuntimeError, match="re-price"):
        _alembic(fresh, "downgrade", "0080")

    _assert_whole_head(fresh)


def test_a_downgrade_refused_at_0069_rolls_back_through_every_index_drop(fresh: str) -> None:
    """0069 refuses a stored card; 0075, 0078, 0079, 0081 and 0091 all lie above it."""
    tenant = uuid.uuid4()
    asyncio.run(
        _execute(
            fresh,
            [
                _tenant(tenant),
                (
                    "INSERT INTO payment_methods (id, tenant_id, provider, provider_token,"
                    " status, is_default, token_fingerprint) VALUES (gen_random_uuid(), :t,"
                    " 'paymob', 'sealed', 'active', false, :fp)",
                    {"t": tenant, "fp": tenant.hex},
                ),
            ],
        )
    )

    with pytest.raises(RuntimeError, match="protected payment tokens"):
        _alembic(fresh, "downgrade", "0068")

    _assert_whole_head(fresh)


def test_a_successful_downgrade_through_0091_and_back_restores_the_index(fresh: str) -> None:
    _alembic(fresh, "downgrade", "0090")
    assert _one(fresh, "SELECT version_num FROM alembic_version") == "0090"
    assert _validity(fresh, (INDEX_0091,)) == {}

    _alembic(fresh, "upgrade", "head")
    _assert_whole_head(fresh)


def test_0099_refuses_while_a_grant_is_withdrawn_and_the_round_trip_is_lossless(
    fresh: str,
) -> None:
    """0100 and 0099 come off in one transaction, and 0099 refuses what it cannot explain."""
    tenant, grant = uuid.uuid4(), uuid.uuid4()
    asyncio.run(
        _execute(
            fresh,
            [
                _tenant(tenant),
                (
                    "INSERT INTO topup_purchases (id, tenant_id, source, product_name,"
                    " entitlement_key, quantity, unit_price, total_amount, currency,"
                    " billing_period_start, billing_period_end, expires_at, status, granted_at,"
                    " reason, withdrawn_at, withdrawal_reason, ended_at) VALUES (:id, :t,"
                    " 'platform_grant', 'Platform grant', 'channel_connections', 1, 0, 0, 'EGP',"
                    " now() - interval '2 days', now() + interval '28 days',"
                    " now() + interval '28 days', 'withdrawn', now() - interval '2 days',"
                    " 'Goodwill.', now(), 'Granted in error.', now())",
                    {"id": grant, "t": tenant},
                ),
            ],
        )
    )

    with pytest.raises(RuntimeError, match="topup_purchases withdrawn by staff: 1"):
        _alembic(fresh, "downgrade", "0098")
    # 0100's index drop was in the same transaction: it is back too.
    _assert_whole_head(fresh)

    asyncio.run(_execute(fresh, [("DELETE FROM topup_purchases WHERE id = :id", {"id": grant})]))
    _alembic(fresh, "downgrade", "0098")
    assert _one(fresh, "SELECT version_num FROM alembic_version") == "0098"
    assert _columns_0099(fresh) == 0
    assert _validity(fresh, (INDEX_0100,)) == {}
    # The labels stay: PostgreSQL cannot drop one.
    assert _one(
        fresh,
        "SELECT count(*) FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid"
        " WHERE t.typname = 'topup_status' AND e.enumlabel = 'withdrawn'",
    )

    _alembic(fresh, "upgrade", "head")
    _assert_whole_head(fresh)
