"""Channel capacity under real concurrency (ENT-07, ENT-08).

Every race here runs on separate committed connections, started together at an
`asyncio.Barrier` - no sleep decides who goes first. Each activation records
how many connections were active in its own transaction just before it
committed, *under the guard's lock*, so "never above capacity" is checked at
every commit rather than only at the end.

A barrier lines up the starts, not the decisions: on a loaded machine one
activation could connect, judge and commit before the others reached the guard,
and a race without the guard's lock (M-E11) would then still leave exactly one -
it did, two runs in three. So the races that count slots go through a
`GuardGate`: every racer arrives at the authoritative check before any of them
asks for the lock, and one the guard allows waits, holding the lock, until every
undecided racer is provably blocked on it.

The suite is run six consecutive times for the report (ENT-08's stability bar).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import BillingInterval, Plan, Subscription, SubscriptionStatus
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.channel_capacity import (
    CapacityReductionCause,
    CapacityReductionStatus,
    ChannelCapacityReduction,
)
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupSource, TopupStatus
from app.db.models.user import User
from app.db.session import Database
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.services.capacity_reduction import ChannelCapacityReductions
from app.services.channel_capacity import (
    ChannelCapacityExceededError,
    ChannelCapacityGuard,
    ChannelSlot,
)
from app.services.channel_connection_service import ChannelConnectionService
from app.services.plan_catalog import PlanCatalog
from app.services.whatsapp_account_service import WhatsAppAccountService
from app.workers.billing_worker import BillingWorker
from tests.billing_fixtures import erase_ledger
from tests.channel_fakes import SyntheticAdapter
from tests.fake_ownership import FakeOwnershipVerifier

pytestmark = pytest.mark.integration

TYPES = ["whatsapp", "instagram", "messenger"]
GATE_DEADLINE_SECONDS = 30.0


@dataclass
class World:
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    plan_id: uuid.UUID
    subscription_id: uuid.UUID
    period_end: datetime
    commits: list[tuple[str, int, int]] = field(default_factory=list)


@pytest_asyncio.fixture
async def maker(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, pool_size=14, max_overflow=6)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _world(maker: async_sessionmaker[AsyncSession], *, slots: int) -> World:
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(UTC).replace(microsecond=0)
    async with maker() as session:
        tenant = Tenant(name="Slots Co", slug=f"slots-{tag}")
        owner = User(email=f"slots-{tag}@example.com", hashed_password="x", is_active=True)
        plan = Plan(
            code=f"slots-{tag}",
            name="Slots",
            price=Decimal("0.00"),
            currency="EGP",
            interval=BillingInterval.MONTHLY,
            limits={"channel_connections": slots},
            allowed_channel_types=TYPES,
        )
        session.add_all([tenant, owner, plan])
        await session.flush()
        session.add(
            Membership(
                tenant_id=tenant.id,
                user_id=owner.id,
                role=TenantRole.TENANT_OWNER,
                status=MembershipStatus.ACTIVE,
            )
        )
        version = await PlanCatalog(session).current_version(plan)
        assert version is not None
        subscription = Subscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            plan_version_id=version.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            billing_anchor_at=now,
        )
        session.add(subscription)
        await session.commit()
        return World(
            tenant_id=tenant.id,
            owner_id=owner.id,
            plan_id=plan.id,
            subscription_id=subscription.id,
            period_end=subscription.current_period_end,
        )


@pytest_asyncio.fixture
async def worlds(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[list[World]]:
    built: list[World] = []
    try:
        yield built
    finally:
        async with maker() as session:
            ids = [world.tenant_id for world in built]
            if ids:
                await erase_ledger(session, ids)
                # Audit rows keep the platform's history by SET NULL; a test's
                # must not outlive it as platform-wide rows another suite counts.
                await session.execute(delete(AuditLog).where(AuditLog.tenant_id.in_(ids)))
                await session.execute(delete(Subscription).where(Subscription.tenant_id.in_(ids)))
                await session.execute(delete(Tenant).where(Tenant.id.in_(ids)))
                await session.execute(
                    delete(Plan).where(Plan.id.in_([world.plan_id for world in built]))
                )
                await session.execute(
                    delete(User).where(User.id.in_([world.owner_id for world in built]))
                )
            await session.commit()


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


async def _advisory_waiters(maker: async_sessionmaker[AsyncSession]) -> int:
    """Connections to this database blocked on an advisory lock right now."""
    async with maker() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                )
            )
            or 0
        )


class GuardGate:
    """Holds every authoritative capacity decision until all racers are provably at it.

    Armed for `expected` activations. Each arrives at `reserve_or_refuse` before
    any of them asks for the workspace's `channel_connections` lock; one the guard
    allows then waits, still holding the lock, until every undecided racer is
    either at the gate too or blocked on the advisory lock. With the lock the
    racers decide one after another, each counting the commits before it;
    without it (M-E11) every one of them judges the same committed count.
    """

    def __init__(self, maker: async_sessionmaker[AsyncSession], expected: int) -> None:
        self.maker = maker
        self.expected = expected
        self.arrived = 0
        self.at_gate = 0
        self.decided = 0

    async def arrive(self) -> bool:
        if self.arrived >= self.expected:
            return False
        self.arrived += 1

        async def everyone_has_arrived() -> bool:
            return self.arrived >= self.expected

        await _poll(everyone_has_arrived, what="every activation to reach the guard")
        return True

    async def hold(self) -> None:
        self.at_gate += 1

        async def everyone_is_here() -> bool:
            undecided = self.expected - self.decided
            return self.at_gate + await _advisory_waiters(self.maker) >= undecided

        await _poll(everyone_is_here, what="every undecided activation to block on the lock")
        self.at_gate -= 1
        self.decided += 1


@pytest.fixture
def guard_gate(
    monkeypatch: pytest.MonkeyPatch, maker: async_sessionmaker[AsyncSession]
) -> Callable[[int], GuardGate]:
    """Install a `GuardGate` around `ChannelCapacityGuard.reserve_or_refuse`."""
    original = ChannelCapacityGuard.reserve_or_refuse

    def install(expected: int) -> GuardGate:
        gate = GuardGate(maker, expected)

        async def gated(self: ChannelCapacityGuard, channel: Channel) -> ChannelSlot:
            if not await gate.arrive():
                return await original(self, channel)
            try:
                slot = await original(self, channel)
            except ConflictError:
                gate.decided += 1
                raise
            await gate.hold()
            return slot

        monkeypatch.setattr(ChannelCapacityGuard, "reserve_or_refuse", gated)
        return gate

    return install


def _registry() -> ChannelRegistry:
    adapters: dict[Channel, ChannelAdapter] = {Channel.WHATSAPP: WhatsAppAdapter()}
    for channel in (Channel.INSTAGRAM, Channel.MESSENGER):
        adapters[channel] = cast(ChannelAdapter, SyntheticAdapter(channel))
    return ChannelRegistry(adapters)


async def _together(*calls: Callable[[], Awaitable[str]]) -> list[str]:
    """Start every call at one barrier, each on its own connection."""
    barrier = asyncio.Barrier(len(calls))

    async def run(call: Callable[[], Awaitable[str]]) -> str:
        await barrier.wait()
        return await call()

    return list(await asyncio.gather(*(run(call) for call in calls)))


async def _active(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ChannelConnection)
            .where(ChannelConnection.tenant_id == tenant_id)
            .where(ChannelConnection.status == ConnectionStatus.ACTIVE)
            .where(ChannelConnection.released_at.is_(None))
        )
        or 0
    )


def _connect(
    maker: async_sessionmaker[AsyncSession], world: World, channel: Channel, *, capacity: int
) -> Callable[[], Awaitable[str]]:
    """Connect one connection on `channel`; record the active count at its commit."""

    async def run() -> str:
        async with maker() as session:
            try:
                if channel is Channel.WHATSAPP:
                    number = f"9{uuid.uuid4().int % 10**11:011d}"
                    await WhatsAppAccountService(
                        session=session,
                        ownership=FakeOwnershipVerifier().owns(number),
                    ).connect(
                        tenant_id=world.tenant_id, phone_number_id=number, access_token="EAAG-race"
                    )
                else:
                    await ChannelConnectionService(
                        session,
                        tenant_id=world.tenant_id,
                        default_plan_code=None,
                        registry=_registry(),
                    ).connect(channel=channel, external_account_id=f"race-{uuid.uuid4().hex}")
            except ChannelCapacityExceededError:
                await session.rollback()
                return f"{channel.value}:refused"
            # Still inside the transaction that holds the guard's lock: this is
            # exactly how many are active the moment it commits.
            world.commits.append((channel.value, await _active(session, world.tenant_id), capacity))
            await session.commit()
            return f"{channel.value}:created"

    return run


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    world: World,
    count: int,
    *,
    status: ConnectionStatus = ConnectionStatus.ACTIVE,
) -> list[uuid.UUID]:
    """`count` Instagram connections written directly, as an earlier connect left them."""
    ids = []
    async with maker() as session:
        for _ in range(count):
            connection = ChannelConnection(
                id=uuid.uuid4(),
                tenant_id=world.tenant_id,
                channel=Channel.INSTAGRAM,
                external_account_id=f"seed-{uuid.uuid4().hex}",
                status=status,
                ownership_started_at=datetime.now(UTC) - timedelta(days=1),
            )
            session.add(connection)
            ids.append(connection.id)
        await session.commit()
    return ids


async def _final(maker: async_sessionmaker[AsyncSession], world: World) -> int:
    async with maker() as session:
        return await _active(session, world.tenant_id)


# ------------------------------------------------------------------ races


async def test_ten_whatsapp_connects_against_one_free_slot_leave_exactly_one(
    maker: async_sessionmaker[AsyncSession],
    worlds: list[World],
    guard_gate: Callable[[int], GuardGate],
) -> None:
    """Capacity 3, two active, ten concurrent WhatsApp claims: one succeeds, nine are 409.

    M-E11's killer: all ten are at the guard's decision together.
    """
    world = await _world(maker, slots=3)
    worlds.append(world)
    await _seed(maker, world, 2)
    gate = guard_gate(10)

    outcomes = await _together(
        *(_connect(maker, world, Channel.WHATSAPP, capacity=3) for _ in range(10))
    )

    assert gate.decided == 10, "every claim reached the guard's decision"
    assert outcomes.count("whatsapp:created") == 1, outcomes
    assert outcomes.count("whatsapp:refused") == 9, outcomes
    assert await _final(maker, world) == 3
    assert all(active <= capacity for _, active, capacity in world.commits)


async def test_ten_connects_of_two_channels_against_one_free_slot_leave_exactly_one(
    maker: async_sessionmaker[AsyncSession],
    worlds: list[World],
    guard_gate: Callable[[int], GuardGate],
) -> None:
    world = await _world(maker, slots=3)
    worlds.append(world)
    await _seed(maker, world, 2)
    gate = guard_gate(10)

    outcomes = await _together(
        *(
            _connect(maker, world, channel, capacity=3)
            for channel in [Channel.INSTAGRAM, Channel.MESSENGER] * 5
        )
    )

    assert gate.decided == 10
    created = [outcome for outcome in outcomes if outcome.endswith(":created")]
    assert len(created) == 1, outcomes
    assert await _final(maker, world) == 3
    assert all(active <= capacity for _, active, capacity in world.commits)


async def test_a_disable_racing_connects_never_puts_more_than_capacity_active(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    """Capacity 3, three active; one is disabled while five connects race."""
    world = await _world(maker, slots=3)
    worlds.append(world)
    seeded = await _seed(maker, world, 3)

    async def disable() -> str:
        async with maker() as session:
            await ChannelConnectionService(
                session, tenant_id=world.tenant_id, default_plan_code=None, registry=_registry()
            ).disable(seeded[0])
            await session.commit()
            return "disabled"

    outcomes = await _together(
        disable, *(_connect(maker, world, Channel.MESSENGER, capacity=3) for _ in range(5))
    )

    assert "disabled" in outcomes
    assert outcomes.count("messenger:created") <= 1, outcomes
    assert await _final(maker, world) <= 3
    assert all(active <= capacity for _, active, capacity in world.commits)


async def test_two_enables_against_one_slot_leave_exactly_one(
    maker: async_sessionmaker[AsyncSession],
    worlds: list[World],
    guard_gate: Callable[[int], GuardGate],
) -> None:
    world = await _world(maker, slots=1)
    worlds.append(world)
    disabled = await _seed(maker, world, 2, status=ConnectionStatus.DISABLED)
    gate = guard_gate(2)

    def enable(connection_id: uuid.UUID) -> Callable[[], Awaitable[str]]:
        async def run() -> str:
            async with maker() as session:
                try:
                    await ChannelConnectionService(
                        session,
                        tenant_id=world.tenant_id,
                        default_plan_code=None,
                        registry=_registry(),
                    ).enable(connection_id)
                except ChannelCapacityExceededError:
                    await session.rollback()
                    return "refused"
                world.commits.append(("enable", await _active(session, world.tenant_id), 1))
                await session.commit()
                return "enabled"

        return run

    outcomes = await _together(enable(disabled[0]), enable(disabled[1]))

    assert gate.decided == 2
    assert sorted(outcomes) == ["enabled", "refused"]
    assert await _final(maker, world) == 1


async def test_typed_and_general_slots_hold_under_concurrency(
    maker: async_sessionmaker[AsyncSession],
    worlds: list[World],
    guard_gate: Callable[[int], GuardGate],
) -> None:
    """1 general + 1 Instagram slot; five Instagram and five WhatsApp race.

    Whatever the order, exactly two connect: at most one WhatsApp (only the
    general slot can take one) and at most two Instagram (its typed slot, then
    the general one).
    """
    world = await _world(maker, slots=1)
    worlds.append(world)
    await _grant(maker, world, quantity=1, channel=Channel.INSTAGRAM, expires=world.period_end)

    calls = [_connect(maker, world, Channel.INSTAGRAM, capacity=2) for _ in range(5)]
    calls += [_connect(maker, world, Channel.WHATSAPP, capacity=2) for _ in range(5)]
    gate = guard_gate(10)
    outcomes = await _together(*calls)

    assert gate.decided == 10
    assert outcomes.count("whatsapp:created") <= 1, outcomes
    assert outcomes.count("instagram:created") <= 2, outcomes
    assert len([o for o in outcomes if o.endswith(":created")]) == 2, outcomes
    assert await _final(maker, world) == 2


async def test_capacity_expiring_while_connects_race_is_never_exceeded_at_commit(
    maker: async_sessionmaker[AsyncSession], worlds: list[World]
) -> None:
    """Base 1 + a general grant of 2 that expires at T; connects either side of T race."""
    world = await _world(maker, slots=1)
    worlds.append(world)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    await _grant(maker, world, quantity=2, channel=None, expires=expiry)
    before, after = expiry - timedelta(seconds=1), expiry + timedelta(seconds=1)

    def connect_at(at: datetime, label: str, capacity: int) -> Callable[[], Awaitable[str]]:
        async def run() -> str:
            async with maker() as session:
                service = ChannelConnectionService(
                    session, tenant_id=world.tenant_id, default_plan_code=None, registry=_registry()
                )
                # The guard reads the clock it is given: this request lives at `at`.
                original = service._guard

                def guard_at() -> Any:
                    built = original()
                    built._clock = lambda: at
                    return built

                service._guard = guard_at  # type: ignore[method-assign]
                try:
                    await service.connect(
                        channel=Channel.INSTAGRAM, external_account_id=f"exp-{uuid.uuid4().hex}"
                    )
                except ChannelCapacityExceededError:
                    await session.rollback()
                    return f"{label}:refused"
                world.commits.append((label, await _active(session, world.tenant_id), capacity))
                await session.commit()
                return f"{label}:created"

        return run

    outcomes = await _together(
        connect_at(before, "before", 3),
        connect_at(before, "before", 3),
        connect_at(after, "after", 1),
        connect_at(after, "after", 1),
    )

    assert await _final(maker, world) <= 3
    assert outcomes.count("after:created") <= 1, outcomes
    # Each activation fitted the capacity in force at its own moment, at its commit.
    assert all(active <= capacity for _, active, capacity in world.commits), world.commits


async def _grant(
    maker: async_sessionmaker[AsyncSession],
    world: World,
    *,
    quantity: int,
    channel: Channel | None,
    expires: datetime,
) -> None:
    async with maker() as session:
        session.add(
            TopupPurchase(
                tenant_id=world.tenant_id,
                subscription_id=world.subscription_id,
                source=TopupSource.PLATFORM_GRANT,
                product_name="Platform grant",
                entitlement_key=TopupEntitlement.CHANNEL_CONNECTIONS,
                channel_type=channel,
                quantity=quantity,
                unit_price=Decimal("0.00"),
                total_amount=Decimal("0.00"),
                currency="EGP",
                billing_period_start=datetime.now(UTC) - timedelta(days=1),
                billing_period_end=world.period_end,
                expires_at=expires,
                status=TopupStatus.GRANTED,
                granted_at=datetime.now(UTC) - timedelta(minutes=1),
                reason="Race grant.",
            )
        )
        await session.commit()


# --------------------------------------------- capacity reductions (ENT-14)


async def _open_reduction(
    maker: async_sessionmaker[AsyncSession], world: World, *, active: int, ended_ago: timedelta
) -> list[uuid.UUID]:
    """`active` connections on a one-slot plan, and a reduction whose grace ended `ended_ago`."""
    ids = []
    start = datetime.now(UTC) - timedelta(days=60)
    async with maker() as session:
        for index in range(active):
            connection = ChannelConnection(
                id=uuid.uuid4(),
                tenant_id=world.tenant_id,
                channel=Channel.INSTAGRAM,
                external_account_id=f"reduce-{uuid.uuid4().hex}",
                status=ConnectionStatus.ACTIVE,
                ownership_started_at=start + timedelta(hours=index),
            )
            session.add(connection)
            ids.append(connection.id)
        await session.flush()
        grace = timedelta(days=7)
        opened = await ChannelCapacityReductions(
            session, tenant_id=world.tenant_id, grace=grace
        ).boundary(
            cause=CapacityReductionCause.DOWNGRADE,
            now=datetime.now(UTC) - grace - ended_ago,
        )
        assert opened is not None and opened.status is CapacityReductionStatus.PENDING_SELECTION
        await session.commit()
    return ids


def _billing_worker(database_url: str) -> tuple[BillingWorker, Database]:
    settings = Settings(_env_file=None, environment="test", database_url=database_url)
    database = Database(settings)
    return BillingWorker(database=database, settings=settings), database


async def _disables(maker: async_sessionmaker[AsyncSession], world: World) -> list[uuid.UUID]:
    async with maker() as session:
        rows = await session.scalars(
            select(AuditLog.target_id)
            .where(AuditLog.tenant_id == world.tenant_id)
            .where(AuditLog.action == AuditAction.CHANNEL_CONNECTION_DISABLED)
        )
        return [row for row in rows if row is not None]


async def test_two_billing_workers_at_the_grace_end_disable_once(
    maker: async_sessionmaker[AsyncSession], worlds: list[World], prepared_database: str
) -> None:
    """One set of disables and one audit entry per connection, however many workers run."""
    world = await _world(maker, slots=1)
    worlds.append(world)
    oldest, *newer = await _open_reduction(maker, world, active=3, ended_ago=timedelta(minutes=1))
    first, first_db = _billing_worker(prepared_database)
    second, second_db = _billing_worker(prepared_database)
    now = datetime.now(UTC)

    async def sweep(worker: BillingWorker) -> str:
        return str(await worker.run_once(now=now))

    try:
        await _together(lambda: sweep(first), lambda: sweep(second))
    finally:
        await first_db.dispose()
        await second_db.dispose()

    assert await _final(maker, world) == 1
    disabled = await _disables(maker, world)
    assert sorted(map(str, disabled)) == sorted(map(str, newer)), "each disabled exactly once"
    async with maker() as session:
        reduction = await session.scalar(
            select(ChannelCapacityReduction).where(
                ChannelCapacityReduction.tenant_id == world.tenant_id
            )
        )
        resolutions = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.tenant_id == world.tenant_id)
            .where(AuditLog.action == AuditAction.CHANNEL_CAPACITY_REDUCTION_RESOLVED)
        )
        kept = await session.get(ChannelConnection, oldest)
    assert reduction is not None
    assert reduction.status is CapacityReductionStatus.RESOLVED_AUTOMATICALLY
    assert resolutions == 1
    assert kept is not None and kept.status is ConnectionStatus.ACTIVE


async def test_an_owner_choosing_while_the_fallback_runs_resolves_once(
    maker: async_sessionmaker[AsyncSession], worlds: list[World], prepared_database: str
) -> None:
    """Whichever wins, the reduction is resolved once and nothing is disabled twice."""
    world = await _world(maker, slots=1)
    worlds.append(world)
    ids = await _open_reduction(maker, world, active=3, ended_ago=timedelta(minutes=1))
    newest = ids[-1]
    worker, database = _billing_worker(prepared_database)
    now = datetime.now(UTC)

    async def owner_keeps_newest() -> str:
        async with maker() as session:
            owner = await session.get(User, world.owner_id)
            assert owner is not None
            reductions = ChannelCapacityReductions(session, tenant_id=world.tenant_id)
            reduction = await reductions.open_reduction()
            if reduction is None:
                return "closed"
            try:
                await reductions.select([newest], expected_revision=reduction.revision, actor=owner)
            except Exception as error:  # the fallback won: stale revision or nothing open
                await session.rollback()
                return type(error).__name__
            await session.commit()
            return "owner"

    async def fallback() -> str:
        return str(await worker.run_once(now=now))

    try:
        outcomes = await _together(owner_keeps_newest, fallback)
    finally:
        await database.dispose()

    assert await _final(maker, world) == 1
    disabled = await _disables(maker, world)
    assert len(disabled) == 2 and len(set(disabled)) == 2, outcomes
    async with maker() as session:
        resolutions = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.tenant_id == world.tenant_id)
            .where(AuditLog.action == AuditAction.CHANNEL_CAPACITY_REDUCTION_RESOLVED)
        )
    assert resolutions == 1, outcomes
