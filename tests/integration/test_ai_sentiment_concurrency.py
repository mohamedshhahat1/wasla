"""Two turns may judge one message at once, and neither may lose its customer (AI-04).

The race was ordinary WhatsApp traffic: a customer sends two messages, two
workers pick up two turns, and both classify the newest message on the
conversation. `SentimentRepository.record` read and then inserted, so both read
"no reading" and the second insert raised `UniqueViolation` after its turn had
engaged the provider. The job dead-lettered, the turn stayed `ENGAGED` for ever,
and one of the two messages was never answered.

Every race here runs on independent connections, and every test proves the
interleaving it depends on actually happened - a second insert genuinely
waiting, both classifications genuinely reaching the provider - because a
concurrency test that passes when the schedule was sequential proves nothing.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models.sentiment import MessageSentiment, SentimentLabel
from app.repositories.sentiment_repository import SentimentRepository
from tests.integration.ai_harness import (
    FakeProviders,
    TurnRunner,
    Workspace,
    wait_for_lock_waiter,
)

pytestmark = pytest.mark.integration


async def _record(
    session: AsyncSession,
    workspace: Workspace,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
) -> tuple[MessageSentiment, bool]:
    return await SentimentRepository(session, tenant_id=workspace.tenant_id).record(
        message_id=message_id,
        conversation_id=conversation_id,
        label=SentimentLabel.ANGRY,
        score=-0.9,
        intent="complaint",
        confidence=0.9,
        escalated=False,
        model="gpt-4.1-mini",
    )


async def _readings(runner: TurnRunner, conversation_id: uuid.UUID) -> int:
    async with runner.database.session() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(MessageSentiment)
                .where(MessageSentiment.conversation_id == conversation_id)
            )
            or 0
        )


async def test_two_connections_recording_one_message_both_converge(
    ai_turns: TurnRunner,
) -> None:
    """The repository race, isolated.

    The first connection writes and holds its transaction open. The second
    writes the same message and must *wait* - asserted, not assumed - and then
    be handed the first one's row rather than an exception.
    """
    workspace = await ai_turns.workspace()
    conversation_id, (message_id,) = await ai_turns.write(workspace, ["this is unacceptable"])
    engine = create_async_engine(ai_turns.url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def second_writer() -> tuple[MessageSentiment, bool]:
        async with factory() as second:
            result = await _record(second, workspace, conversation_id, message_id)
            await second.flush()
            await second.commit()
            return result

    try:
        async with factory() as first:
            winner, won = await _record(first, workspace, conversation_id, message_id)
            await first.flush()
            racing = asyncio.create_task(second_writer())
            # Proven, not assumed: the second insert is blocked on the first
            # one's uncommitted row before the first commits.
            await wait_for_lock_waiter(engine)
            assert not racing.done(), "the second write must be waiting on the first"
            await first.commit()
        loser, lost = await asyncio.wait_for(racing, 10)
    finally:
        await engine.dispose()

    assert won is True
    assert lost is False
    assert loser.id == winner.id
    assert await _readings(ai_turns, conversation_id) == 1


async def test_two_turns_racing_one_conversation_both_finish(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """The worker-level shape the audit reproduced, now converging.

    Both turns load a history ending in the same message, so both classify it,
    held at the classifier until both have arrived. One reading is stored; both
    turns complete; neither job dead-letters.
    """
    workspace = await ai_turns.workspace()
    conversation_id, first = await ai_turns.write(workspace, ["MSG_A"])
    _, second = await ai_turns.write(workspace, ["MSG_B"])
    barrier = asyncio.Barrier(2)

    async def both_at_the_classifier() -> None:
        await asyncio.wait_for(barrier.wait(), 10)

    ai_providers.sentiment_gate = both_at_the_classifier
    await ai_turns.enqueue(workspace, conversation_id, first[0])
    await ai_turns.enqueue(workspace, conversation_id, second[0])

    assert await ai_turns.race(2) == 2

    assert ai_providers.sentiment == 2, "both turns genuinely classified at once"
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed", "completed"]
    assert await _readings(ai_turns, conversation_id) == 1
    # Both customer messages received their turn. Two replies to one burst is
    # the separate, product-level AI-08; what is pinned here is that neither
    # message was lost.
    assert ai_providers.inference == 2
    assert len(ai_providers.sends) == 2


async def test_a_reading_that_cannot_be_stored_costs_the_customer_nothing(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real database error while storing the reading, contained (PD-2).

    `SELECT 1/0` fails inside PostgreSQL and aborts the transaction it runs in -
    which is the point: an exception raised in Python without touching the
    connection would not prove the turn's session survives.
    """

    async def broken(self: SentimentRepository, **_fields: object) -> tuple[object, bool]:
        await self.session.execute(text("SELECT 1 / 0"))
        raise AssertionError("PostgreSQL should have refused the statement above")

    monkeypatch.setattr(SentimentRepository, "record", broken)
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.sentiment == 1, "the classification was reached"
    assert len(ai_providers.sends) == 1
    assert await ai_turns.turn_states(workspace.tenant_id) == ["completed"]
    assert await _readings(ai_turns, conversation_id) == 0
    usage = await ai_turns.usage(workspace.tenant_id)
    assert usage["ai_request"] == 2, "the classification is still paid for"


async def test_the_reading_is_taken_from_the_newest_message_the_turn_answers(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(
        workspace, ["a calm question", "WHY IS NOBODY ANSWERING"]
    )

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.sentiment_requests[0]["input"] == [
        {"role": "user", "content": "WHY IS NOBODY ANSWERING"}
    ]
