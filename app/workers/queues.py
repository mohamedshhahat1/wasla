"""The operator's view of the queues, and the only way back out of one.

A command rather than an HTTP endpoint, and that is the decision rather than an
omission (ADR-071). Replay puts work back on a queue; on the agent queue that
work ends in a message to somebody's customer. An endpoint for it would need a
platform role, a rate limit, an audit trail and a story about what a tenant may
replay, and none of that is worth building before anybody has needed it once.
A command reachable only by whoever can already exec into the worker container
has exactly the audience this should have.

    docker compose exec worker python -m app.workers.queues status
    docker compose exec worker python -m app.workers.queues dead-letters agent
    docker compose exec worker python -m app.workers.queues replay ingestion

**Replay is never automatic, and never bulk by default.** A dead-lettered job
is one the system decided it could not finish; putting it back is a judgement
about why it failed, and a loop that made that judgement on its own would turn
a provider outage into the same jobs failing round and round for ever.

**Replay refuses the agent queue unless forced.** Ingestion and media are
idempotent - re-running replaces a document's chunks, and a file already read
is not read again - so replaying one costs a round trip. An agent turn is not:
it ends in a WhatsApp message that carries no idempotency key, so replaying one
whose failure came after the provider was engaged sends a second answer to a
question that already has one. `--force` exists because an operator who has
read the conversation may know better, and it says so out loud.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import RedisClient
from app.workers.queue import QUEUES, ReliableQueue

logger = get_logger(__name__)

# The queues whose jobs may be replayed without an argument about it, because
# re-running one changes nothing a customer can see. Mirrors the retry policy
# each worker carries: the queues that take `IDEMPOTENT_RETRY` are the queues
# that are safe to replay.
IDEMPOTENT_QUEUES = frozenset({"ingestion", "media"})


async def status(redis: RedisClient) -> int:
    """Every queue's depths, and how long its oldest job has waited."""
    now = datetime.now(UTC)
    header = f"{'queue':<12}{'pending':>9}{'inflight':>10}{'delayed':>9}{'dead':>7}{'oldest':>10}"
    print(header)  # noqa: T201 - an operator command's output is its purpose
    print("-" * len(header))  # noqa: T201
    for name, namespace in QUEUES.items():
        queue = ReliableQueue(redis.client, namespace=namespace)
        age = await queue.oldest_pending_age_seconds(now=now)
        age_text = "-" if age is None else f"{age:.0f}s"
        print(  # noqa: T201
            f"{name:<12}{await queue.depth():>9}{await queue.inflight_depth():>10}"
            f"{await queue.delayed_depth():>9}{await queue.failed_depth():>7}{age_text:>10}"
        )
    return 0


async def dead_letters(redis: RedisClient, *, queue_name: str, limit: int) -> int:
    """The most recent dead-letter records, newest first.

    Printed as the JSON they are stored as. They carry a failure *category*
    rather than an exception, and no message content - see `DeadLetterRecord`
    for what is deliberately absent and why.
    """
    namespace = QUEUES.get(queue_name)
    if namespace is None:
        print(f"unknown queue: {queue_name}", file=sys.stderr)  # noqa: T201
        return 2
    entries = await ReliableQueue(redis.client, namespace=namespace).dead_letters(limit=limit)
    if not entries:
        print(f"{queue_name}: no dead-lettered jobs")  # noqa: T201
        return 0
    for entry in entries:
        try:
            print(json.dumps(json.loads(entry), indent=2, sort_keys=True))  # noqa: T201
        except ValueError:
            print(entry)  # noqa: T201
    return 0


async def replay(redis: RedisClient, *, queue_name: str, limit: int, force: bool) -> int:
    """Put dead-lettered jobs back on the queue, as fresh first attempts.

    Fresh attempts, not continuations: the attempt count is what said the job
    had run out of budget, and an operator replaying it has decided the reason
    for that budget being spent is gone. Carrying the old count forward would
    dead-letter it again on the first failure without giving it the retry the
    operator was asking for.

    Records are taken from the *newest* end, matching what `dead-letters`
    prints, so an operator who has just read a record and decided to replay it
    gets that record rather than one from a fortnight ago.
    """
    namespace = QUEUES.get(queue_name)
    if namespace is None:
        print(f"unknown queue: {queue_name}", file=sys.stderr)  # noqa: T201
        return 2

    if queue_name not in IDEMPOTENT_QUEUES and not force:
        print(  # noqa: T201
            f"refusing to replay {queue_name}: an agent turn is not idempotent, so a "
            "replayed job can send a customer a second reply to a question that "
            "already has one.\nRead the conversation first, then pass --force if "
            "answering it again is genuinely what should happen.",
            file=sys.stderr,
        )
        return 3

    queue = ReliableQueue(redis.client, namespace=namespace)
    entries = await queue.dead_letters(limit=limit)
    if not entries:
        print(f"{queue_name}: nothing to replay")  # noqa: T201
        return 0

    replayed = 0
    for entry in entries:
        try:
            record = json.loads(entry)
            body = record["body"]
        except (ValueError, KeyError, TypeError):
            print(f"skipping an unreadable dead-letter record in {queue_name}")  # noqa: T201
            continue
        if not isinstance(body, str):
            print(f"skipping a dead-letter record with no payload in {queue_name}")  # noqa: T201
            continue
        await queue.enqueue_body(body)
        replayed += 1

    # The records are left where they are. A replayed job that fails again
    # writes a *new* record, and an operator comparing the two learns whether
    # the replay helped - which deleting the original would take away.
    logger.warning(
        "worker.dead_letters_replayed",
        extra={
            "event": "worker.dead_letters_replayed",
            "queue": queue_name,
            "replayed": replayed,
            "forced": force,
        },
    )
    print(  # noqa: T201
        f"{queue_name}: re-queued {replayed} job(s). The dead-letter records are kept; "
        "clear them once the replay is known to have worked."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.workers.queues",
        description="Inspect the job queues and replay dead-lettered work.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="queue depths and the age of the oldest job")

    listing = commands.add_parser("dead-letters", help="print recent dead-letter records")
    listing.add_argument("queue", choices=sorted(QUEUES))
    listing.add_argument("--limit", type=int, default=20)

    again = commands.add_parser("replay", help="re-queue dead-lettered jobs")
    again.add_argument("queue", choices=sorted(QUEUES))
    again.add_argument("--limit", type=int, default=20)
    again.add_argument(
        "--force",
        action="store_true",
        help="replay a queue whose jobs are not idempotent (agent)",
    )
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    redis = RedisClient(settings)
    try:
        if arguments.command == "status":
            return await status(redis)
        if arguments.command == "dead-letters":
            return await dead_letters(redis, queue_name=arguments.queue, limit=arguments.limit)
        return await replay(
            redis,
            queue_name=arguments.queue,
            limit=arguments.limit,
            force=arguments.force,
        )
    finally:
        await redis.close()


def main() -> int:  # pragma: no cover - process entry point
    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
