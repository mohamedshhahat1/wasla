"""A conversation reaches the model in the order the customer wrote it (AI-01).

The failure this pins was invisible in every log. `created_at` is PostgreSQL's
`now()` - the start of the transaction - and one webhook delivery writes all of
its messages in one transaction, so a burst the customer typed carried a single
instant and was sorted by a random UUID. The model read `FOURTH, FIFTH, SECOND,
THIRD, FIRST`; with thirty messages against a twenty-message window, the item it
treated as "what the customer just said" was `M26`.

Every test here writes through the production path - the projection service,
one transaction, one Meta timestamp - and asserts on the serialized provider
request, not on a repository return value, because the request is what the
model actually read.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.agents.memory import build_window
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.repositories.conversation_repository import MessageRepository
from app.services.inbox_service import InboxService
from tests.integration.ai_harness import FakeProviders, TurnRunner

pytestmark = pytest.mark.integration

BATCH = ["FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH"]


async def _instants(runner: TurnRunner, message_ids: list[uuid.UUID]) -> set[datetime]:
    async with runner.database.session() as session:
        rows = await session.scalars(select(Message.created_at).where(Message.id.in_(message_ids)))
        return set(rows)


async def _positions(runner: TurnRunner, conversation_id: uuid.UUID) -> list[int]:
    async with runner.database.session() as session:
        rows = await session.scalars(
            select(Message.sequence)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sequence)
        )
        return list(rows)


async def test_a_webhook_batch_reaches_the_model_in_the_order_it_was_sent(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, BATCH)

    # Non-vacuity: the batch genuinely shares one instant, which is the shape
    # that scrambled it. Distinct timestamps would make this test prove nothing.
    assert len(await _instants(ai_turns, ids)) == 1

    assert await ai_turns.answer(workspace, conversation_id, ids[0]) == 1
    assert ai_providers.inference == 1
    assert ai_providers.inputs() == BATCH


async def test_thirty_messages_against_a_twenty_message_window_end_with_the_newest(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The last item is the one the model reads as the customer's latest words."""
    workspace = await ai_turns.workspace(memory_message_limit=20)
    texts = [f"M{index:02d}" for index in range(30)]
    conversation_id, ids = await ai_turns.write(workspace, texts)
    assert len(await _instants(ai_turns, ids)) == 1

    await ai_turns.answer(workspace, conversation_id, ids[0])

    sent = ai_providers.inputs()
    assert sent == texts[10:], "the window must be the newest twenty, contiguous and in order"
    assert sent[-1] == "M29"


async def test_the_same_conversation_yields_the_same_window_every_time(
    ai_turns: TurnRunner,
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, _ = await ai_turns.write(workspace, BATCH)

    windows: list[list[str]] = []
    for _ in range(3):
        async with ai_turns.database.session() as session:
            history = await MessageRepository(
                session, tenant_id=workspace.tenant_id
            ).list_for_conversation(conversation_id=conversation_id, limit=40)
        windows.append(
            [turn.text for turn in build_window(history, message_limit=20, token_budget=4000).turns]
        )

    assert windows == [BATCH, BATCH, BATCH]


async def test_the_repository_and_the_window_agree_on_one_order(ai_turns: TurnRunner) -> None:
    """Newest-first from SQL, reversed, is exactly the window's order.

    Two orderings - one in SQL and a weaker one in Python - is how the defect
    got in: the query was deterministic and the re-sort threw it away.
    """
    workspace = await ai_turns.workspace()
    conversation_id, _ = await ai_turns.write(workspace, BATCH)

    async with ai_turns.database.session() as session:
        history = await MessageRepository(
            session, tenant_id=workspace.tenant_id
        ).list_for_conversation(conversation_id=conversation_id, limit=40)

    newest_first = [message.body for message in history]
    window = build_window(history, message_limit=20, token_budget=4000)
    assert list(reversed(newest_first)) == [turn.text for turn in window.turns] == BATCH


async def test_concurrent_writers_never_share_a_position(ai_turns: TurnRunner) -> None:
    """Eight transactions on eight connections, released together at one message.

    The allocation is an UPDATE on the conversation row, so the writers queue on
    that row's lock and each reads the counter the previous one committed. A
    `max()+1` read would let two of them compute the same number; the unique
    constraint would then refuse one of them, and this test would fail on the
    exception rather than on the assertion.
    """
    workspace = await ai_turns.workspace()
    conversation_id, _ = await ai_turns.write(workspace, ["opening"])
    writers = 8
    barrier = asyncio.Barrier(writers)
    engine = create_async_engine(ai_turns.url, poolclass=NullPool)

    async def write(index: int) -> None:
        async with engine.connect() as connection, connection.begin():
            await asyncio.wait_for(barrier.wait(), 10)
            await connection.execute(
                insert(Message).values(
                    id=uuid.uuid4(),
                    tenant_id=workspace.tenant_id,
                    conversation_id=conversation_id,
                    direction=MessageDirection.INBOUND,
                    kind=MessageKind.TEXT,
                    status=MessageStatus.RECEIVED,
                    body=f"writer {index}",
                    origin=MessageOrigin.CUSTOMER,
                )
            )
            # Held open a moment, so the others are genuinely waiting on it.
            await asyncio.sleep(0.05)

    try:
        await asyncio.gather(*(write(index) for index in range(writers)))
    finally:
        await engine.dispose()

    assert await _positions(ai_turns, conversation_id) == list(range(1, writers + 2))
    async with ai_turns.database.session() as session:
        counter = await session.scalar(
            select(Conversation.last_message_sequence).where(Conversation.id == conversation_id)
        )
    assert counter == writers + 1


async def test_two_conversations_keep_positions_of_their_own(ai_turns: TurnRunner) -> None:
    workspace = await ai_turns.workspace()
    first, _ = await ai_turns.write(workspace, ["A1", "A2", "A3"], wa_id="201555000001")
    second, _ = await ai_turns.write(workspace, ["B1"], wa_id="201555000002")

    assert await _positions(ai_turns, first) == [1, 2, 3]
    assert await _positions(ai_turns, second) == [1]


async def test_an_agent_reply_takes_the_next_position(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Outbound rows are ordered by the same mechanism as inbound ones.

    Fixing only the inbound path would leave a reply able to tie with the
    message it answers.
    """
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, BATCH)

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert len(ai_providers.sends) == 1
    outbound = await ai_turns.outbound(workspace.tenant_id)
    assert [message.sequence for message in outbound] == [len(BATCH) + 1]


async def test_paging_a_batch_visits_every_message_once_newest_first(
    ai_turns: TurnRunner,
) -> None:
    """The inbox pages on the same order, so a tied batch neither repeats nor skips."""
    workspace = await ai_turns.workspace()
    conversation_id, _ = await ai_turns.write(workspace, BATCH)

    seen: list[str | None] = []
    async with ai_turns.database.session() as session:
        inbox = InboxService(session=session, tenant_id=workspace.tenant_id)
        cursor: str | None = None
        while True:
            page = await inbox.list_messages(
                conversation_id=conversation_id, limit=2, cursor=cursor
            )
            seen.extend(message.body for message in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

    assert seen == list(reversed(BATCH))
