"""Two, four and eight real campaign workers never send one person one campaign twice (CMP-01).

Ported from the audit's P01 and P01b probes (CAMPAIGNS_OUTBOUND_MESSAGING_AUDIT.md
section 15). Every worker here is a real `CampaignWorker` with its own `Database`
and its own commits, and "sent" is counted at the Meta transport
(`tests/integration/campaign_harness.py`). On the audited head these scenarios
put 3/40, 26/40 and 23/250 people's copies on their phones twice; the
per-recipient claim and the lease token (ADR-134) make each of them zero.

Time is scaled, not the mechanism: the lease is fractions of a second and Meta
answers in milliseconds, so a batch outlasts a lease exactly as fifty sends of
up to ten seconds outlast three hundred.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.db.session import Database
from tests.integration.campaign_harness import (
    FakeMeta,
    compose,
    erase,
    ledger,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration


async def _drain(*workers_: object, rounds: int = 40, pause: float = 0.05) -> None:
    """Sweep until nothing is due, every worker in turn, the way replicas would."""
    for _ in range(rounds):
        handled = 0
        for each in workers_:
            handled += await each.run_once()  # type: ignore[attr-defined]
        if handled == 0:
            return
        await asyncio.sleep(pause)


async def _assert_once(
    meta: FakeMeta, database: Database, campaigns: list[uuid.UUID], *, people: int
) -> None:
    """Zero second copies, counted at the transport, and the rows agree."""
    assert meta.received_twice() == 0, meta.per_recipient().most_common(5)
    assert len(meta.requests) == people
    sent = meters = 0
    for campaign_id in campaigns:
        row = await ledger(database, campaign_id)
        assert row.conversations_with_two == 0
        assert row.meters == row.sent, row
        sent += row.sent
        meters += row.meters
    assert sent == people
    assert meters == people


async def test_a_campaign_leased_but_not_started_is_not_claimed_again(
    prepared_database: str,
) -> None:
    """P01 `scheduled_gap`: replica B sweeps while A works campaign 1 of 2.

    Campaign 2 is committed as leased by A and is still `scheduled`, because
    A has not started it. The scheduled branch of the claim ignored the lease,
    so B took it too and both sent it (audit: 3/40 twice, 43 requests).
    """
    meta = FakeMeta(latency=0.02)
    a, db_a = worker(prepared_database, meta, name="a")
    b, db_b = worker(prepared_database, meta, name="b")
    try:
        await quiesce(db_a)
        first = await seed(db_a, recipients=20)
        second = await seed(db_a, recipients=20)
        campaigns = [await compose(db_a, first), await compose(db_a, second)]

        started = asyncio.Event()

        async def on_request(payload: dict[str, object], n: int) -> None:
            if n == 2:
                started.set()

        meta.on_request = on_request
        running = asyncio.create_task(a.run_once())
        await started.wait()
        assert await b.run_once() == 0
        await running
        await _drain(a, b)

        await _assert_once(meta, db_a, campaigns, people=40)
    finally:
        await quiesce(db_a)
        await erase(db_a)
        await db_a.dispose()
        await db_b.dispose()


async def test_a_batch_longer_than_its_lease_is_not_shared(prepared_database: str) -> None:
    """P01 `lease`: one running campaign whose batch outlasts the lease.

    The lease is renewed before every recipient, so it has to outlast one send,
    not forty (audit: lease 3 s vs a 10 s batch, 26/40 twice, 66 requests).
    """
    meta = FakeMeta(latency=0.05)
    a, db_a = worker(prepared_database, meta, name="a", lease_seconds=0.5)
    b, db_b = worker(prepared_database, meta, name="b", lease_seconds=0.5)
    try:
        await quiesce(db_a)
        workspace = await seed(db_a, recipients=40)
        campaign = await compose(db_a, workspace)

        running = asyncio.create_task(a.run_once())
        await asyncio.sleep(1.0)
        await b.run_once()
        await running
        await _drain(a, b)

        await _assert_once(meta, db_a, [campaign], people=40)
    finally:
        await quiesce(db_a)
        await erase(db_a)
        await db_a.dispose()
        await db_b.dispose()


async def test_a_worker_whose_lease_was_taken_over_sends_nothing_more(
    prepared_database: str,
) -> None:
    """A stalls on one send for longer than its lease; B takes the campaign over.

    B's claim carries a new token, so A's next renewal fails and A stops after
    the one send it had in flight. That send was linked to its recipient before
    A asked Meta (ADR-093 TX1), so B finds it linked and abandons it rather
    than sending it again: nobody gets two copies, whoever finishes.
    """
    meta = FakeMeta(latency=0.01, stall={3: 2.0})
    a, db_a = worker(prepared_database, meta, name="a", lease_seconds=0.5)
    b, db_b = worker(prepared_database, meta, name="b", lease_seconds=0.5)
    try:
        await quiesce(db_a)
        workspace = await seed(db_a, recipients=20)
        campaign = await compose(db_a, workspace)

        stalled = asyncio.Event()

        async def on_request(payload: dict[str, object], n: int) -> None:
            if n == 3:
                stalled.set()

        meta.on_request = on_request
        running = asyncio.create_task(a.run_once())
        await stalled.wait()
        # A renewed its lease just before this send; it runs out at 0.5 s.
        await asyncio.sleep(0.9)
        assert await b.run_once() == 1, "B should have taken the expired lease over"
        await running
        await _drain(a, b)

        assert meta.received_twice() == 0, meta.per_recipient().most_common(3)
        # A's two sends, and the one it was stalled on - nothing after B took over.
        assert meta.by_worker()["a"] == 3
        row = await ledger(db_a, campaign)
        assert row.conversations_with_two == 0
        assert row.meters == row.sent
        assert len(meta.requests) == 20
    finally:
        await quiesce(db_a)
        await erase(db_a)
        await db_a.dispose()
        await db_b.dispose()


async def test_the_shipped_shape_scaled_in_time_sends_everyone_once(
    prepared_database: str,
) -> None:
    """P01b, scaled in time only: five campaigns of fifty, claim limit five.

    A sweep leases all five and works them in turn, so the leases of the later
    four run out while the first is sending; replica B re-takes them with new
    tokens and A's `claim_by_id` refuses each one it no longer owns (audit, with
    the shipped constants and a 1.3 s Meta: 23/250 twice, 273 requests).
    """
    meta = FakeMeta(latency=0.01)
    a, db_a = worker(prepared_database, meta, name="a", lease_seconds=0.3)
    b, db_b = worker(prepared_database, meta, name="b", lease_seconds=0.3)
    try:
        await quiesce(db_a)
        campaigns = []
        for _ in range(5):
            workspace = await seed(db_a, recipients=50)
            campaigns.append(await compose(db_a, workspace, rate=600))

        running = asyncio.create_task(a.run_once())
        await asyncio.sleep(0.5)
        taken = await b.run_once()
        await running
        await _drain(a, b)

        assert taken >= 1, "B should have re-taken leases that ran out"
        await _assert_once(meta, db_a, campaigns, people=250)
    finally:
        await quiesce(db_a)
        await erase(db_a)
        await db_a.dispose()
        await db_b.dispose()


@pytest.mark.parametrize("replicas", [2, 4, 8])
async def test_many_workers_on_many_campaigns_send_everyone_once(
    prepared_database: str, replicas: int
) -> None:
    """Replicas sweeping in parallel, with leases shorter than a batch, eight campaigns."""
    meta = FakeMeta(latency=0.005)
    pool = [
        worker(prepared_database, meta, name=f"w{index}", lease_seconds=0.2, claim_limit=2)
        for index in range(replicas)
    ]
    first_db = pool[0][1]
    try:
        await quiesce(first_db)
        campaigns = []
        for _ in range(8):
            workspace = await seed(first_db, recipients=30)
            campaigns.append(await compose(first_db, workspace, rate=600))

        async def replica(index: int) -> None:
            each, _ = pool[index]
            await asyncio.sleep(0.03 * index)
            for _ in range(60):
                await each.run_once()
                await asyncio.sleep(0.02)

        await asyncio.gather(*(replica(index) for index in range(replicas)))
        await _drain(*(each for each, _ in pool))

        await _assert_once(meta, first_db, campaigns, people=240)
        assert len(meta.by_worker()) >= min(2, replicas)
    finally:
        await quiesce(first_db)
        await erase(first_db)
        for _, database in pool:
            await database.dispose()
