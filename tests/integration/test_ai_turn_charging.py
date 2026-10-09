"""AI turns are held at engagement and charged on successful generation (ENT-01..04).

ADR-131. Through the real `AgentWorker`, the real orchestrator, real PostgreSQL
and real Redis, with OpenAI and Meta faked at the transport (`ai_harness`):
nothing here hand-sets an outcome. Every charge and release below is what the
worker decided from what the fake provider answered.

What is proved:

- **Charged on a usable outcome** (ENT-02): a reply, a handoff the agent's own
  tool executed, a reply withheld after it was written, a reply whose delivery
  failed. Exactly one `ai_turn` event, stamped with the turn, its channel and
  connection, at the moment the hold was taken.
- **Not charged otherwise**: a provider failure past the turn's own retries, an
  empty answer, an escalation before any composition - the hold is given back,
  and the next customer can use it. Turns refused before generation never hold.
- **Never oversold** (ENT-03): ten turns overlapping against an allowance of
  three - all ten provably at the decision together, the three holders provably
  still generating while the other seven decide - charge three, reply three and
  hand seven to a person.
- **One allowance for every channel** (ENT-01).
- **Idempotent settle, TTL, cycle**: a second settle changes nothing; an expired
  hold stops counting by the clock, is released by the real billing sweep, and a
  late settle still charges; a hold counts only in the cycle it was taken in.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.agents.registry import HANDOFF_TOOL
from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.db.models.agent_turn import (
    AgentTurn,
    AITurnChargeState,
    AITurnReleaseReason,
    TurnOutcome,
)
from app.db.models.billing import LimitKey
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.conversation import Conversation, ConversationMode, Message, MessageStatus
from app.db.models.usage import UsageEvent, UsageEventType, UsageUnit
from app.db.session import Database
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.repositories.agent_turn_repository import AgentTurnRepository, ExpiredHoldSweep
from app.services.ai_turn_charge import AITurnCharge, SettleResult
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.entitlement_service import Entitlement, EntitlementService
from app.workers.ai_worker import QUOTA_HANDOFF_REASON
from app.workers.billing_worker import BillingWorker
from app.workers.queue import AgentQueue
from tests.channel_fakes import SyntheticAdapter, synthetic_payload
from tests.integration.ai_harness import (
    DEFAULT_REPLY,
    FakeProviders,
    JsonObject,
    TurnRunner,
    Workspace,
    _TakesTheHandoff,
    scripted,
    text_response,
    tool_call_response,
)

pytestmark = pytest.mark.integration

TURNS = LimitKey.PERIOD_AI_TURNS.value
CHARGED, HELD, RELEASED = (
    AITurnChargeState.CHARGED,
    AITurnChargeState.HELD,
    AITurnChargeState.RELEASED,
)
GATE_DEADLINE_SECONDS = 120.0
# The races prove the allowance cannot be oversold, which does not depend on
# how fast the database is. They decide one turn at a time under the lock, and
# on a loaded machine a single count was measured at 16.6 s, so ten in a row
# could outlast the production lock wait (20 s) and send the tail to the retry
# path - correct behaviour, proven by its own test with a 300 ms wait, but not
# this race. The racing workers get room instead.
RACE_LOCK_WAIT = timedelta(seconds=100)
RACE_SESSION_TIMEOUT_MS = 150_000


# ---------------------------------------------------------------- helpers


@dataclass(frozen=True, slots=True)
class Charge:
    agent_turn_id: uuid.UUID | None
    channel: Channel | None
    connection_id: uuid.UUID | None
    occurred_at: datetime


async def _turns(ai_turns: TurnRunner, tenant_id: uuid.UUID) -> list[AgentTurn]:
    async with ai_turns.database.session() as session:
        rows = await session.scalars(
            select(AgentTurn).where(AgentTurn.tenant_id == tenant_id).order_by(AgentTurn.created_at)
        )
        return list(rows)


async def _charges(ai_turns: TurnRunner, tenant_id: uuid.UUID) -> list[Charge]:
    async with ai_turns.database.session() as session:
        rows = await session.execute(
            select(
                UsageEvent.agent_turn_id,
                UsageEvent.channel,
                UsageEvent.connection_id,
                UsageEvent.occurred_at,
            ).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.event_type == UsageEventType.AI_TURN,
            )
        )
        return [Charge(*row) for row in rows.all()]


def _states(turns: list[AgentTurn]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for turn in turns:
        key = "none" if turn.charge_state is None else turn.charge_state.value
        tally[key] = tally.get(key, 0) + 1
    return tally


def _outcomes(turns: list[AgentTurn]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for turn in turns:
        key = "none" if turn.outcome is None else turn.outcome.value
        tally[key] = tally.get(key, 0) + 1
    return tally


async def _failing(_request: JsonObject) -> httpx.Response:
    """A provider that fails every attempt; `retry-after: 0` spends the retries at once."""
    return httpx.Response(
        500, json={"error": {"message": "upstream failed"}}, headers={"retry-after": "0"}
    )


async def _poll(
    condition: Callable[[], Awaitable[bool]], *, what: str, deadline: float = GATE_DEADLINE_SECONDS
) -> None:
    """Wait until `condition` holds, or fail - a condition wait, never an ordering by sleep."""
    loop = asyncio.get_running_loop()
    until = loop.time() + deadline
    while not await condition():
        if loop.time() > until:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def _advisory_waiters(database: Database) -> int:
    """Connections to this database blocked on an advisory lock right now."""
    async with database.session() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                )
            )
            or 0
        )


async def _count_outcome(ai_turns: TurnRunner, tenant_id: uuid.UUID, outcome: str) -> int:
    async with ai_turns.database.session() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(AgentTurn)
                .where(AgentTurn.tenant_id == tenant_id, AgentTurn.outcome == outcome)
            )
            or 0
        )


class DecisionGate:
    """Holds every hold decision until all of them are provably at the decision.

    Armed for `expected` decisions. A turn that has counted waits until every
    other undecided turn has either counted too or is blocked on the
    workspace's advisory lock - which, with the lock in place, is where they
    are, and without it (M-E05) is never true until all have counted. So with
    the lock the turns decide one after another, each seeing the holds before
    it; without it, every one of them counts the same empty figure.

    Every armed turn first arrives at the decision (`arrive`) before any of
    them asks for the lock. Without that, the first turn would wait at the
    gate *holding* the lock for as long as the slowest worker took to dequeue
    and claim, and on a cold database the early waiters' lock wait
    (`AI_TURN_HOLD_LOCK_WAIT`) could run out first - a harness artefact a
    production turn, which holds the lock for milliseconds, never meets.

    Once one decision has seen every undecided turn blocked on the lock, the
    rest decide without asking again: a turn blocked on the lock cannot move
    without it. Re-polling the database at every decision made ten decisions
    in a row outlast the lock wait on a loaded machine. Without the lock
    every turn has counted before the first one proceeds, so the race stays
    exactly as decisive.
    """

    def __init__(self, database: Database, expected: int) -> None:
        self.database = database
        self.expected = expected
        self.arrived = 0
        self.counted = 0
        self.decided = 0
        self.everyone_seen = False

    async def arrive(self) -> None:
        if self.arrived >= self.expected:
            return
        self.arrived += 1

        async def everyone_has_arrived() -> bool:
            return self.arrived >= self.expected

        await _poll(everyone_has_arrived, what="every turn to arrive at the hold decision")

    async def wait(self) -> None:
        self.counted += 1
        if not self.everyone_seen:

            async def everyone_is_here() -> bool:
                undecided = self.expected - self.decided
                in_flight = self.counted - self.decided
                return in_flight + await _advisory_waiters(self.database) >= undecided

            await _poll(everyone_is_here, what="every turn to reach the hold decision")
            self.everyone_seen = True
        self.decided += 1


@pytest.fixture
def decision_gate(monkeypatch: pytest.MonkeyPatch) -> Callable[[Database, int], DecisionGate]:
    """Install a `DecisionGate` behind the AI turn count of `EntitlementService`."""
    import app.services.entitlement_service as entitlement_module

    original = EntitlementService._used_and_held
    original_hold = EntitlementService.hold_ai_turn
    monkeypatch.setattr(entitlement_module, "AI_TURN_HOLD_LOCK_WAIT", RACE_LOCK_WAIT)

    def install(database: Database, expected: int) -> DecisionGate:
        gate = DecisionGate(database, expected)

        async def arriving(self: EntitlementService) -> Entitlement:
            await gate.arrive()
            return await original_hold(self)

        async def gated(self: EntitlementService, key: LimitKey, **kwargs: Any) -> tuple[int, int]:
            counted = await original(self, key, **kwargs)
            if key is LimitKey.PERIOD_AI_TURNS and gate.counted < gate.expected:
                await gate.wait()
            return counted

        monkeypatch.setattr(EntitlementService, "hold_ai_turn", arriving)
        monkeypatch.setattr(EntitlementService, "_used_and_held", gated)
        return gate

    return install


async def _resolved(ai_turns: TurnRunner, workspace: Workspace) -> None:
    """Resolve the workspace's plan once, before a race, so its first version exists.

    The first read of a plan with no version writes version 1. Done inside a
    race, the first turn's write would sit uncommitted while it waits at the
    `DecisionGate`, and every other turn would block on that row rather than
    reach the decision - so a race test would measure the catalogue, not the
    hold.
    """
    async with ai_turns.database.session() as session:
        await EntitlementService(
            session,
            tenant_id=workspace.tenant_id,
            default_plan_code=ai_turns.settings.default_plan_code,
        ).check(LimitKey.PERIOD_AI_TURNS, additional=0)


async def _customers(ai_turns: TurnRunner, workspace: Workspace, count: int) -> list[uuid.UUID]:
    """`count` customers write once each; their turns are queued. Returns the conversations."""
    conversations = []
    for index in range(count):
        conversation_id, ids = await ai_turns.write(
            workspace, [f"hello from {index}"], wa_id=f"2015550{index:05d}"
        )
        await ai_turns.enqueue(workspace, conversation_id, ids[0])
        conversations.append(conversation_id)
    return conversations


async def _race(ai_turns: TurnRunner, workers: int, registry: ChannelRegistry | None = None) -> int:
    """`TurnRunner.race`, with the worker's channel registry replaceable.

    Each worker's sessions may wait out `RACE_LOCK_WAIT` (statement and
    idle-in-transaction bounds raised to match); every other bound is the
    deployment's.
    """
    settings = ai_turns.settings.model_copy(
        update={
            "database_statement_timeout_ms": RACE_SESSION_TIMEOUT_MS,
            "database_idle_in_transaction_timeout_ms": RACE_SESSION_TIMEOUT_MS,
        }
    )
    databases = [Database(settings) for _ in range(workers)]
    try:
        runners = []
        for database in databases:
            worker = ai_turns.worker(database)
            if registry is not None:
                worker._channels = registry
            runners.append(worker.run_once(wait_seconds=1))
        results = await asyncio.gather(*runners)
    finally:
        for database in databases:
            await database.dispose()
    return sum(1 for result in results if result)


async def _quota_handoffs(ai_turns: TurnRunner, conversations: list[uuid.UUID]) -> int:
    handed = 0
    for conversation_id in conversations:
        conversation = await ai_turns.conversation(conversation_id)
        if (
            conversation.mode is ConversationMode.HUMAN
            and conversation.handoff_reason == QUOTA_HANDOFF_REASON
        ):
            handed += 1
    return handed


# ------------------------------------------------------ ENT-02: what is charged


async def test_a_reply_is_charged_once_on_its_channel_and_connection(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    assert await ai_turns.answer(workspace, conversation_id, ids[0]) == 1

    assert ai_providers.inference == 1
    assert len(ai_providers.sends) == 1
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.charge_state is CHARGED
    assert turn.held_at is not None and turn.charged_at is not None
    assert turn.held_at <= turn.charged_at
    assert (turn.released_at, turn.charge_release_reason) == (None, None)
    assert await _charges(ai_turns, workspace.tenant_id) == [
        Charge(turn.id, Channel.WHATSAPP, workspace.account_id, turn.held_at)
    ]
    async with ai_turns.database.session() as session:
        [(quantity, unit, meta)] = (
            await session.execute(
                select(UsageEvent.quantity, UsageEvent.unit, UsageEvent.meta).where(
                    UsageEvent.agent_turn_id == turn.id
                )
            )
        ).all()
    assert (quantity, unit, meta) == (1, UsageUnit.COUNT, {"conversation_id": str(conversation_id)})


async def test_a_provider_failure_is_not_charged_and_gives_its_hold_back(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """M-E01's killer: charging at engagement charges this turn."""
    await ai_turns.plan({TURNS: 1})
    workspace = await ai_turns.workspace()
    ai_providers.agent = _failing
    first, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, first, ids[0])

    # The path was reached: the turn held, engaged and spent its own retries.
    assert ai_providers.inference == 3
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.charge_state is RELEASED
    assert turn.charge_release_reason is AITurnReleaseReason.GENERATION_FAILED
    assert turn.held_at is not None and turn.charged_at is None
    usage = await ai_turns.usage(workspace.tenant_id)
    assert "ai_turn" not in usage
    # The provider-request cost meter is unchanged: every attempt was a request.
    assert usage["ai_request"] >= 1

    # The hold went back, so the allowance of one answers the next customer.
    ai_providers.agent = scripted(text_response(DEFAULT_REPLY))
    second, more = await ai_turns.write(workspace, ["anyone?"], wa_id="201555000333")
    await ai_turns.answer(workspace, second, more[0])

    assert len(ai_providers.sends) == 1
    assert (await ai_turns.usage(workspace.tenant_id))["ai_turn"] == 1
    assert (await ai_turns.conversation(second)).mode is ConversationMode.AI


async def test_an_empty_answer_is_not_charged(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """M-E02's killer."""
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    ai_providers.agent = scripted(text_response(None))
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 1
    # The customer is still told a colleague will follow up.
    assert len(ai_providers.sends) == 1
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.outcome is not None and turn.outcome.value == "empty_response"
    assert turn.charge_state is RELEASED
    assert turn.charge_release_reason is AITurnReleaseReason.NOT_CHARGEABLE
    assert "ai_turn" not in await ai_turns.usage(workspace.tenant_id)


async def test_an_escalation_before_any_reply_is_not_charged(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    ai_providers.reading = {
        "sentiment": "angry",
        "score": -0.95,
        "intent": "complaint",
        "confidence": 0.95,
    }
    conversation_id, ids = await ai_turns.write(workspace, ["this is a disgrace"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert (ai_providers.sentiment, ai_providers.inference) == (1, 0)
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.outcome is not None and turn.outcome.value == "escalated"
    assert turn.charge_state is RELEASED
    assert turn.charge_release_reason is AITurnReleaseReason.NOT_CHARGEABLE
    assert "ai_turn" not in await ai_turns.usage(workspace.tenant_id)


async def test_a_handoff_the_agent_executed_is_charged(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace(grants=[HANDOFF_TOOL])
    ai_providers.agent = scripted(
        tool_call_response(HANDOFF_TOOL, {"reason": "The customer asked for a person."})
    )
    conversation_id, ids = await ai_turns.write(workspace, ["get me a human"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.sends == []
    assert (await ai_turns.conversation(conversation_id)).mode is ConversationMode.HUMAN
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.outcome is not None and turn.outcome.value == "handed_off"
    assert turn.charge_state is CHARGED
    assert len(await _charges(ai_turns, workspace.tenant_id)) == 1


async def test_a_reply_whose_delivery_fails_is_still_charged(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """M-E03's killer: the generation happened, so the turn is charged."""
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    ai_providers.meta_status = 400
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    await ai_turns.answer(workspace, conversation_id, ids[0])

    # The send was attempted, refused by the provider and recorded as failed.
    assert len(ai_providers.sends) == 1
    [message] = await ai_turns.outbound(workspace.tenant_id)
    assert message.status is MessageStatus.FAILED
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.charge_state is CHARGED
    assert len(await _charges(ai_turns, workspace.tenant_id)) == 1


async def test_a_reply_withheld_after_it_was_written_is_charged(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """A colleague takes over while the model composes: nothing is sent, the cost was incurred."""
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])

    async def taken_over_while_composing(_request: JsonObject) -> JsonObject:
        await ai_turns.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(mode=ConversationMode.HUMAN)
        )
        return text_response("Here is everything you asked for.")

    ai_providers.agent = taken_over_while_composing

    await ai_turns.answer(workspace, conversation_id, ids[0])

    assert ai_providers.inference == 1
    assert ai_providers.sends == []
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.outcome is not None and turn.outcome.value == "suppressed_human"
    assert turn.charge_state is CHARGED
    assert len(await _charges(ai_turns, workspace.tenant_id)) == 1


async def test_turns_refused_before_generation_hold_nothing(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    # A conversation a person owns.
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    owned, ids = await ai_turns.write(workspace, ["hello"])
    await ai_turns.execute(
        update(Conversation).where(Conversation.id == owned).values(mode=ConversationMode.HUMAN)
    )
    await ai_turns.answer(workspace, owned, ids[0])

    # A workspace with no allowance left.
    await ai_turns.plan({TURNS: 0})
    exhausted = await ai_turns.workspace()
    blocked, more = await ai_turns.write(exhausted, ["hello"])
    await ai_turns.answer(exhausted, blocked, more[0])

    assert ai_providers.inference == 0
    for tenant_id, outcome in (
        (workspace.tenant_id, "suppressed_human"),
        (exhausted.tenant_id, "quota_blocked"),
    ):
        [turn] = await _turns(ai_turns, tenant_id)
        assert turn.outcome is not None and turn.outcome.value == outcome
        assert (turn.charge_state, turn.held_at) == (None, None)
        assert "ai_turn" not in await ai_turns.usage(tenant_id)
    conversation = await ai_turns.conversation(blocked)
    assert conversation.handoff_reason == QUOTA_HANDOFF_REASON


async def test_a_duplicate_job_holds_and_charges_once(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    await ai_turns.enqueue(workspace, conversation_id, ids[0])
    await ai_turns.enqueue(workspace, conversation_id, ids[0])

    assert await ai_turns.drain() == 2, "both envelopes must reach a worker"

    assert ai_providers.inference == 1
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.charge_state is CHARGED
    assert len(await _charges(ai_turns, workspace.tenant_id)) == 1


async def test_a_turn_that_cannot_get_the_allowance_lock_is_retried_not_lost(
    ai_turns: TurnRunner, ai_providers: FakeProviders, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contention on the workspace's lock ends in a retry, never a dead letter (ENT-03).

    Found at runtime (APP-E2E R-3): ten turns on one workspace, under load,
    queued on the lock past the session's lock timeout and six were
    dead-lettered unanswered. Here the lock is held by another transaction for
    longer than the hold may wait.
    """
    import app.services.entitlement_service as entitlement_module

    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    await ai_turns.enqueue(workspace, conversation_id, ids[0])
    monkeypatch.setattr(entitlement_module, "AI_TURN_HOLD_LOCK_WAIT", timedelta(milliseconds=300))
    lock = entitlement_module._lock_id(workspace.tenant_id, LimitKey.PERIOD_AI_TURNS)
    worker = ai_turns.worker()
    queue = worker.queue

    async with ai_turns.database.session() as blocker:
        await blocker.execute(select(func.pg_advisory_xact_lock(lock)))
        assert await worker.run_once(wait_seconds=1)

        # Retried, with nothing held, engaged, called or charged - and the
        # claim given back, so the retry can adopt it at once.
        assert await queue.delayed_depth() == 1
        assert await queue.dead_letters() == []
        [turn] = await _turns(ai_turns, workspace.tenant_id)
        assert (str(turn.state), turn.charge_state) == ("claimed", None)
        assert turn.claim_expires_at is not None
        assert turn.claim_expires_at <= datetime.now(UTC)
        assert ai_providers.inference == 0
        assert await _charges(ai_turns, workspace.tenant_id) == []
        await blocker.rollback()

    assert await queue.promote_due(now=datetime.now(UTC) + timedelta(minutes=5)) == 1
    assert await ai_turns.drain() == 1

    assert ai_providers.inference == 1
    assert len(ai_providers.sends) == 1
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.charge_state is CHARGED
    assert len(await _charges(ai_turns, workspace.tenant_id)) == 1


# ------------------------------------------------- ENT-03: never oversold


async def test_ten_overlapping_turns_against_three_charge_three_and_hand_off_seven(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    decision_gate: Callable[[Database, int], DecisionGate],
) -> None:
    """M-E04's and M-E05's killer.

    All ten are at the hold decision together (`DecisionGate`), and the three
    that hold are still generating - held, not charged - while the other seven
    decide: the provider answers only once the seven have been handed off, or
    once all ten are inside it, which is what an oversold allowance looks like.
    """
    await ai_turns.plan({TURNS: 3})
    workspace = await ai_turns.workspace()
    await _resolved(ai_turns, workspace)
    conversations = await _customers(ai_turns, workspace, 10)
    inside = 0

    async def slow(_request: JsonObject) -> JsonObject:
        nonlocal inside
        inside += 1

        async def the_others_have_decided() -> bool:
            blocked = await _count_outcome(ai_turns, workspace.tenant_id, "quota_blocked")
            return blocked + inside >= 10

        await _poll(the_others_have_decided, what="the other turns to decide")
        return text_response(DEFAULT_REPLY)

    ai_providers.agent = slow
    gate = decision_gate(ai_turns.database, 10)

    assert await _race(ai_turns, 10) == 10, "every envelope must be taken"

    assert gate.decided == 10, "every turn reached the hold decision"
    turns = await _turns(ai_turns, workspace.tenant_id)
    charges = await _charges(ai_turns, workspace.tenant_id)
    assert len(turns) == 10
    assert _states(turns) == {"charged": 3, "none": 7}
    assert _outcomes(turns) == {"replied": 3, "quota_blocked": 7}
    assert len(charges) == 3
    assert {charge.agent_turn_id for charge in charges} == {
        turn.id for turn in turns if turn.charge_state is CHARGED
    }
    assert ai_providers.inference == 3
    assert len(ai_providers.sends) == 3
    assert await _quota_handoffs(ai_turns, conversations) == 7


async def test_ten_overlapping_failing_turns_charge_nothing(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    decision_gate: Callable[[Database, int], DecisionGate],
) -> None:
    """Under full overlap three hold and fail; seven never hold. Nothing is charged.

    The exact figures are the point: all ten decide together, so the seven that
    found the allowance held are handed to a person, and the three holds are
    all given back. `test_failing_turns_in_waves_give_every_hold_back` is the
    case where every one of the ten holds.
    """
    await ai_turns.plan({TURNS: 3})
    workspace = await ai_turns.workspace()
    await _resolved(ai_turns, workspace)
    conversations = await _customers(ai_turns, workspace, 10)
    inside: set[str] = set()
    others_decided = asyncio.Event()

    async def failing_once_the_others_decided(request: JsonObject) -> httpx.Response:
        # Held open until the seven have decided, so a fast failure cannot give
        # its hold back to a turn that has not counted yet.
        inside.add(str(request.get("input")))
        if not others_decided.is_set():

            async def the_others_have_decided() -> bool:
                blocked = await _count_outcome(ai_turns, workspace.tenant_id, "quota_blocked")
                return blocked + len(inside) >= 10

            await _poll(the_others_have_decided, what="the other turns to decide")
            others_decided.set()
        return await _failing(request)

    ai_providers.agent = failing_once_the_others_decided
    gate = decision_gate(ai_turns.database, 10)

    assert await _race(ai_turns, 10) == 10

    assert gate.decided == 10
    turns = await _turns(ai_turns, workspace.tenant_id)
    assert _states(turns) == {"released": 3, "none": 7}
    assert {turn.charge_release_reason for turn in turns if turn.charge_state is RELEASED} == {
        AITurnReleaseReason.GENERATION_FAILED
    }
    assert await _charges(ai_turns, workspace.tenant_id) == []
    assert ai_providers.inference == 9, "three turns, each spending three attempts"
    assert ai_providers.sends == []
    assert await _quota_handoffs(ai_turns, conversations) == 7


async def test_failing_turns_in_waves_give_every_hold_back(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """Ten failing turns, at most three at once: every one holds, every hold comes back."""
    await ai_turns.plan({TURNS: 3})
    workspace = await ai_turns.workspace()
    conversations = await _customers(ai_turns, workspace, 10)
    ai_providers.agent = _failing

    taken = 0
    for _ in range(4):
        taken += await _race(ai_turns, 3)

    assert taken == 10
    turns = await _turns(ai_turns, workspace.tenant_id)
    assert _states(turns) == {"released": 10}
    assert await _charges(ai_turns, workspace.tenant_id) == []
    assert await _quota_handoffs(ai_turns, conversations) == 0
    assert ai_providers.inference == 30


async def test_three_failures_then_three_answers_charge_three(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 3})
    workspace = await ai_turns.workspace()
    ai_providers.agent = _failing
    failed = await _customers(ai_turns, workspace, 3)
    assert await ai_turns.drain() == 3

    ai_providers.agent = scripted(text_response(DEFAULT_REPLY))
    answered = []
    for index in range(3):
        conversation_id, ids = await ai_turns.write(
            workspace, ["still there?"], wa_id=f"2015551{index:05d}"
        )
        await ai_turns.answer(workspace, conversation_id, ids[0])
        answered.append(conversation_id)

    turns = await _turns(ai_turns, workspace.tenant_id)
    assert _states(turns) == {"released": 3, "charged": 3}
    assert len(await _charges(ai_turns, workspace.tenant_id)) == 3
    assert len(ai_providers.sends) == 3
    assert await _quota_handoffs(ai_turns, failed + answered) == 0


# ------------------------------------------- ENT-01: one meter, every channel


async def _with_page(
    ai_turns: TurnRunner,
) -> tuple[Workspace, ChannelConnection, SyntheticAdapter, ChannelRegistry]:
    """A workspace with a WhatsApp number and a second, synthetic Messenger connection."""
    workspace = await ai_turns.workspace()
    adapter = SyntheticAdapter(Channel.MESSENGER, tagged=True)
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.MESSENGER: cast(ChannelAdapter, adapter),
        },
    )
    async with ai_turns.database.session() as session:
        page = ChannelConnection(
            id=uuid.uuid4(),
            tenant_id=workspace.tenant_id,
            channel=Channel.MESSENGER,
            external_account_id=f"page-{uuid.uuid4().hex[:10]}",
            status=ConnectionStatus.ACTIVE,
            ownership_started_at=datetime.now(UTC) - timedelta(days=2),
        )
        session.add(page)
    return workspace, page, adapter, registry


async def _page_customer_writes(
    ai_turns: TurnRunner, adapter: SyntheticAdapter, page: ChannelConnection, sender: str
) -> tuple[uuid.UUID, uuid.UUID]:
    mid = f"m.{uuid.uuid4().hex}"
    async with ai_turns.database.session() as session:
        await ChannelIngestionService(
            session=session,
            adapter=cast(ChannelAdapter, adapter),
            queue=cast(AgentQueue, _TakesTheHandoff()),
        ).ingest(
            adapter.parse(
                synthetic_payload(
                    page.external_account_id,
                    {
                        "type": "message",
                        "id": mid,
                        "from": sender,
                        "at": int(datetime.now(UTC).timestamp()),
                        "text": "is anyone there?",
                    },
                )
            )
        )
        message = await session.scalar(select(Message).where(Message.wa_message_id == mid))
        assert message is not None
        return message.conversation_id, message.id


async def test_one_allowance_is_shared_by_every_channel(
    ai_turns: TurnRunner,
    ai_providers: FakeProviders,
    decision_gate: Callable[[Database, int], DecisionGate],
) -> None:
    """M-E06's killer: a WhatsApp turn and a Messenger turn at once, against one turn."""
    await ai_turns.plan({TURNS: 1}, channels=(Channel.WHATSAPP, Channel.MESSENGER))
    workspace, page, adapter, registry = await _with_page(ai_turns)
    await _resolved(ai_turns, workspace)
    whatsapp, ids = await ai_turns.write(workspace, ["hello"])
    await ai_turns.enqueue(workspace, whatsapp, ids[0])
    messenger, trigger = await _page_customer_writes(ai_turns, adapter, page, "psid-0shared")
    await ai_turns.enqueue(workspace, messenger, trigger)
    gate = decision_gate(ai_turns.database, 2)

    assert await _race(ai_turns, 2, registry) == 2

    assert gate.decided == 2
    turns = await _turns(ai_turns, workspace.tenant_id)
    assert _states(turns) == {"charged": 1, "none": 1}
    assert _outcomes(turns) == {"replied": 1, "quota_blocked": 1}
    charges = await _charges(ai_turns, workspace.tenant_id)
    assert len(charges) == 1
    assert len(ai_providers.sends) + len(adapter.log.sent) == 1
    assert await _quota_handoffs(ai_turns, [whatsapp, messenger]) == 1


async def test_each_channel_draws_from_the_workspace_total(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 2}, channels=(Channel.WHATSAPP, Channel.MESSENGER))
    workspace, page, adapter, registry = await _with_page(ai_turns)
    whatsapp, ids = await ai_turns.write(workspace, ["hello"])
    await ai_turns.enqueue(workspace, whatsapp, ids[0])
    assert await _race(ai_turns, 1, registry) == 1
    messenger, trigger = await _page_customer_writes(ai_turns, adapter, page, "psid-0second")
    await ai_turns.enqueue(workspace, messenger, trigger)
    assert await _race(ai_turns, 1, registry) == 1
    # The allowance is spent; a third turn on either channel goes to a person.
    third, more = await _page_customer_writes(ai_turns, adapter, page, "psid-0third")
    await ai_turns.enqueue(workspace, third, more)
    assert await _race(ai_turns, 1, registry) == 1

    charges = await _charges(ai_turns, workspace.tenant_id)
    assert sorted((charge.channel, charge.connection_id) for charge in charges) == sorted(
        [(Channel.MESSENGER, page.id), (Channel.WHATSAPP, workspace.account_id)]
    )
    async with ai_turns.database.session() as session:
        service = EntitlementService(
            session,
            tenant_id=workspace.tenant_id,
            default_plan_code=ai_turns.settings.default_plan_code,
        )
        entitlement = await service.check(LimitKey.PERIOD_AI_TURNS, additional=0)
        by_channel = await service.ai_turns_by_channel()
    assert (entitlement.limit, entitlement.used, entitlement.held, entitlement.remaining) == (
        2,
        2,
        0,
        0,
    )
    assert by_channel == {Channel.WHATSAPP: 1, Channel.MESSENGER: 1}
    assert await _quota_handoffs(ai_turns, [third]) == 1


# ---------------------------------------------- settle, TTL and usage cycle


async def test_settling_twice_charges_once(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """M-E07's killer, and E05's: one charge per turn, ever."""
    await ai_turns.plan({TURNS: 5})
    workspace = await ai_turns.workspace()
    conversation_id, ids = await ai_turns.write(workspace, ["hello"])
    await ai_turns.answer(workspace, conversation_id, ids[0])
    [turn] = await _turns(ai_turns, workspace.tenant_id)
    assert turn.charge_state is CHARGED

    async with ai_turns.database.session() as session:
        charge = AITurnCharge(session, tenant_id=workspace.tenant_id)
        again = await charge.settle(agent_turn_id=turn.id, chargeable=True)
        refund = await charge.settle(agent_turn_id=turn.id, chargeable=False)

    assert (again, refund) == (SettleResult.UNCHANGED, SettleResult.UNCHANGED)
    [after] = await _turns(ai_turns, workspace.tenant_id)
    assert (after.charge_state, after.charged_at) == (CHARGED, turn.charged_at)
    assert len(await _charges(ai_turns, workspace.tenant_id)) == 1

    # The backstop: a second charge naming the turn cannot be written at all.
    with pytest.raises(IntegrityError):
        async with ai_turns.database.session() as session:
            session.add(
                UsageEvent(
                    tenant_id=workspace.tenant_id,
                    event_type=UsageEventType.AI_TURN,
                    quantity=1,
                    unit=UsageUnit.COUNT,
                    occurred_at=datetime.now(UTC),
                    agent_turn_id=turn.id,
                )
            )
    # And only an `ai_turn` row may name a turn.
    with pytest.raises(IntegrityError):
        async with ai_turns.database.session() as session:
            session.add(
                UsageEvent(
                    tenant_id=workspace.tenant_id,
                    event_type=UsageEventType.AI_REQUEST,
                    quantity=1,
                    unit=UsageUnit.COUNT,
                    occurred_at=datetime.now(UTC),
                    agent_turn_id=uuid.uuid4(),
                )
            )


async def _dead_hold(ai_turns: TurnRunner, workspace: Workspace, *, held_at: datetime) -> uuid.UUID:
    """A turn engaged with a hold by a worker that then died."""
    conversation_id, ids = await ai_turns.write(workspace, ["hello"], wa_id="201555999999")
    async with ai_turns.database.session() as session:
        turns = AgentTurnRepository(session, tenant_id=workspace.tenant_id)
        assert await turns.claim(
            conversation_id=conversation_id, trigger_message_id=ids[0], worker_id="died"
        )
        assert await turns.engage(trigger_message_id=ids[0], hold=True, now=held_at)
        turn_id = await turns.id_for(trigger_message_id=ids[0])
    assert turn_id is not None
    return turn_id


async def _expire_like_the_sweep(ai_turns: TurnRunner, *, held_at: datetime) -> list[uuid.UUID]:
    """Release a dead worker's hold the way the billing sweep would (F-1, row class R-OPEN).

    A dead worker's hold is a state production reaches - and leaves within one
    sweep interval of its TTL, because the billing sweep releases it. A test
    that writes one and never runs the sweep leaves a hold open for ever, which
    the kept-data sweep rightly counts as stranded (F-1a). So the test runs the
    sweep's own release, at the first moment that sweep would act on `held_at`:
    now, if the hold is already past its TTL, else the TTL's end. The release is
    global, like the sweep's; anything older it releases is equally dead.
    """
    ttl = timedelta(seconds=ai_turns.settings.ai_turn_hold_ttl_seconds)
    now = max(datetime.now(UTC), held_at + ttl)
    async with ai_turns.database.session() as session:
        released = await ExpiredHoldSweep(session).release_expired(held_before=now - ttl, now=now)
    assert released, "the billing sweep's release released nothing"
    return released


async def _complete_like_the_worker(
    ai_turns: TurnRunner, workspace: Workspace, turn_id: uuid.UUID
) -> None:
    """Finish a late-charged turn as its worker does (F-1, row class R-LATE).

    The worker settles a usable outcome and then completes the turn with it
    (`AgentWorker`: settle, commit, `_complete_turn`). A test that drives only
    the settle leaves a charged turn with no outcome, which is what a charged
    provider failure looks like to the kept-data sweep (B6).
    """
    async with ai_turns.database.session() as session:
        trigger = await session.scalar(
            select(AgentTurn.trigger_message_id).where(
                AgentTurn.tenant_id == workspace.tenant_id, AgentTurn.id == turn_id
            )
        )
        assert trigger is not None
        assert await AgentTurnRepository(session, tenant_id=workspace.tenant_id).complete(
            trigger_message_id=trigger, outcome=TurnOutcome.REPLIED
        )


async def test_an_expired_hold_stops_counting_and_is_released_by_the_sweep(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    """M-E08's killer: a dead worker's hold must not keep the last turn from a customer."""
    await ai_turns.plan({TURNS: 1})
    workspace = await ai_turns.workspace()
    ttl = timedelta(seconds=ai_turns.settings.ai_turn_hold_ttl_seconds)
    now = datetime.now(UTC)
    held_at = now - ttl - timedelta(minutes=1)
    if held_at.month != now.month:  # pragma: no cover - the first minutes of a month
        pytest.skip("an expired hold in the previous month proves the cycle rule, not the TTL")
    dead = await _dead_hold(ai_turns, workspace, held_at=held_at)

    # Within the TTL the same hold counts, so the clock is what released it.
    async with ai_turns.database.session() as session:
        fresh = await EntitlementService(
            session,
            tenant_id=workspace.tenant_id,
            default_plan_code=ai_turns.settings.default_plan_code,
            clock=lambda: held_at + timedelta(minutes=1),
        ).check(LimitKey.PERIOD_AI_TURNS, additional=1)
        expired = await EntitlementService(
            session,
            tenant_id=workspace.tenant_id,
            default_plan_code=ai_turns.settings.default_plan_code,
        ).check(LimitKey.PERIOD_AI_TURNS, additional=1)
    assert (fresh.held, fresh.allowed) == (1, False)
    assert (expired.held, expired.allowed) == (0, True)

    # A customer is answered while the dead hold is still on the row.
    conversation_id, ids = await ai_turns.write(workspace, ["hello?"], wa_id="201555888888")
    await ai_turns.answer(workspace, conversation_id, ids[0])
    assert len(ai_providers.sends) == 1
    turns = {turn.id: turn for turn in await _turns(ai_turns, workspace.tenant_id)}
    assert turns[dead].charge_state is HELD

    # The real billing sweep releases it, as expired.
    await BillingWorker(database=ai_turns.database, settings=ai_turns.settings).run_once(now=now)
    turns = {turn.id: turn for turn in await _turns(ai_turns, workspace.tenant_id)}
    assert turns[dead].charge_state is RELEASED
    assert turns[dead].charge_release_reason is AITurnReleaseReason.HOLD_EXPIRED

    # The worker was not dead after all: its late settle still charges, at the
    # moment it held, and may leave the workspace over its allowance.
    async with ai_turns.database.session() as session:
        late = await AITurnCharge(session, tenant_id=workspace.tenant_id).settle(
            agent_turn_id=dead, chargeable=True
        )
    assert late is SettleResult.LATE_CHARGE
    turns = {turn.id: turn for turn in await _turns(ai_turns, workspace.tenant_id)}
    assert turns[dead].charge_state is CHARGED
    assert turns[dead].charge_release_reason is AITurnReleaseReason.HOLD_EXPIRED
    charged_at = {
        charge.agent_turn_id: charge.occurred_at
        for charge in await _charges(ai_turns, workspace.tenant_id)
    }
    assert charged_at[dead] == held_at
    async with ai_turns.database.session() as session:
        after = await EntitlementService(
            session,
            tenant_id=workspace.tenant_id,
            default_plan_code=ai_turns.settings.default_plan_code,
        ).check(LimitKey.PERIOD_AI_TURNS, additional=0)
    assert (after.used, after.held, after.remaining, after.over_limit) == (2, 0, 0, True)
    await _complete_like_the_worker(ai_turns, workspace, dead)


async def test_a_hold_counts_only_in_the_usage_cycle_it_was_taken_in(
    ai_turns: TurnRunner, ai_providers: FakeProviders
) -> None:
    await ai_turns.plan({TURNS: 1})
    workspace = await ai_turns.workspace()
    month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    await _dead_hold(ai_turns, workspace, held_at=month - timedelta(minutes=2))

    async def held_at(moment: datetime) -> int:
        async with ai_turns.database.session() as session:
            entitlement = await EntitlementService(
                session,
                tenant_id=workspace.tenant_id,
                default_plan_code=ai_turns.settings.default_plan_code,
                clock=lambda: moment,
            ).check(LimitKey.PERIOD_AI_TURNS, additional=0)
            return entitlement.held

    # Both moments are inside the TTL; only the cycle differs.
    assert await held_at(month - timedelta(minutes=1)) == 1
    assert await held_at(month + timedelta(minutes=3)) == 0
    # The dead hold is the test's device, not its subject: release it as the
    # billing sweep would have long ago (F-1, row class R-OPEN).
    await _expire_like_the_sweep(ai_turns, held_at=month - timedelta(minutes=2))
