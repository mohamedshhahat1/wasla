"""Worker selection and the shutdown path.

`main` itself is not exercised here — it builds real infrastructure and installs
signal handlers, which belongs to the container check rather than to a unit
test. What is worth pinning down is which workers a given `WORKER_KINDS` starts,
and that stopping actually stops them.
"""

from __future__ import annotations

import asyncio

import pytest

from app.workers.ai_worker import AgentWorker
from app.workers.billing_worker import BillingWorker
from app.workers.campaign_worker import CampaignWorker
from app.workers.follow_up_worker import FollowUpWorker
from app.workers.ingestion_worker import IngestionWorker
from app.workers.media_worker import MediaWorker
from app.workers.recovery import RecoveryWorker
from app.workers.retention_worker import RetentionWorker
from app.workers.runner import (
    AGENT,
    ALL_KINDS,
    CAMPAIGN,
    FOLLOW_UP,
    INGESTION,
    MEDIA,
    build_workers,
    run,
    selected_kinds,
)


class FakeRedis:
    """Stands in for RedisClient; the workers only reach for `.client`."""

    @property
    def client(self):
        return object()


class SpyWorker:
    """Records that it ran, and stops when asked."""

    def __init__(self, *, forever: bool = True) -> None:
        self.started = False
        self.stopped = False
        self._forever = forever
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        self.started = True
        if self._forever:
            await self._stopping.wait()

    def stop(self) -> None:
        self.stopped = True
        self._stopping.set()


def test_no_setting_runs_every_worker():
    """The safe default: a deployment that forgets the variable still works."""
    assert selected_kinds("") == ALL_KINDS
    assert selected_kinds("   ") == ALL_KINDS


def test_a_subset_can_be_selected():
    assert selected_kinds("agent") == (AGENT,)
    assert selected_kinds("ingestion,follow_up") == (INGESTION, FOLLOW_UP)


def test_selection_is_normalised():
    """Whitespace, case and duplicates must not change what starts."""
    assert selected_kinds(" AGENT , agent,  Follow_Up ") == (AGENT, FOLLOW_UP)


def test_the_order_is_stable_however_it_was_written():
    """So logs read the same across deployments."""
    assert selected_kinds("follow_up,agent") == selected_kinds("agent,follow_up")


def test_campaigns_can_be_run_on_their_own():
    """Sending a broadcast is bandwidth against Meta, not inference.

    A workspace mid-campaign is the case that most wants its own replica, and
    that is this variable rather than another image.
    """
    assert selected_kinds("campaign") == (CAMPAIGN,)


def test_media_is_ordered_before_the_agent_it_feeds():
    """The order work actually flows in: a file is read, then answered."""
    assert selected_kinds("agent,media") == (MEDIA, AGENT)


def test_an_unknown_kind_is_refused():
    """A typo would otherwise start a process that quietly does nothing.

    The symptom — work piling up in a queue nobody is reading — shows up far
    from the cause, so this fails at startup instead.
    """
    with pytest.raises(ValueError, match="Unknown worker kinds: agnt"):
        selected_kinds("agent,agnt")


def test_the_error_names_the_valid_choices():
    with pytest.raises(ValueError, match="media, agent, ingestion, follow_up, campaign"):
        selected_kinds("nonsense")


def test_media_can_be_run_on_its_own():
    """Downloading files is a different shape of work from inference.

    A deployment that wants to scale them apart does it with this variable
    rather than a second image.
    """
    assert selected_kinds("media") == (MEDIA,)


def test_each_kind_builds_its_own_worker(settings):
    workers = build_workers(
        kinds=ALL_KINDS,
        database=object(),  # type: ignore[arg-type]
        redis=FakeRedis(),  # type: ignore[arg-type]
        settings=settings,
    )

    assert [type(worker) for worker in workers] == [
        MediaWorker,
        AgentWorker,
        IngestionWorker,
        FollowUpWorker,
        CampaignWorker,
        BillingWorker,
        # Email is absent because this fixture leaves it disabled; recovery is
        # present because it is not optional. It reclaims what *other*
        # processes were holding when they died, so a deployment that runs it
        # nowhere has no crash recovery at all (ADR-074).
        RecoveryWorker,
        # Retention is present by the same rule: a deployment that had to name
        # it is one where forgetting to is a media store that grows for ever
        # with nothing to say so. It removes nothing until MEDIA_RETENTION_DAYS
        # is set, but its reconciliation pass still has to run (ADR-078).
        RetentionWorker,
    ]


def test_only_the_selected_kinds_are_built(settings):
    workers = build_workers(
        kinds=(FOLLOW_UP,),
        database=object(),  # type: ignore[arg-type]
        redis=FakeRedis(),  # type: ignore[arg-type]
        settings=settings,
    )

    assert [type(worker) for worker in workers] == [FollowUpWorker]


def test_selecting_nothing_builds_nothing(settings):
    assert (
        build_workers(
            kinds=(),
            database=object(),  # type: ignore[arg-type]
            redis=FakeRedis(),  # type: ignore[arg-type]
            settings=settings,
        )
        == []
    )


async def test_running_no_workers_returns_rather_than_hanging():
    """A misconfigured process should exit, not sit there looking healthy."""
    await asyncio.wait_for(run([]), timeout=2)


async def test_every_worker_runs_concurrently():
    workers = [SpyWorker(), SpyWorker(), SpyWorker()]
    task = asyncio.create_task(run(workers))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)

    assert all(worker.started for worker in workers)

    for worker in workers:
        worker.stop()
    await asyncio.wait_for(task, timeout=2)


async def test_stopping_every_worker_ends_the_run():
    workers = [SpyWorker(), SpyWorker()]
    task = asyncio.create_task(run(workers))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)

    for worker in workers:
        worker.stop()

    await asyncio.wait_for(task, timeout=2)
    assert all(worker.stopped for worker in workers)


async def test_a_worker_that_returns_does_not_cancel_its_siblings():
    """Gather, not a task group.

    A task group would cancel the others the instant one finished, and
    cancelling a worker mid-job is what the graceful path exists to avoid.
    """
    finishing = SpyWorker(forever=False)
    lasting = SpyWorker()
    task = asyncio.create_task(run([finishing, lasting]))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)

    assert finishing.started
    assert not lasting.stopped
    assert not task.done()

    lasting.stop()
    await asyncio.wait_for(task, timeout=2)
