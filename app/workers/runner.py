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
from collections.abc import Iterable
from typing import Final, Protocol

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import RedisClient
from app.db.session import Database
from app.workers.ai_worker import AgentWorker
from app.workers.billing_worker import BillingWorker
from app.workers.campaign_worker import CampaignWorker
from app.workers.follow_up_worker import FollowUpWorker
from app.workers.ingestion_worker import IngestionWorker
from app.workers.media_worker import MediaWorker

logger = get_logger(__name__)

AGENT: Final = "agent"
INGESTION: Final = "ingestion"
FOLLOW_UP: Final = "follow_up"
MEDIA: Final = "media"
CAMPAIGN: Final = "campaign"
BILLING: Final = "billing"
# Media comes before agent deliberately. It is the order the work flows in -
# a file is read, then answered - and the order the log lines appear in at
# startup, which is worth having match. Billing is last: it is the only loop
# whose period is measured in days, and nothing else waits on it.
ALL_KINDS: Final = (MEDIA, AGENT, INGESTION, FOLLOW_UP, CAMPAIGN, BILLING)

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
    return workers


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


async def main() -> None:
    """Entry point for the worker container."""
    settings = get_settings()
    configure_logging(settings)

    database = Database(settings)
    redis = RedisClient(settings)
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

    try:
        await run(workers)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C in development, where the signal handler above is unavailable.
        for worker in workers:
            worker.stop()
    finally:
        await redis.close()
        await database.dispose()
        logger.info("worker.shutdown", extra={"event": "worker.shutdown"})


if __name__ == "__main__":  # pragma: no cover - process entry point
    asyncio.run(main())
