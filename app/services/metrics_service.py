"""The exposition an operator's scraper reads.

Three sources, assembled here because a scraper wants one document:

1. **This process's own registry** — HTTP rate and latency, dependency
   readiness, unhandled errors. Ordinary in-process counters.
2. **Redis counters** — job outcomes and provider calls, written by whichever
   process did the work. The worker serves no HTTP by design, so this is how
   its numbers reach a scrape (ADR-069).
3. **Redis state, read live** — queue depth, in-flight, delayed, dead-letter
   depth, the age of the oldest waiting job, and whether each worker kind has
   beaten recently. These are *gauges of what is true now*, so reading them at
   scrape time is both simpler and more honest than having a worker publish
   them on a timer.

**Redis being down must not empty the page.** A scrape that answers 503 during
the exact outage an operator is investigating is worse than one that answers
with the half it can still see, so a failure collecting the Redis half is
logged and dropped, and the in-process half is served regardless. The absence
of the queue gauges is itself the signal - a scraper alerting on
`absent(wasla_queue_pending_jobs)` learns that Redis is unreachable, which is
the same thing it needed to know.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.pool import QueuePool

from app.core.logging import get_logger
from app.core.metrics import (
    REGISTRY,
    MetricsRegistry,
    render_gauge_lines,
    render_histogram_lines,
)
from app.core.telemetry import (
    REDIS_COUNTERS,
    REDIS_HISTOGRAMS,
    read_redis_counters,
    read_redis_histograms,
)
from app.db.session import Database
from app.repositories.conversation_repository import UnresolvedOutboundDirectory
from app.repositories.whatsapp_repository import InboundEventSweep
from app.services.backup_status import read_backup_status
from app.workers.heartbeat import heartbeat_key
from app.workers.inbound_recovery import unprocessed_since
from app.workers.queue import QUEUES, ReliableQueue
from app.workers.runner import ALL_KINDS

logger = get_logger(__name__)

# The four depth gauges, as (metric, help, the `QueueSnapshot` field it reads).
# A table rather than four near-identical blocks: the only thing that differs
# between them is which number is being published.
DEPTH_GAUGES: tuple[tuple[str, str, str], ...] = (
    ("wasla_queue_pending_jobs", "Jobs waiting to be reserved.", "pending"),
    (
        "wasla_queue_inflight_jobs",
        "Jobs a worker has claimed and not yet finished.",
        "inflight",
    ),
    ("wasla_queue_delayed_jobs", "Jobs waiting for a retry to come due.", "delayed"),
    (
        "wasla_queue_dead_letter_jobs",
        "Jobs that stopped being retried and are waiting for an operator.",
        "dead_lettered",
    ),
    (
        "wasla_queue_expired_reservations",
        "In-flight jobs whose worker has stopped renewing their lease.",
        "expired",
    ),
)


# Which process's pool a sample describes. Two values, ever.
#
# The label exists because this metric would otherwise lie by omission. `/metrics`
# is served by the API, and `AsyncEngine.pool` is a *process-local* object: the
# API can see its own pool and has no way at all to see the worker's. A series
# named `wasla_db_pool_checked_out` with no label reads as "the deployment's
# database pool", which is the one thing it is not. With the role on it, the
# absence of `process_role="worker"` is visible rather than implied, and the
# worker's samples can be added later without renaming anything (ADR-069 puts
# the worker's numbers in Redis, but a pool is a level rather than a total and
# a stale level is worse than a missing one). See ADR-085.
API_ROLE: Final = "api"

# `pool.overflow()` is deliberately not published. Its value is
# `open_connections - pool_size`, so it reads `-5` on a cold pool of five and
# an operator alerting on "overflow above zero" would be alerting on warmth.
# What saturation actually needs is `checked_out` against `size + max_overflow`,
# and all three of those are published below.
POOL_GAUGES: Final[tuple[tuple[str, str], ...]] = (
    (
        "wasla_db_pool_checked_out",
        "Pooled database connections currently held by application code.",
    ),
    (
        "wasla_db_pool_checked_in",
        "Pooled database connections open and idle.",
    ),
    (
        "wasla_db_pool_size",
        "Connections this process's pool keeps before it overflows.",
    ),
    (
        "wasla_db_pool_max_overflow",
        "Connections this process may open beyond the pool size.",
    ),
)


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """What one queue looks like at this instant."""

    name: str
    pending: int
    inflight: int
    delayed: int
    dead_lettered: int
    expired: int
    oldest_pending_age_seconds: float | None


class MetricsService:
    """Collects and renders the operational exposition."""

    def __init__(
        self,
        redis: Redis | None,
        *,
        registry: MetricsRegistry = REGISTRY,
        backup_status_path: str | None = None,
        database: Database | None = None,
    ) -> None:
        self._redis = redis
        self._registry = registry
        self._backup_status_path = backup_status_path
        self._database = database

    async def render(self, *, now: datetime | None = None) -> str:
        lines = await self._external(now=now)
        lines.extend(self._pool_lines())
        lines.extend(self._backup_lines(now=now))
        lines.extend(await self._consistency_lines())
        lines.extend(await self._messaging_lines(now=now))
        return self._registry.render(extra=lines)

    async def _messaging_lines(self, *, now: datetime | None) -> list[str]:
        """The two messaging backlogs nobody could previously see.

        **Inbound events still owing work.** A webhook that could not reach
        Redis stored the message, answered `200` and told nothing to answer it,
        and no number anywhere said how many of those there were (MSG-02). The
        cutoff is the sweeper's own, imported rather than restated, so an
        operator alerting on a backlog and a sweeper draining one are looking
        at the same set - two independently chosen thresholds would make the
        alert fire on work the sweeper considers in flight.

        **Outbound sends whose outcome is unknown.** `docs/WHATSAPP.md`
        promised these were "shown to a person"; nothing showed them to anybody
        (MSG-11). They are deliberately never resolved automatically, which is
        exactly why they have to be counted: a design that refuses to guess is
        only honest if somebody is told there is something to decide.

        No labels on any of the four. A tenant id here would be one time series
        per customer, and *which* workspace is a question for the operator
        command and the database - the alert fires on whether there is a
        backlog at all.

        Failures are swallowed and logged, like every other database-backed
        gauge here: losing the whole scrape because one count timed out is a
        worse outcome than losing this one, and during the outage that produced
        the backlog it is the most likely thing to time out.
        """
        database = self._database
        if database is None:
            return []
        moment = now or datetime.now(UTC)
        try:
            async with database.session() as session:
                inbound, inbound_age = await InboundEventSweep(session).backlog(
                    older_than=unprocessed_since(moment)
                )
                outbound, outbound_age = await UnresolvedOutboundDirectory(session).backlog()
        except Exception:
            logger.warning(
                "metrics.messaging_read_failed",
                extra={"event": "metrics.messaging_read_failed"},
            )
            return []

        lines: list[str] = []
        for name, help_text, value in (
            (
                "wasla_unprocessed_inbound_events",
                "Stored inbound events whose agent or media handoff never reached a queue.",
                float(inbound),
            ),
            (
                "wasla_unprocessed_inbound_oldest_age_seconds",
                "Age of the oldest inbound event still owing work.",
                inbound_age,
            ),
            (
                "wasla_unresolved_outbound_messages",
                "Sends Meta may have delivered, whose outcome is unknown.",
                float(outbound),
            ),
            (
                "wasla_oldest_unresolved_outbound_age_seconds",
                "Age of the oldest send whose outcome is unknown.",
                outbound_age,
            ),
        ):
            lines.extend(render_gauge_lines(name, help_text, [({}, value)]))
        return lines

    async def _consistency_lines(self) -> list[str]:
        """Invariants counted at scrape time, so a violation is alertable.

        One today: **active workspaces with no active owner**. Every code path
        that could produce one is supposed to prevent it - the tenant lock in
        `WorkspaceService`, the last-owner refusals in `MembershipService`, the
        automatic suspension in `AccountService._withdraw_memberships` - and
        this is what notices when one of them stops working, or when somebody
        produces the state with SQL.

        Counted rather than derived from an event, and that is the point: an
        event tells you a path you instrumented was taken, and a count tells you
        the state of the world however it got there. A defect nobody thought of
        does not emit an event.

        No labels at all. The number of offending workspaces is what an alert
        fires on; *which* ones is a question for the audit log and the database,
        where an identifier is appropriate. A tenant id here would be a time
        series per customer.

        Failures are swallowed and logged. A scrape that raises publishes
        nothing, and losing every other metric because one consistency query
        timed out is a worse outcome than losing this one.
        """
        database = self._database
        if database is None:
            return []
        try:
            async with database.session() as session:
                offenders = await session.scalar(text("""
                        SELECT count(*) FROM tenants t
                        WHERE t.status = 'active'
                          AND t.deleted_at IS NULL
                          AND NOT EXISTS (
                              SELECT 1 FROM memberships m
                              WHERE m.tenant_id = t.id
                                AND m.status = 'active'
                                AND m.role = 'tenant_owner'
                          )
                        """))
        except Exception:
            logger.warning(
                "metrics.consistency_read_failed",
                extra={"event": "metrics.consistency_read_failed"},
            )
            return []

        return render_gauge_lines(
            "wasla_orphaned_workspaces",
            "Active workspaces with no active owner. Should always be zero.",
            [({}, float(offenders or 0))],
        )

    def _pool_lines(self) -> list[str]:
        """This process's connection pool, read at the moment of the scrape.

        A level rather than a total, so it is read live like the queue depths
        above rather than published on a timer. The numbers come from
        `QueuePool`'s documented methods - `checkedout`, `checkedin`, `size` -
        and nothing here reaches into the pool's internals; a pool class that
        does not offer them (a `NullPool` under a test, a `StaticPool`) simply
        publishes nothing, which is the honest answer to "how full is a pool
        that does not queue".

        `max_overflow` comes from the application's own settings rather than
        from the pool, because `QueuePool` keeps it private and the deployment
        is where the number was decided anyway.
        """
        database = self._database
        if database is None:
            return []
        try:
            pool = database.engine.pool
            if not isinstance(pool, QueuePool):
                return []
            values = {
                "wasla_db_pool_checked_out": float(pool.checkedout()),
                "wasla_db_pool_checked_in": float(pool.checkedin()),
                "wasla_db_pool_size": float(pool.size()),
                "wasla_db_pool_max_overflow": float(database.max_overflow),
            }
        except Exception:
            logger.warning(
                "metrics.pool_read_failed",
                extra={"event": "metrics.pool_read_failed"},
            )
            return []

        lines: list[str] = []
        for name, help_text in POOL_GAUGES:
            lines.extend(
                render_gauge_lines(name, help_text, [({"process_role": API_ROLE}, values[name])])
            )
        return lines

    def _backup_lines(self, *, now: datetime | None) -> list[str]:
        """What the backup process last wrote down, if this deployment mounts it.

        Synchronous and file-backed rather than a counter, because the process
        that knows the answer has already exited by the time anybody scrapes -
        see `app/services/backup_status.py`. Absent when no path is configured
        or no status has been written: a missing series says "this deployment
        cannot tell you", which is a truer alert than a zero.
        """
        if not self._backup_status_path:
            return []
        status = read_backup_status(self._backup_status_path)
        if status is None:
            return []

        lines: list[str] = []
        age = status.age_seconds(now=now)
        if status.last_success_at is not None:
            lines.extend(
                render_gauge_lines(
                    "wasla_backup_last_success_timestamp_seconds",
                    "When a backup last reached its off-host destination.",
                    [({}, status.last_success_at.timestamp())],
                )
            )
        if age is not None:
            lines.extend(
                render_gauge_lines(
                    "wasla_backup_age_seconds",
                    "How long since a backup last reached its off-host destination.",
                    [({}, age)],
                )
            )
        lines.extend(
            _counter(
                "wasla_backup_failures_total",
                "Backup runs that did not produce a durable off-host copy.",
                # `stage` is a bounded set the script chooses: dump, validate,
                # upload, retention. Never a message, never a bucket name.
                [({"stage": status.failed_stage or "none"}, float(status.failures_total))],
            )
        )
        return lines

    async def snapshot(self, *, now: datetime | None = None) -> list[QueueSnapshot]:
        """Every queue's depths, for the exposition and for an operator command."""
        redis = self._redis
        if redis is None:
            return []
        moment = now or datetime.now(UTC)
        snapshots: list[QueueSnapshot] = []
        for name, namespace in QUEUES.items():
            queue = ReliableQueue(redis, namespace=namespace)
            snapshots.append(
                QueueSnapshot(
                    name=name,
                    pending=await queue.depth(),
                    inflight=await queue.inflight_depth(),
                    delayed=await queue.delayed_depth(),
                    dead_lettered=await queue.failed_depth(),
                    expired=await queue.expired_depth(now=moment),
                    oldest_pending_age_seconds=await queue.oldest_pending_age_seconds(now=moment),
                )
            )
        return snapshots

    async def _external(self, *, now: datetime | None) -> list[str]:
        redis = self._redis
        if redis is None:
            return []
        lines: list[str] = []
        try:
            lines.extend(await self._counter_lines(redis))
            lines.extend(await self._histogram_lines(redis))
            lines.extend(await self._queue_lines(now=now))
            lines.extend(await self._heartbeat_lines(redis))
        except Exception:
            # Deliberately broad, and deliberately not re-raised. See the
            # module docstring: half an exposition beats a 503 during an
            # incident, and an absent series is itself alertable.
            logger.warning(
                "metrics.collection_failed",
                extra={"event": "metrics.collection_failed"},
            )
        return lines

    async def _counter_lines(self, redis: Redis) -> list[str]:
        collected = await read_redis_counters(redis)
        lines: list[str] = []
        for metric, (help_text, _) in REDIS_COUNTERS.items():
            lines.extend(_counter(metric, help_text, collected.get(metric, [])))
        return lines

    async def _histogram_lines(self, redis: Redis) -> list[str]:
        """Provider latency, counted by whichever process made the call.

        Rendered even when empty, so the exposition carries the `# HELP` and
        `# TYPE` for a distribution this process may never have observed - a
        deployment whose API has taken no payment still publishes the metric
        the worker fills in.
        """
        collected = await read_redis_histograms(redis)
        lines: list[str] = []
        for metric, (help_text, _, bounds) in REDIS_HISTOGRAMS.items():
            lines.extend(
                render_histogram_lines(metric, help_text, bounds, collected.get(metric, []))
            )
        return lines

    async def _queue_lines(self, *, now: datetime | None) -> list[str]:
        snapshots = await self.snapshot(now=now)
        lines: list[str] = []
        for name, help_text, field in DEPTH_GAUGES:
            lines.extend(
                render_gauge_lines(
                    name,
                    help_text,
                    [({"queue": item.name}, float(getattr(item, field))) for item in snapshots],
                )
            )
        # Only the queues that have something waiting. An empty queue has no
        # oldest job, and publishing zero for one would read as "nothing has
        # been waiting long" - a claim about latency rather than about
        # emptiness, and the one an alert would act on.
        lines.extend(
            render_gauge_lines(
                "wasla_queue_oldest_pending_age_seconds",
                "How long the job at the head of the queue has been waiting.",
                [
                    ({"queue": item.name}, item.oldest_pending_age_seconds)
                    for item in snapshots
                    if item.oldest_pending_age_seconds is not None
                ],
            )
        )
        return lines

    async def _heartbeat_lines(self, redis: Redis) -> list[str]:
        """Whether each worker kind has beaten inside its expiry.

        Read from the same keys `worker-health` reads, deliberately: a second
        heartbeat system would be a second thing to keep true, and the point of
        publishing this is that the container probe and the alert agree.
        """
        samples: list[tuple[dict[str, str], float]] = []
        for kind in ALL_KINDS:
            alive = await redis.exists(heartbeat_key(kind))
            samples.append(({"kind": kind}, 1.0 if alive else 0.0))
        return render_gauge_lines(
            "wasla_worker_heartbeat_alive",
            "Whether a worker kind has refreshed its liveness key inside its expiry.",
            samples,
        )


def _counter(
    name: str,
    help_text: str,
    samples: list[tuple[dict[str, str], float]],
) -> list[str]:
    """Render Redis-held totals as a counter rather than a gauge.

    Separate from `render_gauge_lines` only in the `# TYPE` line, which is what
    tells a scraper it may compute a rate and expect a reset when Redis is
    flushed or restored.
    """
    lines = render_gauge_lines(name, help_text, samples)
    lines[1] = f"# TYPE {name} counter"
    return lines


__all__ = ["API_ROLE", "DEPTH_GAUGES", "POOL_GAUGES", "MetricsService", "QueueSnapshot"]
