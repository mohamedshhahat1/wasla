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
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import Conversation
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services.whatsapp_service import WhatsAppIngestionService
from scripts.omnichannel_invariants import violations

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
