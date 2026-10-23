"""The AI writes each follow-up step from the conversation (FUS-04, FUS-07).

The model is faked at the OpenAI transport (`ScriptedModel`); Meta at its own.
A step is written at dispatch, with no tools, with no lock or transaction held
across the call; a reply, a takeover or a restart during the call discards the
text unsent. The AI may skip with a reason, which ends the run. A failing
model is tried once more, then the step is skipped. A step that called the
model is one AI turn, charged once; one cancelled before the call costs nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models.conversation import Conversation, ConversationMode
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.follow_up import FollowUpStatus
from app.db.models.membership import Membership
from app.db.models.usage import UsageEvent, UsageEventType
from app.db.models.user import User
from app.services.inbox_service import InboxService
from tests.integration.campaign_harness import USER_DOMAIN, FakeMeta, erase, worker
from tests.integration.sequence_harness import (
    STEP_PREFIX,
    World,
    ai_reply,
    build,
    conversations,
    customer_writes,
    make_due,
    pending,
    runs_of,
    step_sends,
    steps_of,
    sweeper,
    wa_id,
)

pytestmark = [pytest.mark.integration, pytest.mark.quiet_hours]


async def _world(prepared_database: str, name: str, **build_args: object) -> World:
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name=name)
    return await build(database, prepared_database, meta, **build_args)  # type: ignore[arg-type]


async def _charges(world: World) -> list[UsageEvent]:
    async with world.database.session_factory() as session:
        rows = await session.execute(
            select(UsageEvent).where(
                UsageEvent.tenant_id == world.workspace.tenant_id,
                UsageEvent.event_type == UsageEventType.AI_TURN,
            )
        )
        return list(rows.scalars())


async def _due_step(world: World, words: str = "How much is the oak cabinet?") -> Conversation:
    conversation = (await conversations(world))[0]
    await customer_writes(world, conversation, words)
    await ai_reply(world, conversation)
    await make_due(world, conversation)
    return conversation


# --------------------------------------------------------------------------- written


async def test_the_step_is_written_from_the_conversation_with_no_tools(
    prepared_database: str,
) -> None:
    world = await _world(prepared_database, "ai-written", steps=[5, 30])
    try:
        conversation = await _due_step(world)
        await sweeper(world).run_once()

        (call,) = world.model.calls
        assert "tools" not in call, "a follow-up turn offers the model no tools"
        assert call["text"]["format"]["name"] == "follow_up_step"
        assert "You sell kitchen fittings" in call["instructions"]
        assert "follow-up 1 of 2" in call["instructions"]
        assert "How much is the oak cabinet?" in world.model.last_customer_words(call)
        (sent,) = step_sends(world, await wa_id(world, conversation))
        assert sent["payload"]["type"] == "text"
        assert sent["payload"]["text"]["body"] == (f"{STEP_PREFIX} How much is the oak cabinet?")
        (charge,) = await _charges(world)
        assert charge.meta is not None and charge.agent_turn_id is None
        step = next(row for row in await steps_of(world, conversation) if row.step_index == 1)
        assert charge.meta["follow_up_id"] == str(step.id)
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_guidance_reaches_the_model(prepared_database: str) -> None:
    world = await _world(prepared_database, "ai-guidance", steps=[5])
    try:
        from sqlalchemy import update

        from app.db.models.follow_up_settings import FollowUpSettings

        async with world.database.session_factory() as session:
            await session.execute(
                update(FollowUpSettings)
                .where(FollowUpSettings.tenant_id == world.workspace.tenant_id)
                .values(steps=[{"delay_minutes": 5, "guidance": "Ask if they need help ordering"}])
            )
            await session.commit()
        await _due_step(world)
        await sweeper(world).run_once()
        assert "Ask if they need help ordering" in world.model.calls[0]["instructions"]
    finally:
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- races against the call


async def test_a_reply_while_the_model_writes_discards_the_text(prepared_database: str) -> None:
    world = await _world(prepared_database, "ai-race-reply", steps=[5, 30])
    try:
        conversation = await _due_step(world)

        async def customer_answers() -> None:
            await customer_writes(world, conversation, "Never mind, found it.")

        world.model.on_call = customer_answers
        await sweeper(world).run_once()

        assert len(world.model.calls) == 1
        assert step_sends(world, await wa_id(world, conversation)) == []
        step = (await steps_of(world, conversation))[0]
        assert (step.status, step.cancelled_reason) == (
            FollowUpStatus.CANCELLED,
            "customer_replied",
        )
        assert step.claim_token is None and step.message_id is None
        assert (await runs_of(world, conversation))[0].end_reason == "customer_replied"
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_a_takeover_while_the_model_writes_discards_the_text(prepared_database: str) -> None:
    world = await _world(prepared_database, "ai-race-takeover", steps=[5])
    try:
        conversation = await _due_step(world)
        async with world.database.session_factory() as session:
            person = User(
                email=f"ai-{uuid.uuid4().hex[:8]}{USER_DOMAIN}",
                full_name="Colleague",
                is_active=True,
                email_verified_at=datetime.now(UTC),
            )
            session.add(person)
            await session.flush()
            session.add(
                Membership(
                    tenant_id=world.workspace.tenant_id,
                    user_id=person.id,
                    role=TenantRole.MEMBER,
                    status=MembershipStatus.ACTIVE,
                )
            )
            await session.commit()

        async def colleague_takes_over() -> None:
            async with world.database.session_factory() as session:
                await InboxService(session=session, tenant_id=world.workspace.tenant_id).set_mode(
                    conversation_id=conversation.id,
                    mode=ConversationMode.HUMAN,
                    actor=person,
                )
                await session.commit()

        world.model.on_call = colleague_takes_over
        await sweeper(world).run_once()

        assert step_sends(world, await wa_id(world, conversation)) == []
        assert (await runs_of(world, conversation))[0].end_reason == "human_took_over"
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_no_lock_or_transaction_is_held_while_the_model_writes(
    prepared_database: str,
) -> None:
    """During the call the step and its conversation can be locked NOWAIT by anybody."""
    world = await _world(prepared_database, "ai-no-lock", steps=[5])
    seen: list[str] = []
    engine = create_async_engine(prepared_database, poolclass=NullPool)
    try:
        conversation = await _due_step(world)
        step = await pending(world, conversation)
        assert step is not None

        async def probe() -> None:
            async with engine.connect() as connection:
                await connection.execute(text("SET lock_timeout = '1ms'"))
                for table, key in (("follow_ups", step.id), ("conversations", conversation.id)):
                    await connection.execute(
                        text(
                            f"SELECT 1 FROM {table} WHERE id = :id FOR UPDATE NOWAIT"  # noqa: S608
                        ),
                        {"id": key},
                    )
                    seen.append(table)
                idle = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND state = 'idle in transaction'"
                    )
                )
                seen.append(f"idle_in_transaction={idle}")
                await connection.rollback()

        world.model.on_call = probe
        await sweeper(world).run_once()

        assert seen == ["follow_ups", "conversations", "idle_in_transaction=0"]
        assert len(step_sends(world, await wa_id(world, conversation))) == 1
    finally:
        await engine.dispose()
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- the AI's skip


@pytest.mark.parametrize(
    ("reason", "end_reason"),
    [("purchased", "purchased"), ("concluded", "ai_skipped"), ("declined", "ai_skipped")],
)
async def test_the_ai_s_skip_ends_the_run(
    prepared_database: str, reason: str, end_reason: str
) -> None:
    """ "I'll come to the store tomorrow": asking "are you interested?" makes no sense."""
    world = await _world(prepared_database, f"ai-skip-{reason}", steps=[5, 30])
    try:
        conversation = await _due_step(world, "I'll come to the store tomorrow to buy it.")
        world.model.answer = lambda payload: {"action": "skip", "reason": reason, "text": ""}
        await sweeper(world).run_once()

        (step,) = await steps_of(world, conversation)
        assert (step.status, step.skip_reason) == (FollowUpStatus.SKIPPED, reason)
        assert await pending(world, conversation) is None
        assert (await runs_of(world, conversation))[0].end_reason == end_reason
        assert step_sends(world, await wa_id(world, conversation)) == []
        assert len(await _charges(world)) == 1, "a skip that called the model is one AI turn"
    finally:
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- failures


async def test_a_model_that_fails_twice_skips_the_step_after_exactly_two_calls(
    prepared_database: str,
) -> None:
    world = await _world(prepared_database, "ai-fail", steps=[5, 30])
    try:
        conversation = await _due_step(world)
        world.model.fail = 5
        await sweeper(world).run_once()

        assert len(world.model.calls) == 2
        (step,) = await steps_of(world, conversation)
        assert (step.status, step.skip_reason) == (FollowUpStatus.SKIPPED, "ai_unavailable")
        assert (await runs_of(world, conversation))[0].end_reason == "ai_unavailable"
        assert len(await _charges(world)) == 1
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_a_model_that_fails_once_is_retried_and_sent(prepared_database: str) -> None:
    world = await _world(prepared_database, "ai-retry", steps=[5])
    try:
        conversation = await _due_step(world)
        world.model.fail = 1
        await sweeper(world).run_once()
        assert len(world.model.calls) == 2
        assert len(step_sends(world, await wa_id(world, conversation))) == 1
    finally:
        await erase(world.database)
        await world.database.dispose()


@pytest.mark.parametrize(
    "written",
    [
        "x" * 5000,
        "Call me on +20 100 123 4567 for the price.",
        "Our key is sk-abcdefghijklmnop, keep it.",
        "   ",
    ],
)
async def test_text_that_fails_the_guardrails_is_never_sent(
    prepared_database: str, written: str
) -> None:
    world = await _world(prepared_database, "ai-guard", steps=[5])
    try:
        conversation = await _due_step(world)
        world.model.answer = lambda payload: {"action": "send", "reason": None, "text": written}
        await sweeper(world).run_once()
        (step,) = await steps_of(world, conversation)
        assert (step.status, step.skip_reason) == (FollowUpStatus.SKIPPED, "guardrail")
        number = await wa_id(world, conversation)
        assert [entry for entry in world.meta.requests if entry["to"] == number][-1]["payload"][
            "text"
        ]["body"] == "It costs 100."
    finally:
        await erase(world.database)
        await world.database.dispose()


# --------------------------------------------------------------------------- charging


async def test_a_step_cancelled_before_the_call_costs_nothing(prepared_database: str) -> None:
    world = await _world(prepared_database, "ai-cancelled", steps=[5])
    try:
        conversation = await _due_step(world)
        await customer_writes(world, conversation, "Still deciding.")
        await sweeper(world).run_once()
        assert world.model.calls == []
        assert await _charges(world) == []
    finally:
        await erase(world.database)
        await world.database.dispose()


async def test_an_exhausted_allowance_skips_without_calling_the_model(
    prepared_database: str,
) -> None:
    world = await _world(
        prepared_database, "ai-allowance", steps=[5], limits={"period_ai_turns": 0}
    )
    try:
        conversation = await _due_step(world)
        await sweeper(world).run_once()
        assert world.model.calls == []
        (step,) = await steps_of(world, conversation)
        assert (step.status, step.skip_reason) == (FollowUpStatus.SKIPPED, "allowance_exhausted")
        assert (await runs_of(world, conversation))[0].end_reason == "allowance_exhausted"
        async with world.database.session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(UsageEvent)
                    .where(UsageEvent.tenant_id == world.workspace.tenant_id)
                    .where(UsageEvent.event_type == UsageEventType.AI_TURN)
                )
                == 0
            )
    finally:
        await erase(world.database)
        await world.database.dispose()
