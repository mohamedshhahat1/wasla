"""The worker process.

Until now the workers were classes nothing ran: `AgentWorker`, `IngestionWorker`
and `FollowUpWorker` all had a `run_forever`, and no process ever called it. This
is that process.

They all run concurrently in one event loop rather than as separate containers.
Every one of them is I/O-bound — waiting on Redis, on PostgreSQL, on OpenAI, on
Meta — so they interleave rather than compete, and a single process is markedly
simpler to deploy and to watch. `WORKER_KINDS` selects which run here, so
scaling one apart from the others later needs a different environment variable
rather than a different image.

Shutdown is deliberate. A container is stopped with SIGTERM and killed if it has
not gone in ten seconds or so; a worker that ignores it loses whatever it was
holding. Each loop is asked to stop, and each finishes the job in its hand
before returning.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Iterable, Sequence
from typing import Final, Protocol

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import RedisClient
from app.core.telemetry import set_counter_sink
from app.core.tracing import WORKER_SERVICE_NAME, configure_tracing, shutdown_tracing
from app.db.session import Database
from app.workers.ai_worker import AgentWorker
from app.workers.billing_worker import BillingWorker
from app.workers.campaign_worker import CampaignWorker
from app.workers.email_worker import EmailWorker
from app.workers.follow_up_worker import FollowUpWorker
from app.workers.heartbeat import (
    DEFAULT_INTERVAL_SECONDS,
    Heartbeat,
    all_alive,
)
from app.workers.ingestion_worker import IngestionWorker
from app.workers.media_worker import MediaWorker
from app.workers.queue import LEASE_RENEWAL_FRACTION, ReliableQueue
from app.workers.recovery import RecoveryWorker
from app.workers.retention_worker import RetentionWorker
from app.workers.upload_worker import UploadRecoveryWorker

logger = get_logger(__name__)

AGENT: Final = "agent"
INGESTION: Final = "ingestion"
FOLLOW_UP: Final = "follow_up"
MEDIA: Final = "media"
CAMPAIGN: Final = "campaign"
BILLING: Final = "billing"
EMAIL: Final = "email"
RECOVERY: Final = "recovery"
RETENTION: Final = "retention"
UPLOADS: Final = "uploads"
# Media comes before agent deliberately. It is the order the work flows in -
# a file is read, then answered - and the order the log lines appear in at
# startup, which is worth having match. Billing and email come last: billing
# is the only loop whose period is measured in days, and email consumes what
# the others (and the API) produce, so nothing upstream waits on either.
#
# Recovery is last of all, and it is the only loop that does nothing for its
# own queue: it reclaims what *other* processes were holding when they died,
# so a deployment that runs it nowhere has no crash recovery at all. Leaving
# it out of `WORKER_KINDS` is therefore a decision worth having to make
# explicitly, which is what including it here by default achieves.
# Retention sits beside recovery at the end, and for a related reason: it is
# housekeeping rather than customer-facing work, and it is the only loop that
# deletes anything. A deployment that has not set MEDIA_RETENTION_DAYS runs it
# and it removes nothing, which is the intended default - but the loop still
# has to run, because its reconciliation pass is what finishes rows an earlier
# sweep claimed and could not complete (ADR-078).
#
# Uploads is last, and like recovery it is a loop a deployment must decide to
# leave out rather than forget to include. It finishes object writes that were
# interrupted between the store and the database, and a deployment running it
# nowhere has attachments that are in the bucket and invisible, with nothing
# looking for them (ADR-087).
ALL_KINDS: Final = (
    MEDIA,
    AGENT,
    INGESTION,
    FOLLOW_UP,
    CAMPAIGN,
    BILLING,
    EMAIL,
    RECOVERY,
    RETENTION,
    UPLOADS,
)

KINDS_VARIABLE: Final = "WORKER_KINDS"


class Worker(Protocol):
    """What the runner needs of a worker, and nothing more."""

    async def run_forever(self) -> None: ...

    def stop(self) -> None: ...


def selected_kinds(raw: str | None = None) -> tuple[str, ...]:
    """Which workers this process should run.

    Defaults to all of them. An unrecognised name is refused rather than
    ignored: a typo in a deployment variable would otherwise silently start a
    process that does nothing, and the symptom — work quietly piling up in a
    queue — appears nowhere near the cause.
    """
    value = raw if raw is not None else os.environ.get(KINDS_VARIABLE, "")
    if not value.strip():
        return ALL_KINDS

    kinds = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    unknown = sorted(set(kinds) - set(ALL_KINDS))
    if unknown:
        raise ValueError(
            f"Unknown worker kinds: {', '.join(unknown)}. " f"Choose from: {', '.join(ALL_KINDS)}."
        )
    # Deduplicated, and ordered as ALL_KINDS is, so logs read the same however
    # the variable was written.
    return tuple(kind for kind in ALL_KINDS if kind in kinds)


def build_workers(
    *,
    kinds: Iterable[str],
    database: Database,
    redis: RedisClient,
    settings: Settings,
) -> list[Worker]:
    """Construct the selected workers over shared infrastructure.

    One database pool and one Redis client between them, as in the API process:
    a pool per worker in one process would multiply the connection count for
    no gain.
    """
    workers: list[Worker] = []
    for kind in kinds:
        if kind == AGENT:
            workers.append(AgentWorker(database=database, redis=redis, settings=settings))
        elif kind == INGESTION:
            workers.append(IngestionWorker(database=database, redis=redis, settings=settings))
        elif kind == FOLLOW_UP:
            workers.append(FollowUpWorker(database=database, settings=settings))
        elif kind == MEDIA:
            workers.append(MediaWorker(database=database, redis=redis, settings=settings))
        elif kind == CAMPAIGN:
            workers.append(CampaignWorker(database=database, settings=settings))
        elif kind == BILLING:
            workers.append(BillingWorker(database=database, settings=settings))
        elif kind == EMAIL:
            if not settings.email_enabled:
                # A deployment without email configured runs no email loop and
                # is healthy without one. The heartbeat still beats for the
                # kind - process liveness is what it asserts - and the outbox
                # is empty by construction, because enqueue is a no-op when
                # email is disabled.
                logger.info("worker.email_disabled", extra={"event": "worker.email_disabled"})
                continue
            # Constructing the worker builds the provider, so a container
            # missing RESEND_API_KEY refuses to boot here rather than
            # claiming rows it can never send (ADR-042).
            workers.append(EmailWorker(database=database, settings=settings))
        elif kind == RECOVERY:
            workers.append(RecoveryWorker(redis=redis, settings=settings))
        elif kind == RETENTION:
            workers.append(RetentionWorker(database=database, settings=settings))
        elif kind == UPLOADS:
            workers.append(UploadRecoveryWorker(database=database, settings=settings))
    return workers


def reservation_queues(workers: Iterable[Worker]) -> list[ReliableQueue]:
    """The queues whose leases this process is responsible for renewing.

    Collected from the workers themselves rather than rebuilt, because a lease
    is renewed for the *instance* that holds the reservation - a second
    `AgentQueue` over the same Redis knows nothing about what the first one is
    holding, and renewing from it would extend nothing while looking like it
    had.
    """
    queues: list[ReliableQueue] = []
    for worker in workers:
        queue = getattr(worker, "queue", None)
        if isinstance(queue, ReliableQueue):
            queues.append(queue)
    return queues


async def run(workers: list[Worker]) -> None:
    """Run every worker until one is asked to stop, then stop the rest.

    ``asyncio.gather`` rather than a task group: a task group cancels its
    siblings the instant one raises, and cancelling a worker mid-job is exactly
    what the graceful path is trying to avoid. Each worker already contains its
    own failures, so reaching here means something genuinely unrecoverable.
    """
    if not workers:
        logger.warning("worker.nothing_to_run")
        return

    await asyncio.gather(*(worker.run_forever() for worker in workers))


def _install_signal_handlers(workers: list[Worker]) -> None:
    """Ask every worker to stop when the container is told to go.

    ``add_signal_handler`` is not implemented on Windows, where this runs only
    in development; there, Ctrl-C raises KeyboardInterrupt and `main` handles it.
    """
    loop = asyncio.get_running_loop()

    def request_stop(name: str) -> None:
        logger.info("worker.stop_requested", extra={"signal": name})
        for worker in workers:
            worker.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, request_stop, sig.name)


async def beat_while_running(
    redis: RedisClient,
    kinds: Iterable[str],
    *,
    stopping: asyncio.Event,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Refresh this process's liveness keys until it is asked to stop.

    One task for every kind rather than one per worker loop, and the difference
    is the honest part: this proves the process is up and its event loop is
    scheduling, not that any particular loop is making progress. Anything that
    stops the loop - a crash, a hang, a blocking call in async code - stops the
    beat, which is exactly what a container liveness probe should assert.
    """
    heartbeats = [Heartbeat(redis, kind=kind) for kind in kinds]
    while not stopping.is_set():
        for heartbeat in heartbeats:
            await heartbeat.beat()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=interval_seconds)


async def renew_while_running(
    queues: Sequence[ReliableQueue],
    *,
    stopping: asyncio.Event,
    interval_seconds: float,
) -> None:
    """Keep this process's claims on the jobs it is working on.

    The other half of crash recovery, and the half that stops it doing harm. A
    lease long enough to cover the longest job anybody might run would take an
    hour to notice a dead worker; a lease short enough to notice quickly would
    be stolen out from under a worker still using it. Renewal removes the
    choice: the timeout can be short because a living process keeps saying so.

    A renewal that fails is logged and not retried here - the next tick tries
    again, and if the process is in a state where renewals keep failing then a
    reaper reclaiming its work is the correct outcome rather than a bug.
    """
    if not queues:
        return
    while not stopping.is_set():
        for queue in queues:
            try:
                await queue.renew_leases()
            except Exception:
                logger.warning(
                    "worker.lease_renewal_failed",
                    extra={
                        "event": "worker.lease_renewal_failed",
                        "queue": queue.namespace,
                    },
                )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=interval_seconds)


async def check_health() -> bool:
    """Whether this container's configured loops are all beating.

    Run by the image's HEALTHCHECK. It builds its own Redis client and closes
    it, because it is a separate process from the worker it is asking about.
    """
    settings = get_settings()
    redis = RedisClient(settings)
    try:
        return await all_alive(redis, selected_kinds())
    finally:
        await redis.close()


async def main() -> None:
    """Entry point for the worker container."""
    settings = get_settings()
    configure_logging(settings)
    # Before the infrastructure, so a worker that was asked to trace and cannot
    # refuses to start rather than working untraced. One service name for the
    # whole process, not one per loop: nine loops run here, and which one a
    # span belongs to is `wasla.queue` on the span itself.
    configure_tracing(settings, service_name=WORKER_SERVICE_NAME)

    database = Database(settings)
    redis = RedisClient(settings)
    # Job outcomes and provider calls are counted into Redis, because this
    # process serves no HTTP for a scraper to read (ADR-069). The API renders
    # them.
    set_counter_sink(redis.client if settings.metrics_enabled else None)
    kinds = selected_kinds()
    workers = build_workers(
        kinds=kinds,
        database=database,
        redis=redis,
        settings=settings,
    )

    logger.info(
        "worker.startup",
        extra={
            "event": "worker.startup",
            "environment": settings.environment,
            "kinds": list(kinds),
        },
    )
    _install_signal_handlers(workers)

    stopping = asyncio.Event()
    heartbeat = asyncio.create_task(beat_while_running(redis, kinds, stopping=stopping))
    renewal = asyncio.create_task(
        renew_while_running(
            reservation_queues(workers),
            stopping=stopping,
            interval_seconds=settings.queue_visibility_timeout_seconds * LEASE_RENEWAL_FRACTION,
        )
    )

    try:
        await run(workers)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C in development, where the signal handler above is unavailable.
        for worker in workers:
            worker.stop()
    finally:
        # Stopped before Redis closes, so the beat does not fail on a client
        # that is already going away and log a warning nobody should act on.
        set_counter_sink(None)
        stopping.set()
        heartbeat.cancel()
        renewal.cancel()
        for task in (heartbeat, renewal):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await redis.close()
        await database.dispose()
        # Last, so the batch processor gets to flush what the loops produced.
        shutdown_tracing()
        logger.info("worker.shutdown", extra={"event": "worker.shutdown"})


if __name__ == "__main__":  # pragma: no cover - process entry point
    asyncio.run(main())
