"""Every trigger function resolves names in one fixed schema (DB-024).

A function that guards money should not depend on the caller's `search_path`.
Every function this schema defines - found in the catalog, not listed here, so
the next one is covered without anybody remembering to add it - must pin
`public, pg_catalog` and run as its caller (`SECURITY INVOKER`). Functions an
extension installed are the extension's and are left out.

Also here, because it is the other half of migration 0080: `conversations`
keeps ten percent of each page free for the sequence trigger's HOT update
(DB-020).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models.conversation import CONVERSATIONS_FILLFACTOR

pytestmark = pytest.mark.integration

PINNED = "search_path=public, pg_catalog"

OWN_FUNCTIONS = """
    SELECT p.proname, coalesce(p.proconfig, '{}'), p.prosecdef
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = current_schema()
       AND NOT EXISTS (
           SELECT 1 FROM pg_depend d
            WHERE d.objid = p.oid AND d.classid = 'pg_proc'::regclass AND d.deptype = 'e')
     ORDER BY p.proname
"""


async def test_every_function_pins_its_search_path(db_connection: AsyncConnection) -> None:
    rows = (await db_connection.execute(text(OWN_FUNCTIONS))).all()
    # Non-vacuous: the schema has the message sequence function and the ledger's.
    names = {name for name, _, _ in rows}
    assert {"wasla_assign_message_sequence", "invoices_refuse_history_rewrite"} <= names
    unpinned = sorted(name for name, config, _ in rows if PINNED not in config)
    assert not unpinned, f"functions resolving names through the caller's search_path: {unpinned}"


async def test_no_function_runs_with_its_owners_rights(db_connection: AsyncConnection) -> None:
    rows = (await db_connection.execute(text(OWN_FUNCTIONS))).all()
    definers = sorted(name for name, _, definer in rows if definer)
    assert not definers, f"SECURITY DEFINER functions: {definers}"


async def test_conversations_leave_room_for_hot_updates(db_connection: AsyncConnection) -> None:
    options = await db_connection.scalar(
        text("SELECT reloptions FROM pg_class WHERE relname = 'conversations'")
    )
    assert f"fillfactor={CONVERSATIONS_FILLFACTOR}" in (options or [])
