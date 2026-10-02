"""The status lookup is an index seek, on the schema the migrations build (OMNI-029).

Every delivery-status webhook resolves its message through
`OutboundMessageDirectory.find_by_provider_message_id`, and WhatsApp sends three
per outbound message. After the channel-neutral remediation that lookup named
only `connection_id IN (...)` and the provider id - columns no index leads with -
so on a seeded database of 200,000 messages it was a parallel sequential scan of
`messages` (4,999 buffers, 22.6 ms), growing with the whole platform's traffic.

This suite seeds a table large enough that the planner prefers a sequential
scan for the old predicate - and proves it does, which is what makes the main
assertion mean something - then explains **the statement the repository
issues**, compiled from the repository itself, and requires an index whose
leading columns the predicate serves. It runs in the migration-built lane in CI
(`WASLA_TEST_SCHEMA=migrations`), so the index it relies on is the migrations',
not only the models'.

Mutant this suite kills: M-O05 (the tenant predicate removed).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.channel import ChannelConnection
from app.db.models.conversation import Message, MessageDirection
from app.repositories.conversation_repository import OutboundMessageDirectory

pytestmark = pytest.mark.integration

WORKSPACES = 40
CONTACTS_PER_WORKSPACE = 50
MESSAGES_PER_CONVERSATION = 20
SERVING_INDEXES = frozenset(
    {
        "uq_messages_tenant_id_connection_id_wa_message_id",
        "uq_messages_tenant_id_wa_message_id",
    }
)


SEED = (
    "INSERT INTO tenants (id, name, slug, status) SELECT gen_random_uuid(), 'Plan ' || g,"
    " :prefix || g, 'active' FROM generate_series(1, :workspaces) g",
    "INSERT INTO whatsapp_accounts (id, tenant_id, phone_number_id, waba_id,"
    " display_phone_number, status) SELECT gen_random_uuid(), t.id, 'pn-' || t.slug,"
    " 'waba-' || t.slug, '+2010', 'active' FROM tenants t WHERE t.slug LIKE :prefix || '%'",
    "INSERT INTO contacts (id, tenant_id, wa_id) SELECT gen_random_uuid(), t.id,"
    " '2019' || lpad((row_number() OVER ())::text, 8, '0') FROM tenants t"
    " CROSS JOIN generate_series(1, :contacts) WHERE t.slug LIKE :prefix || '%'",
    "INSERT INTO conversations (id, tenant_id, contact_id, account_id, status, mode)"
    " SELECT gen_random_uuid(), c.tenant_id, c.id, a.id, 'open', 'ai' FROM contacts c"
    " JOIN whatsapp_accounts a ON a.tenant_id = c.tenant_id"
    " JOIN tenants t ON t.id = c.tenant_id WHERE t.slug LIKE :prefix || '%'",
    "INSERT INTO messages (id, tenant_id, conversation_id, direction, kind, status, origin,"
    " wa_message_id, sent_at) SELECT gen_random_uuid(), v.tenant_id, v.id,"
    " (CASE WHEN g % 2 = 0 THEN 'outbound' ELSE 'inbound' END)::message_direction, 'text',"
    " (CASE WHEN g % 2 = 0 THEN 'sent' ELSE 'received' END)::message_status,"
    " (CASE WHEN g % 2 = 0 THEN 'agent' ELSE 'customer' END)::message_origin,"
    " 'wamid.' || replace(gen_random_uuid()::text, '-', ''), now()"
    " FROM conversations v JOIN tenants t ON t.id = v.tenant_id"
    " CROSS JOIN generate_series(1, :messages) g WHERE t.slug LIKE :prefix || '%'",
    "ANALYZE messages",
)


async def _seed(session: AsyncSession) -> None:
    """40 workspaces, 2,000 conversations, 40,000 messages - through the real triggers."""
    values = {
        "prefix": f"plan-{uuid.uuid4().hex[:8]}-",
        "workspaces": WORKSPACES,
        "contacts": CONTACTS_PER_WORKSPACE,
        "messages": MESSAGES_PER_CONVERSATION,
    }
    for statement in SEED:
        clause = text(statement)
        used = {name: value for name, value in values.items() if f":{name}" in statement}
        await session.execute(clause, used)


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", ()):
        yield from _nodes(child)


async def _plan(session: AsyncSession, statement: Select[Any]) -> list[dict[str, Any]]:
    compiled = statement.compile(
        dialect=session.get_bind().dialect, compile_kwargs={"literal_binds": True}
    )
    rows = await session.execute(text(f"EXPLAIN (FORMAT JSON) {compiled}"))
    document = rows.scalar_one()
    parsed = json.loads(document) if isinstance(document, str) else document
    return list(_nodes(parsed[0]["Plan"]))


def _scans_messages_sequentially(nodes: list[dict[str, Any]]) -> bool:
    return any(
        node["Node Type"] in ("Seq Scan", "Parallel Seq Scan")
        and node.get("Relation Name") == "messages"
        for node in nodes
    )


async def _sample(session: AsyncSession) -> tuple[Message, ChannelConnection]:
    message = await session.scalar(
        select(Message)
        .where(Message.direction == MessageDirection.OUTBOUND)
        .order_by(Message.id)
        .offset(12_345)
        .limit(1)
    )
    assert message is not None
    connection = await session.get(ChannelConnection, message.connection_id)
    assert connection is not None
    return message, connection


async def test_the_status_lookup_as_issued_is_an_index_seek(db_session: AsyncSession) -> None:
    await _seed(db_session)
    message, connection = await _sample(db_session)

    # Non-vacuity: on this table the old predicate - the connection alone - is
    # planned as a sequential scan. Without that, "no Seq Scan" below proves
    # nothing about the index.
    old = select(Message).where(
        Message.connection_id.in_([connection.id]),
        Message.wa_message_id == message.wa_message_id,
        Message.direction == MessageDirection.OUTBOUND,
    )
    assert _scans_messages_sequentially(await _plan(db_session, old.limit(1)))

    issued = OutboundMessageDirectory.provider_message_lookup(
        str(message.wa_message_id), holders=[connection]
    ).limit(1)
    nodes = await _plan(db_session, issued)

    assert not _scans_messages_sequentially(nodes), nodes
    served = [node for node in nodes if node.get("Index Name") in SERVING_INDEXES]
    assert served, f"no node uses an index the predicate serves: {nodes}"
    assert any("tenant_id" in node.get("Index Cond", "") for node in served)


async def test_an_id_that_is_not_ours_is_an_index_seek_too(db_session: AsyncSession) -> None:
    """A status for a message sent outside Wasla - ordinary traffic - must be cheap as well."""
    await _seed(db_session)
    _, connection = await _sample(db_session)

    nodes = await _plan(
        db_session,
        OutboundMessageDirectory.provider_message_lookup(
            "wamid.sent-from-elsewhere", holders=[connection]
        ).limit(1),
    )

    assert not _scans_messages_sequentially(nodes), nodes


async def test_the_lookup_still_finds_the_message_across_a_handover(
    db_session: AsyncSession,
) -> None:
    """MSG-04 through the new predicate: two claims, two workspaces, the right message."""
    await _seed(db_session)
    message, connection = await _sample(db_session)
    other_claim = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=(await _sample_other_workspace(db_session, connection.tenant_id)),
        channel=connection.channel,
        external_account_id=connection.external_account_id,
        status=connection.status,
        ownership_started_at=connection.ownership_started_at,
    )

    found = await OutboundMessageDirectory(db_session).find_by_provider_message_id(
        str(message.wa_message_id), holders=[other_claim, connection]
    )
    missed = await OutboundMessageDirectory(db_session).find_by_provider_message_id(
        str(message.wa_message_id), holders=[other_claim]
    )

    assert found is not None and found.id == message.id
    assert missed is None


async def _sample_other_workspace(session: AsyncSession, not_this: uuid.UUID) -> uuid.UUID:
    other = await session.scalar(
        select(ChannelConnection.tenant_id).where(ChannelConnection.tenant_id != not_this).limit(1)
    )
    assert other is not None
    return other
