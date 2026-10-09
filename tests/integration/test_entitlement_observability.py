"""The entitlement signals reach the exposition with closed labels (section 32, ADR-131).

Each counter is written by the code path that decides it - the capacity guard,
the AI worker's hold and settle, the billing sweep, the reduction flow - and
read back from real Redis; each gauge is rendered by the real `MetricsService`
from the database. No label carries a workspace, connection, contact, product,
invoice or amount: a value outside a metric's domain is `other`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import MetricsRegistry
from app.core.telemetry import (
    REDIS_COUNTERS,
    read_redis_counters,
    record_capacity_disables,
    record_capacity_reduction,
    record_entitlement_refusal,
    set_counter_sink,
)
from app.db.models.billing import LimitKey, SubscriptionStatus
from app.db.models.channel import Channel
from app.repositories.billing_repository import SubscriptionRepository
from app.repositories.entitlement_census import ChannelCapacityCensus
from app.services.ai_turn_charge import AITurnCharge, SettleResult, record_settlement
from app.services.channel_capacity import (
    ChannelCapacityExceededError,
    ChannelCapacityGuard,
    ChannelTypeNotAllowedError,
)
from app.services.metrics_service import MetricsService
from app.workers.billing_worker import BillingWorker
from tests.integration.ai_harness import (
    DEFAULT_REPLY,
    FakeProviders,
    TurnRunner,
    scripted,
    text_response,
)
from tests.integration.test_ai_turn_charging import _dead_hold, _failing
from tests.integration.test_capacity_reductions import (
    _boundary,
    _reductions,
    _seven_on_business_with_pro_scheduled,
)
from tests.integration.test_channel_topups import _catalogue, _number
from tests.integration.topup_harness import base_now, workspace
from tests.redis_url import redis_url_for

pytestmark = pytest.mark.integration

# A database of its own on the suite's Redis.
REDIS_URL = redis_url_for(8)
TURNS = LimitKey.PERIOD_AI_TURNS.value

Series = dict[tuple[tuple[str, str], ...], float]


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def sink(redis: Redis) -> Iterator[Redis]:
    set_counter_sink(redis)
    try:
        yield redis
    finally:
        set_counter_sink(None)


async def _series(redis: Redis, metric: str) -> Series:
    counters = await read_redis_counters(redis)
    return {tuple(sorted(labels.items())): value for labels, value in counters.get(metric, [])}


def _labels(**labels: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


async def test_labels_outside_a_domain_are_other(sink: Redis) -> None:
    await record_entitlement_refusal("tenant-1234", "because")
    await record_capacity_reduction("a-workspace", "a-connection-id")
    await record_capacity_disables("someone@example.com", 2)

    assert await _series(sink, "wasla_entitlement_refusals_total") == {
        _labels(key="other", reason="other"): 1.0
    }
    assert await _series(sink, "wasla_channel_capacity_reductions_total") == {
        _labels(cause="other", resolution="other"): 1.0
    }
    assert await _series(sink, "wasla_channel_capacity_reduction_disables_total") == {
        _labels(actor="other"): 2.0
    }


async def test_the_guard_counts_each_refusal_once(db_session: AsyncSession, sink: Redis) -> None:
    """A pre-check refusal, and an authoritative one, each one sample."""
    now = base_now()
    await _catalogue(db_session)
    tenant, _owner, subscription = await workspace(db_session, now=now, plan_code="starter")
    await _number(db_session, tenant)
    guard = ChannelCapacityGuard(db_session, tenant_id=tenant.id, default_plan_code="starter")

    with pytest.raises(ChannelCapacityExceededError):
        await guard.precheck(Channel.WHATSAPP)
    with pytest.raises(ChannelCapacityExceededError):
        await guard.reserve_or_refuse(Channel.INSTAGRAM)

    assert await _series(sink, "wasla_entitlement_refusals_total") == {
        _labels(key="channel_connections", reason="capacity_exceeded"): 2.0
    }


async def test_a_type_refusal_is_counted_under_allowed_channel_types(
    db_session: AsyncSession, sink: Redis
) -> None:
    now = base_now()
    await _catalogue(db_session)
    tenant, _owner, _ = await workspace(db_session, now=now, plan_code="pro")
    guard = ChannelCapacityGuard(db_session, tenant_id=tenant.id, default_plan_code="starter")

    with pytest.raises(ChannelTypeNotAllowedError):
        await guard.precheck(Channel.TELEGRAM)

    assert await _series(sink, "wasla_entitlement_refusals_total") == {
        _labels(key="allowed_channel_types", reason="type_not_allowed"): 1.0
    }


async def test_ai_turns_count_holds_charges_releases_and_the_quota(
    ai_turns: TurnRunner, ai_providers: FakeProviders, sink: Redis
) -> None:
    """Through the real worker: a failure releases, an answer charges, the rest is refused."""
    await ai_turns.plan({TURNS: 1})
    workspace_ = await ai_turns.workspace()
    ai_providers.agent = _failing
    failed, ids = await ai_turns.write(workspace_, ["hello"])
    await ai_turns.answer(workspace_, failed, ids[0])
    ai_providers.agent = scripted(text_response(DEFAULT_REPLY))
    answered, more = await ai_turns.write(workspace_, ["anyone?"], wa_id="201555000444")
    await ai_turns.answer(workspace_, answered, more[0])
    refused, last = await ai_turns.write(workspace_, ["still there?"], wa_id="201555000445")
    await ai_turns.answer(workspace_, refused, last[0])

    assert await _series(sink, "wasla_ai_turn_charge_total") == {
        _labels(outcome="held"): 2.0,
        _labels(outcome="released"): 1.0,
        _labels(outcome="charged"): 1.0,
    }
    assert await _series(sink, "wasla_entitlement_refusals_total") == {
        _labels(key="period_ai_turns", reason="quota_exhausted"): 1.0
    }


async def test_an_expired_hold_and_its_late_charge_are_counted(
    ai_turns: TurnRunner, ai_providers: FakeProviders, sink: Redis
) -> None:
    await ai_turns.plan({TURNS: 5})
    workspace_ = await ai_turns.workspace()
    ttl = timedelta(seconds=ai_turns.settings.ai_turn_hold_ttl_seconds)
    now = datetime.now(UTC)
    held_at = now - ttl - timedelta(minutes=1)
    if held_at.month != now.month:  # pragma: no cover - the first minutes of a month
        pytest.skip("an expired hold in the previous month is the cycle rule's test")
    dead = await _dead_hold(ai_turns, workspace_, held_at=held_at)

    await BillingWorker(database=ai_turns.database, settings=ai_turns.settings).run_once(now=now)
    async with ai_turns.database.session() as session:
        late = await AITurnCharge(session, tenant_id=workspace_.tenant_id).settle(
            agent_turn_id=dead, chargeable=True
        )
    await record_settlement(late)

    assert late is SettleResult.LATE_CHARGE
    assert await _series(sink, "wasla_ai_turn_charge_total") == {
        _labels(outcome="hold_expired"): 1.0,
        _labels(outcome="late_charge"): 1.0,
    }


async def test_a_reduction_counts_its_opening_its_resolution_and_its_disables(
    db_session: AsyncSession, sink: Redis
) -> None:
    """Through the real billing worker at the boundary, then an owner's choice."""
    tenant, owner, ids = await _seven_on_business_with_pro_scheduled(db_session)
    await _boundary(db_session, tenant)
    reductions = _reductions(db_session, tenant)
    reduction = await reductions.open_reduction()
    assert reduction is not None

    await reductions.select(
        [ids[0], ids[2], ids[4]], expected_revision=reduction.revision, actor=owner
    )

    assert await _series(sink, "wasla_channel_capacity_reductions_total") == {
        _labels(cause="downgrade", resolution="opened"): 1.0,
        _labels(cause="downgrade", resolution="resolved_by_owner"): 1.0,
    }
    assert await _series(sink, "wasla_channel_capacity_reduction_disables_total") == {
        _labels(actor="owner"): 4.0
    }


async def test_the_gauges_render_holds_capacity_and_reductions(
    ai_turns: TurnRunner, ai_providers: FakeProviders, redis: Redis
) -> None:
    """The scrape-time gauges, from the database, through the real exposition."""
    await ai_turns.plan({TURNS: 5})
    workspace_ = await ai_turns.workspace()
    ttl = timedelta(seconds=ai_turns.settings.ai_turn_hold_ttl_seconds)
    now = datetime.now(UTC)
    await _dead_hold(ai_turns, workspace_, held_at=now - ttl - timedelta(minutes=1))
    await _dead_hold(ai_turns, workspace_, held_at=now - timedelta(seconds=30))

    body = await MetricsService(
        redis,
        registry=MetricsRegistry(),
        database=ai_turns.database,
        settings=ai_turns.settings,
    ).render(now=now)

    samples = {
        line.split(" ")[0]: float(line.split(" ")[1])
        for line in body.splitlines()
        if line.startswith(("wasla_ai_turn_holds", "wasla_channel_capacity_"))
    }
    assert samples["wasla_ai_turn_holds_open"] >= 2
    assert samples["wasla_ai_turn_holds_past_ttl"] >= 1
    assert "wasla_channel_capacity_over_limit_workspaces" in samples
    assert "wasla_channel_capacity_reductions_open" in samples
    for line in body.splitlines():
        if line.startswith(("wasla_ai_turn_holds", "wasla_channel_capacity_")):
            assert "{" not in line.split(" ")[0], f"an entitlement gauge carries a label: {line}"


async def test_the_over_limit_gauge_counts_a_suspended_workspace_over_its_fallback(
    db_session: AsyncSession, redis: Redis
) -> None:
    """ENT-16: suspended with more connections than the default plan holds."""
    tenant, _owner, _ids = await _seven_on_business_with_pro_scheduled(db_session)
    census_before = await _over_limit(db_session)
    subscription = await SubscriptionRepository(db_session, tenant_id=tenant.id).get()
    assert subscription is not None
    subscription.status = SubscriptionStatus.SUSPENDED
    await db_session.flush()

    assert await _over_limit(db_session) == census_before + 1


async def _over_limit(session: AsyncSession) -> int:
    return await ChannelCapacityCensus(session, default_plan_code="starter").over_limit_workspaces(
        now=datetime.now(UTC)
    )


def test_the_new_counters_are_in_the_catalogue_with_closed_labels() -> None:
    for metric, labels in (
        ("wasla_entitlement_refusals_total", ("key", "reason")),
        ("wasla_ai_turn_charge_total", ("outcome",)),
        ("wasla_channel_capacity_reductions_total", ("cause", "resolution")),
        ("wasla_channel_capacity_reduction_disables_total", ("actor",)),
    ):
        assert REDIS_COUNTERS[metric][1] == labels, metric
