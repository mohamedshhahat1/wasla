"""Every enum's labels sit in the same order in every schema (DB-015).

`test_schema_parity.py` compares label *sets*. The order mattered too: a model-
built schema created `subscription_status` and `audit_action` in their classes'
order, while production holds them in the order the migrations appended them,
so `ORDER BY status` sorted one way in the tests and another in production.

The declared order is the class order, or `DATABASE_LABEL_ORDER` for a type
whose production order is its history. This runs in both lanes: against a
migration-built schema it proves the declaration matches what the migrations
produce; against a model-built one, that `create_all` honours it.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models import Base
from app.db.models.billing import SubscriptionStatus
from app.db.models.enums import DATABASE_LABEL_ORDER

pytestmark = pytest.mark.integration


def _declared_orders() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            column_type = column.type
            if isinstance(column_type, sa.Enum) and column_type.name:
                name = column_type.name
                found[name] = list(DATABASE_LABEL_ORDER.get(name, column_type.enums))
    return found


async def _database_orders(connection: AsyncConnection) -> dict[str, list[str]]:
    rows = await connection.execute(sa.text("""
            SELECT t.typname, e.enumlabel
            FROM pg_enum e
            JOIN pg_type t ON t.oid = e.enumtypid
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = current_schema()
            ORDER BY t.typname, e.enumsortorder
            """))
    found: dict[str, list[str]] = {}
    for type_name, label in rows:
        found.setdefault(type_name, []).append(label)
    return found


async def test_every_enum_holds_its_labels_in_the_declared_order(
    db_connection: AsyncConnection,
) -> None:
    declared = _declared_orders()
    database = await _database_orders(db_connection)
    differences = {
        name: {"declared": declared.get(name), "database": database.get(name)}
        for name in sorted(set(declared) | set(database))
        if declared.get(name) != database.get(name)
    }
    assert not differences, f"enum label order differs from the declaration: {differences}"


async def test_the_two_drifted_types_keep_their_production_order(
    db_connection: AsyncConnection,
) -> None:
    """Named, so a regression reads as the audit finding it reintroduces."""
    database = await _database_orders(db_connection)
    assert database["subscription_status"] == [
        "trialing",
        "active",
        "past_due",
        "cancelled",
        "expired",
        "suspended",
    ]
    assert database["audit_action"][:3] == [
        "member_invited",
        "invitation_revoked",
        "invitation_accepted",
    ]
    # Appended by 0037 after the account and billing labels before it.
    assert database["audit_action"].index("subscription_suspended") > database[
        "audit_action"
    ].index("subscription_past_due")


def test_a_database_order_must_name_every_label_once() -> None:
    from app.db.models.enums import _enum_type

    with pytest.raises(ValueError):
        _enum_type(
            SubscriptionStatus,
            name="subscription_status_bad",
            database_order=("trialing", "active"),
        )
