"""Each invariant the final remediation added counts a violation when one exists.

`python -m scripts.omnichannel_invariants verify` gained checks for what the final
audit's Q3-Q6 measured and the tool did not (OMNI-030, OMNI-032, OMNI-036,
OMNI-041, OMNI-043). An invariant that cannot fail proves nothing, so each one
here is shown non-vacuous: a violation no application path writes is injected
by hand, inside the test's rolled-back transaction, and exactly that invariant
counts one more.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import Conversation
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.whatsapp_service import WhatsAppIngestionService
from scripts.omnichannel_invariants import census, violations
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

pytestmark = pytest.mark.integration


async def _number(session: AsyncSession) -> WhatsAppAccount:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Invariants {tag}", slug=f"invariants-{tag}")
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"pn-{tag}",
        waba_id=f"waba-{tag}",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()
    return account


def _inbound(account: WhatsAppAccount, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": account.phone_number_id},
                            "contacts": [{"wa_id": "201000000808"}],
                            "messages": [
                                {
                                    "from": "201000000808",
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    **message,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _counts(session: AsyncSession) -> dict[str, int]:
    return await violations(await session.connection(), read_only=False)


async def _added(session: AsyncSession, before: dict[str, int]) -> dict[str, int]:
    after = await _counts(session)
    return {name: after[name] - before[name] for name in after if after[name] != before[name]}


async def _conversation(session: AsyncSession, account: WhatsAppAccount) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(Conversation.tenant_id == account.tenant_id)
    )
    assert conversation is not None
    return conversation


async def test_a_window_anchor_moved_back_is_counted(db_session: AsyncSession) -> None:
    """OMNI-036 (the audit's Q4): the anchor older than the newest inbound message."""
    account = await _number(db_session)
    await WhatsAppIngestionService(session=db_session).ingest(
        _inbound(account, {"type": "text", "text": {"body": "hello"}})
    )
    await db_session.flush()
    before = await _counts(db_session)
    conversation = await _conversation(db_session, account)

    await db_session.execute(
        text("UPDATE conversations SET last_inbound_at = :at WHERE id = :id"),
        {"at": datetime.now(UTC) - timedelta(days=3), "id": conversation.id},
    )

    assert await _added(db_session, before) == {"window_anchor_older_than_newest_inbound": 1}


async def _census(session: AsyncSession) -> dict[str, int]:
    return await census(await session.connection(), read_only=False)


async def test_a_processed_message_event_without_its_message_is_counted(
    db_session: AsyncSession,
) -> None:
    """OMNI-032 (the audit's Q3): what recovery and the media sweep rely on."""
    account = await _number(db_session)
    await WhatsAppIngestionService(session=db_session).ingest(
        _inbound(account, {"type": "text", "text": {"body": "hello"}})
    )
    await db_session.flush()
    before = await _counts(db_session)

    await db_session.execute(
        text(
            "UPDATE whatsapp_events SET event_id = 'evt.' || event_id, state = 'processed'"
            " WHERE tenant_id = :t"
        ),
        {"t": account.tenant_id},
    )

    assert await _added(db_session, before) == {"message_event_without_its_projected_message": 1}


async def test_collision_evidence_that_is_not_failed_is_counted(db_session: AsyncSession) -> None:
    """OMNI-043: evidence is never recovered or projected."""
    account = await _number(db_session)
    before = await _counts(db_session)
    census_before = await _census(db_session)

    await db_session.execute(
        text(
            "INSERT INTO whatsapp_events (id, tenant_id, account_id, channel, event_id, kind,"
            " state, payload, received_at) VALUES (gen_random_uuid(), :t, :a, 'whatsapp',"
            " :key, 'message', 'received', '{}', now())"
        ),
        {"t": account.tenant_id, "a": account.id, "key": f"collision:{account.id.hex}:wamid.x"},
    )

    assert await _added(db_session, before) == {"collision_evidence_not_failed": 1}
    census_after = await _census(db_session)
    assert (
        census_after["collision_evidence_events"] == census_before["collision_evidence_events"] + 1
    )


async def test_a_tap_stored_without_its_words_is_counted(db_session: AsyncSession) -> None:
    """OMNI-030: the title is the body; and Q5 counts the old parser's exposure."""
    account = await _number(db_session)
    await WhatsAppIngestionService(session=db_session).ingest(
        _inbound(
            account,
            {
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "book-yes", "title": "Yes, book it"},
                },
            },
        )
    )
    await db_session.flush()
    before = await _counts(db_session)
    census_before = await _census(db_session)

    await db_session.execute(
        text("UPDATE messages SET body = NULL WHERE tenant_id = :t"), {"t": account.tenant_id}
    )

    assert await _added(db_session, before) == {"inbound_tap_without_its_words": 1}
    after = await _census(db_session)
    assert after["q5_inbound_interactive_without_text"] == (
        census_before["q5_inbound_interactive_without_text"] + 1
    )


async def test_a_retained_stop_tap_without_an_opt_out_is_counted(db_session: AsyncSession) -> None:
    """OMNI-030 (the audit's Q6): the evidence `recover-button-opt-outs` replays."""
    account = await _number(db_session)
    await WhatsAppIngestionService(session=db_session).ingest(
        _inbound(
            account,
            {"type": "button", "button": {"text": "Stop promotions", "payload": "STOP"}},
        )
    )
    await db_session.flush()
    before = await _census(db_session)

    await db_session.execute(
        text(
            "UPDATE contact_channel_consents SET marketing_opt_out_at = NULL,"
            " opt_out_source = NULL, opt_out_via = NULL WHERE tenant_id = :t"
        ),
        {"t": account.tenant_id},
    )

    after = await _census(db_session)
    assert after["q6_retained_stop_taps_without_opt_out"] == (
        before["q6_retained_stop_taps_without_opt_out"] + 1
    )


async def test_an_undisclosed_ai_reply_on_a_disclosure_channel_is_counted(
    db_session: AsyncSession,
) -> None:
    """OMNI-041: Messenger and Instagram require the disclosure on every AI conversation."""
    tenant = Tenant(name="Disclosure oracle", slug=f"oracle-{uuid.uuid4().hex[:10]}")
    db_session.add(tenant)
    await db_session.flush()
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.MESSENGER,
        external_account_id=f"page-{uuid.uuid4().hex[:10]}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(connection)
    await db_session.flush()
    adapter = SyntheticAdapter(Channel.MESSENGER, tagged=True)
    await ChannelIngestionService(session=db_session, adapter=cast(ChannelAdapter, adapter)).ingest(
        adapter.parse(
            synthetic_payload(
                connection.external_account_id,
                {"type": "message", "id": "m.oracle", "from": "psid-oracle", "at": 1790000000},
            )
        )
    )
    await db_session.flush()
    conversation = await db_session.scalar(
        select(Conversation).where(Conversation.account_id == connection.id)
    )
    assert conversation is not None
    before = await _counts(db_session)

    await db_session.execute(
        text(
            "INSERT INTO messages (id, tenant_id, conversation_id, direction, kind, status,"
            " origin, delivery_state, body, sent_at) VALUES (gen_random_uuid(), :t, :c,"
            " 'outbound', 'text', 'sent', 'agent', 'sent', 'An automated reply', now())"
        ),
        {"t": tenant.id, "c": conversation.id},
    )

    assert await _added(db_session, before) == {
        "ai_reply_on_a_disclosure_channel_never_disclosed": 1
    }
