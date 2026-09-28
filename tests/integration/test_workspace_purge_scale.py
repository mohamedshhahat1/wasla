"""A workspace purge costs what the workspace holds, not what the platform holds.

DB-002: purging one workspace's 10,000 messages took 6.4 s, 5.3 s of it in one
foreign-key action. Every deleted message fired `UPDATE campaign_recipients SET
message_id = NULL WHERE message_id = $1`, and with no index on the column each
of those was a scan of every workspace's recipients - messages x platform-wide
recipients, inside one transaction.

The fix is an index behind each action (migration 0075) and deleting the
children first. What is proved here is the property rather than a timing: the
plan PostgreSQL uses for each action's lookup - a generic plan, as the RI
trigger's is - reads an index, and any cascading or SET NULL key into
`messages` or `conversations` has one. A timing would pass on a fast machine
with the index missing; a plan cannot.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.workspace_purge_service import PURGED_TABLES

pytestmark = pytest.mark.integration

# The lookups the purge's deletes fire, as the RI triggers phrase them.
LOOKUPS = (
    (
        "UPDATE ONLY campaign_recipients SET message_id = NULL WHERE message_id = $1",
        "ix_campaign_recipients_message_id",
    ),
    (
        "UPDATE ONLY campaign_recipients SET conversation_id = NULL WHERE conversation_id = $1",
        "ix_campaign_recipients_conversation_id",
    ),
    (
        "UPDATE ONLY follow_ups SET message_id = NULL WHERE message_id = $1",
        "ix_follow_ups_message_id",
    ),
    # The cascade's lookup also names `tenant_id`, which the tenant index can
    # serve on an empty table as cheaply as anything; the selective part - one
    # conversation out of a workspace's whole history - is what needs this.
    ("DELETE FROM ONLY agent_turns WHERE conversation_id = $1", "ix_agent_turns_conversation_id"),
)


@pytest.mark.parametrize(("statement", "index"), LOOKUPS, ids=[i for _, i in LOOKUPS])
async def test_each_purge_foreign_key_action_reads_an_index(
    db_session: AsyncSession, statement: str, index: str
) -> None:
    """A generic plan, the one an RI trigger caches, uses the named index.

    `enable_seqscan` off makes the answer independent of table size: on the
    test's near-empty tables a scan would be cheaper, and the question is
    whether an index able to serve the lookup exists at all.
    """
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    await db_session.execute(text(f"PREPARE purge_lookup(uuid) AS {statement}"))
    try:
        plan = "\n".join(
            row[0]
            for row in await db_session.execute(
                text("EXPLAIN EXECUTE purge_lookup('00000000-0000-0000-0000-000000000001')")
            )
        )
    finally:
        await db_session.execute(text("DEALLOCATE purge_lookup"))
    assert index in plan, plan
    assert "Seq Scan" not in plan, plan


async def test_every_cascading_key_into_messages_or_conversations_is_indexed(
    db_session: AsyncSession,
) -> None:
    """The general rule, so the next table with such a key cannot ship without one.

    For each foreign key that references `messages` or `conversations` and
    acts on delete (CASCADE or SET NULL), some valid index on the child table
    must lead with the key's own column - `tenant_id` aside, which every
    composite key repeats and no lookup is selective on.
    """
    rows = await db_session.execute(text("""
        SELECT c.conname, c.conrelid::regclass::text,
               ARRAY(SELECT a.attname FROM unnest(c.conkey) k
                     JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k
                     WHERE a.attname <> 'tenant_id') AS columns,
               EXISTS (
                   SELECT 1 FROM pg_index x
                    WHERE x.indrelid = c.conrelid AND x.indisvalid
                      AND (SELECT a.attname FROM pg_attribute a
                            WHERE a.attrelid = x.indrelid AND a.attnum = x.indkey[0])
                          = ANY(ARRAY(SELECT a.attname FROM unnest(c.conkey) k
                                      JOIN pg_attribute a ON a.attrelid = c.conrelid
                                       AND a.attnum = k WHERE a.attname <> 'tenant_id'))
               ) AS indexed
          FROM pg_constraint c
         WHERE c.contype = 'f'
           AND c.confrelid IN ('messages'::regclass, 'conversations'::regclass)
           AND c.confdeltype IN ('c', 'n')
        """))
    unindexed = [f"{row[1]}.{row[2]} ({row[0]})" for row in rows if not row[3]]
    assert unindexed == []


def test_the_purge_deletes_what_names_a_message_before_the_messages() -> None:
    """Children first: their SET NULL actions then find nothing to update."""
    order = list(PURGED_TABLES)
    for child in ("campaign_recipients", "follow_ups"):
        assert order.index(child) < order.index("messages") < order.index("conversations")
