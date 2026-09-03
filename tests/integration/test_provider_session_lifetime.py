"""Does an agent turn hold a database connection while it waits for OpenAI?

Before this phase it did, and that made the concurrency of agent turns
`pool_size + max_overflow` per worker process rather than the depth of the
queue. This file is the executed proof that it no longer does, and it is
written to *fail* if the provider call moves back inside the transaction
(ADR-080).

**Why the pool is one connection with no overflow.** A generous pool proves
nothing: two turns would both get a connection whether or not either released
it, and the test would pass on the broken code. With exactly one connection,
"both turns are inside the provider call at the same time" is only reachable if
the first one gave the connection back. That is the whole design.

**Why a barrier and not a stopwatch.** The proof is structural: each turn
announces itself on arrival at the provider and then waits for the other to
arrive. If the connection were held, the second turn could not reach the
provider at all, and the barrier would time out instead of a wall-clock
assertion coming out the wrong side of a threshold on a busy CI machine.

These tests own their data. They need real commits - a release *is* a commit -
so they cannot use the transaction-rollback fixture the rest of the suite
shares. Each builds its own engine and deletes its workspace afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.orchestrator import AgentOrchestrator
from app.agents.registry import ToolRegistry
from app.db.models.agent import Agent, AgentStatus
from app.db.models.billing import (
    BillingInterval,
    LimitKey,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEventType
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.openai.types import AgentReply, TokenUsage
from app.repositories.billing_repository import SubscriptionRepository
from app.services.entitlement_service import EntitlementService

pytestmark = pytest.mark.integration

MODEL = "claude-opus-5"
TURNS = 2
# Generous: it is a deadlock detector, not a performance threshold. A machine
# slow enough to miss this is a machine where nothing is being measured anyway.
BARRIER_TIMEOUT = 20.0

NOW = datetime.now(UTC)
PERIOD_START = NOW - timedelta(days=5)
PERIOD_END = NOW + timedelta(days=25)


class BlockingProvider:
    """A provider that parks every caller until the test lets it go.

    Duck-types `ResponsesClient.respond`. `arrived` is released as each turn
    reaches the provider and nothing returns until `resume` is set, so the
    window in which "every turn is inside the provider call" is held open by
    the test rather than closing the instant the last one arrives.

    That matters for what is being measured. A barrier the provider owns frees
    all its waiters the moment the last one turns up, and they immediately go
    back to the database - so a checkout count sampled afterwards catches the
    turns *resuming*, not the turns waiting, and reports a connection in use
    that has nothing to do with the provider call. Handing the gate to the test
    makes the sample point exact.
    """

    def __init__(self, *, reply: str = "answered") -> None:
        self.arrived = asyncio.Semaphore(0)
        self.resume = asyncio.Event()
        self.calls = 0
        self._reply = reply

    async def respond(self, **_: object) -> AgentReply:
        self.calls += 1
        self.arrived.release()
        async with asyncio.timeout(BARRIER_TIMEOUT):
            await self.resume.wait()
        return AgentReply(
            text=self._reply,
            tool_calls=(),
            usage=TokenUsage(input_tokens=7, output_tokens=11, total_tokens=18),
            response_id="resp_blocking",
            raw={},
        )


@pytest_asyncio.fixture
async def one_connection(prepared_database: str):
    """An engine whose pool holds exactly one connection, and a session maker.

    `pool_timeout` is short on purpose: on the broken code the second turn asks
    for a connection nobody will give back, and failing in seconds reads as the
    bug it is rather than as a hung suite.
    """
    engine = create_async_engine(
        prepared_database,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def workspace(one_connection) -> AsyncIterator[dict[str, uuid.UUID]]:
    """A committed workspace with two conversations, removed afterwards.

    Committed rather than staged, because the code under test commits and a
    staged row would vanish underneath it. Cleanup is a delete of the tenant,
    which cascades.
    """
    _, factory = one_connection
    suffix = uuid.uuid4().hex[:8]
    async with factory() as session:
        tenant = Tenant(name="Pool Pressure", slug=f"pool-{suffix}")
        session.add(tenant)
        await session.flush()
        agent = Agent(
            tenant_id=tenant.id,
            name="Answering",
            model=MODEL,
            system_prompt="You answer briefly.",
            status=AgentStatus.ACTIVE,
            is_default=True,
        )
        account = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"phone-{suffix}",
            waba_id="555000111",
            display_phone_number="+201000000000",
        )
        session.add_all([agent, account])
        await session.flush()

        conversations = []
        for index in range(TURNS):
            contact = Contact(tenant_id=tenant.id, wa_id=f"2010{suffix}{index}")
            session.add(contact)
            await session.flush()
            conversation = Conversation(
                tenant_id=tenant.id,
                contact_id=contact.id,
                account_id=account.id,
                status=ConversationStatus.OPEN,
                mode=ConversationMode.AI,
            )
            session.add(conversation)
            await session.flush()
            session.add(
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    kind=MessageKind.TEXT,
                    status=MessageStatus.DELIVERED,
                    body=f"How much does finishing cost? ({index})",
                )
            )
            conversations.append(conversation.id)
        await session.commit()
        tenant_id = tenant.id

    yield {"tenant_id": tenant_id, "conversations": conversations}

    async with factory() as session:
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


async def _turn(factory, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, provider):
    """One agent turn on its own session, as the worker runs it."""
    async with factory() as session:
        orchestrator = AgentOrchestrator(
            session=session,
            tenant_id=tenant_id,
            client=provider,
            registry=ToolRegistry(),
        )
        outcome = await orchestrator.answer(conversation_id=conversation_id)
        await session.commit()
        return outcome


async def test_two_turns_wait_on_the_provider_at_once_through_a_pool_of_one(
    one_connection,
    workspace,
):
    """GATE: the connection is in the pool while the provider is thinking.

    Two assertions, and the second is the one that cannot be argued with. The
    barrier proves both turns reached the provider, which a pool of one makes
    impossible unless the first released. The pool's own checkout count,
    sampled while both are parked, says the same thing from the other side.
    """
    engine, factory = one_connection
    provider = BlockingProvider()

    turns = [
        asyncio.create_task(
            _turn(
                factory,
                tenant_id=workspace["tenant_id"],
                conversation_id=conversation_id,
                provider=provider,
            )
        )
        for conversation_id in workspace["conversations"]
    ]
    try:
        # Both turns have entered the provider call. On the old code this line
        # is where the test hangs: turn two is still queued for a connection
        # turn one is holding for the length of the inference.
        async with asyncio.timeout(BARRIER_TIMEOUT):
            for _ in range(TURNS):
                await provider.arrived.acquire()

        # Sampled while both turns are parked inside the provider and neither
        # can have resumed, because only this line lets them.
        checked_out_while_waiting = engine.pool.checkedout()
        provider.resume.set()
        outcomes = await asyncio.gather(*turns)
    finally:
        for task in turns:
            task.cancel()

    assert provider.calls == TURNS
    assert (
        checked_out_while_waiting == 0
    ), "a connection was checked out while every turn was waiting on the provider"
    assert [outcome.reply for outcome in outcomes] == ["answered"] * TURNS


async def test_the_pool_is_genuinely_one_connection(one_connection, workspace):
    """The control.

    If the pool silently allowed two connections the test above would pass on
    the broken code and mean nothing, so this asserts the constraint it relies
    on: a second connection, asked for while the first is held, is refused.
    """
    engine, factory = one_connection

    async with factory() as held:
        await held.execute(text("SELECT 1"))
        assert engine.pool.checkedout() == 1
        async with factory() as second:
            with pytest.raises(Exception, match="(?i)timeout|QueuePool"):
                await second.execute(text("SELECT 1"))


async def test_a_release_makes_the_turns_work_committed_rather_than_pending(
    one_connection,
    workspace,
):
    """What the release costs, asserted rather than described.

    A turn is no longer one transaction, so a reply that is composed and then
    lost still leaves the sentiment reading and any tool work that preceded it.
    Here the observable is simpler and enough: the conversation row this turn
    read is still readable on a *different* session mid-turn, which it could
    not be if the turn were holding an uncommitted transaction over the only
    connection in the pool.
    """
    engine, factory = one_connection
    provider = BlockingProvider()
    conversation_id = workspace["conversations"][0]

    turn = asyncio.create_task(
        _turn(
            factory,
            tenant_id=workspace["tenant_id"],
            conversation_id=conversation_id,
            provider=provider,
        )
    )
    try:
        async with asyncio.timeout(BARRIER_TIMEOUT):
            await provider.arrived.acquire()
        # Mid-provider-call, on another session, over the same single-connection
        # pool. This is the read that used to be impossible.
        async with factory() as observer:
            mode = await observer.scalar(
                select(Conversation.mode).where(Conversation.id == conversation_id)
            )
        assert mode is ConversationMode.AI
        assert engine.pool.checkedout() == 0
        provider.resume.set()
        outcome = await turn
    finally:
        turn.cancel()

    assert outcome.reply == "answered"


# ------------------------ what the release means for state that moves under it


async def test_the_allowance_is_resolved_again_for_every_round(one_connection, workspace):
    """A plan that changes mid-turn is seen by the next round, not cached.

    The reservation is what enforces the AI-request limit, and moving the
    provider call out of the transaction only stays safe if each round still
    asks the database rather than a value read before the first inference. So
    this suspends the subscription between two reservations and asserts the
    second one resolved the change.

    The *policy* it asserts is the existing one and is deliberately not a
    lockout: a subscription that has stopped serving falls back to the default
    plan's limits (`EntitlementService._resolve`), so a suspended workspace
    keeps answering customers at free-tier allowance instead of going silent.
    Here the fallback plan permits nothing, which is what makes the change
    observable at all.
    """
    _, factory = one_connection
    tenant_id = workspace["tenant_id"]

    async with factory() as session:
        paid = Plan(
            code=f"paid-{uuid.uuid4().hex[:8]}",
            name="Paid",
            price=Decimal("10.00"),
            currency="USD",
            interval=BillingInterval.MONTHLY,
            limits={LimitKey.PERIOD_AI_REQUESTS.value: 100},
        )
        free = Plan(
            code=f"free-{uuid.uuid4().hex[:8]}",
            name="Free",
            price=Decimal("0.00"),
            currency="USD",
            interval=BillingInterval.MONTHLY,
            limits={LimitKey.PERIOD_AI_REQUESTS.value: 0},
        )
        session.add_all([paid, free])
        await session.flush()
        plan_ids = [paid.id, free.id]
        subscription = SubscriptionRepository(session, tenant_id=tenant_id).create(
            plan_id=paid.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=PERIOD_START,
            current_period_end=PERIOD_END,
        )
        await session.commit()
        subscription_id = subscription.id
        free_code = free.code

    async def reserve() -> bool:
        """One round's reservation, exactly as the worker builds it."""
        async with factory() as reservation:
            entitlements = EntitlementService(
                reservation,
                tenant_id=tenant_id,
                default_plan_code=free_code,
            )
            outcome = await entitlements.consume(
                LimitKey.PERIOD_AI_REQUESTS,
                event_type=UsageEventType.AI_REQUEST,
            )
            await reservation.commit()
            return outcome.allowed

    assert await reserve() is True

    # The turn is now, conceptually, inside its first inference. Nothing holds
    # a connection, which is what lets this happen at all on a pool of one.
    async with factory() as elsewhere:
        held = await elsewhere.get(Subscription, subscription_id)
        assert held is not None
        held.status = SubscriptionStatus.SUSPENDED
        await elsewhere.commit()

    assert await reserve() is False

    # Plans have no `tenant_id`, so the workspace fixture's delete does not
    # reach them. Left behind, they are rows the plan-catalogue suites count.
    #
    # The subscription goes first, and by hand: the foreign key from a
    # subscription to its plan is `RESTRICT`, deliberately, so a plan somebody
    # is paying for cannot be deleted out from under them. The workspace
    # fixture would cascade it away, but that teardown runs after this line.
    async with factory() as session:
        await session.execute(delete(Subscription).where(Subscription.id == subscription_id))
        await session.execute(delete(Plan).where(Plan.id.in_(plan_ids)))
        await session.commit()


async def test_a_reservation_can_be_taken_while_a_provider_call_is_in_flight(
    one_connection,
    workspace,
):
    """The deadlock this ordering exists to avoid.

    `reserve_round` needs a session of its own, because `consume` holds an
    advisory lock until its transaction ends and holding that across an
    inference would serialise every conversation in the workspace. Two sessions
    at once needs two connections - unless the turn has given its own back,
    which is why the reservation sits inside the released block rather than
    before it.
    """
    _, factory = one_connection
    provider = BlockingProvider()
    reserved: list[bool] = []

    async def reserve() -> bool:
        async with factory() as reservation:
            await reservation.execute(text("SELECT 1"))
            reserved.append(True)
            return True

    async def turn():
        async with factory() as session:
            orchestrator = AgentOrchestrator(
                session=session,
                tenant_id=workspace["tenant_id"],
                client=provider,
                registry=ToolRegistry(),
                reserve_round=reserve,
            )
            outcome = await orchestrator.answer(conversation_id=workspace["conversations"][0])
            await session.commit()
            return outcome

    task = asyncio.create_task(turn())
    try:
        async with asyncio.timeout(BARRIER_TIMEOUT):
            await provider.arrived.acquire()
        assert reserved == [True], "the reservation ran before the provider was called"
        provider.resume.set()
        outcome = await task
    finally:
        task.cancel()

    assert outcome.reply == "answered"
