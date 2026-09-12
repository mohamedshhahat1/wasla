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
    docker compose exec worker python -m app.workers.queues replay agent --force --dry-run
    docker compose exec worker python -m app.workers.queues unprocessed-inbound
    docker compose exec worker python -m app.workers.queues unindexed-documents
    docker compose exec worker python -m app.workers.queues unresolved-sends

The last three read the database rather than Redis, and they are here because
this is where an operator already looks when work is not moving. Each answers
a question the deployment previously had no way to ask: what inbound did we
store and never process, what did somebody upload that is still not
searchable, and what did we send that we cannot account for.

**Replay is never automatic, and never bulk by default.** A dead-lettered job
is one the system decided it could not finish; putting it back is a judgement
about why it failed, and a loop that made that judgement on its own would turn
a provider outage into the same jobs failing round and round for ever.

**Replay refuses the agent queue unless forced.** Ingestion and media are
idempotent - re-running replaces a document's chunks, and a file already read
is not read again - so replaying one costs a round trip. An agent turn is not,
so `--force` exists because an operator who has read the conversation may know
better, and it says so out loud.

**And an uncertain record is never replayed by default, on any queue.** The
queue gate above asks whether this *queue* is safe; it cannot ask whether a
particular *record* is, and a dead-letter list mixing ordinary `provider_error`
failures with `uncertain_delivery` ones used to be replayable only as a whole.
That is the worst possible moment for it - a long mixed list is what a provider
outage produces - and `uncertain_delivery` means precisely "a customer may
already have this reply" (WQ-06). Recovering the ordinary failures no longer
takes the uncertain ones with them:

    replay agent --force --dry-run          # look first
    replay agent --force                    # the ordinary ones
    replay agent --force --include-uncertain --job-id <id>   # one, deliberately
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import RedisClient
from app.db.session import Database
from app.repositories.conversation_repository import UnresolvedOutboundDirectory
from app.repositories.knowledge_repository import PendingDocumentSweep
from app.repositories.whatsapp_repository import InboundEventSweep
from app.workers.inbound_recovery import unprocessed_since
from app.workers.ingestion_recovery import unindexed_since
from app.workers.queue import QUEUES, ReliableQueue
from app.workers.retry import FailureCategory

logger = get_logger(__name__)

# The queues whose jobs may be replayed without an argument about it, because
# re-running one changes nothing a customer can see. Mirrors the retry policy
# each worker carries: the queues that take `IDEMPOTENT_RETRY` are the queues
# that are safe to replay.
IDEMPOTENT_QUEUES = frozenset({"ingestion", "media"})

# The one category that is never replayed by default, on any queue. A job
# dead-lettered as `uncertain_delivery` engaged a provider and then stopped,
# so the customer may already have the reply it was about to send - which is
# the exact harm the engagement barrier exists to prevent (ADR-074). Read
# from the taxonomy rather than written as a literal, so the two cannot
# drift apart.
UNCERTAIN_DELIVERY = str(FailureCategory.UNCERTAIN_DELIVERY)


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


async def replay(
    redis: RedisClient,
    *,
    queue_name: str,
    limit: int,
    force: bool,
    include_uncertain: bool = False,
    job_id: str | None = None,
    dry_run: bool = False,
) -> int:
    """Put dead-lettered jobs back on the queue, as fresh first attempts.

    Fresh attempts, not continuations: the attempt count is what said the job
    had run out of budget, and an operator replaying it has decided the reason
    for that budget being spent is gone. Carrying the old count forward would
    dead-letter it again on the first failure without giving it the retry the
    operator was asking for.

    Records are taken from the *newest* end, matching what `dead-letters`
    prints, so an operator who has just read a record and decided to replay it
    gets that record rather than one from a fortnight ago.

    **Two gates, not one, and the second is the one that was missing.** The
    queue gate (`--force`) asks whether this *queue* is safe to replay at all.
    It is correct and it stays. What it could not ask is whether a particular
    *record* is safe, and `--force` took the whole batch - so an operator
    recovering from a provider outage, which is exactly when the list is long
    and mixed, replayed the `uncertain_delivery` records sitting alongside the
    ordinary ones. `uncertain_delivery` means precisely "a customer may already
    have this reply": it is the harm the engagement barrier exists to prevent,
    reintroduced at the operator's own hand (WQ-06).

    So an uncertain record is skipped by default, on every queue, whatever
    `--force` says. `--include-uncertain` exists because an operator who has
    read the conversation may know the reply never arrived - but it has to be
    asked for separately from "replay this queue", because those are different
    decisions about different risks.

    `--job-id` narrows to one record, and `--dry-run` prints what would happen
    without doing it. Both exist for the same reason: the safe way to recover a
    mixed list is to look first and act narrowly, and until now the only
    available action was the widest one.
    """
    namespace = QUEUES.get(queue_name)
    if namespace is None:
        print(f"unknown queue: {queue_name}", file=sys.stderr)  # noqa: T201
        return 2

    if queue_name not in IDEMPOTENT_QUEUES and not force and not dry_run:
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
    protected = 0
    skipped = 0
    header = f"{'age':>10}  {'category':<22}  {'attempts':>8}  {'workspace':<36}  job"
    print(header)  # noqa: T201
    print("-" * len(header))  # noqa: T201
    now = datetime.now(UTC)

    for entry in entries:
        try:
            record = json.loads(entry)
            body = record["body"]
        except (ValueError, KeyError, TypeError):
            print(f"skipping an unreadable dead-letter record in {queue_name}")  # noqa: T201
            skipped += 1
            continue
        if not isinstance(body, str) or not isinstance(record, dict):
            print(f"skipping a dead-letter record with no payload in {queue_name}")  # noqa: T201
            skipped += 1
            continue

        identifier = record.get("job_id")
        if job_id is not None and identifier != job_id:
            continue

        category = record.get("category")
        age = _record_age(record, now=now)
        # Identifiers, a category, a count and an age. No message content and no
        # credentials, for the same reason the record itself carries none: this
        # is printed into a terminal scrollback and a shell history.
        line = (
            f"{age:>10}  {category!s:<22}  {record.get('attempts', '?')!s:>8}  "
            f"{record.get('tenant_id') or '-'!s:<36}  {identifier or '-'}"
        )

        if category == UNCERTAIN_DELIVERY and not include_uncertain:
            print(f"{line}   PROTECTED - not replayed")  # noqa: T201
            protected += 1
            continue

        if dry_run:
            print(f"{line}   would replay")  # noqa: T201
            replayed += 1
            continue

        await queue.enqueue_body(body)
        print(f"{line}   replayed")  # noqa: T201
        replayed += 1

    # The records are left where they are. A replayed job that fails again
    # writes a *new* record, and an operator comparing the two learns whether
    # the replay helped - which deleting the original would take away.
    if not dry_run:
        logger.warning(
            "worker.dead_letters_replayed",
            extra={
                "event": "worker.dead_letters_replayed",
                "queue": queue_name,
                "replayed": replayed,
                "protected": protected,
                "forced": force,
                "included_uncertain": include_uncertain,
            },
        )

    verb = "would re-queue" if dry_run else "re-queued"
    print()  # noqa: T201
    print(  # noqa: T201
        f"{queue_name}: {verb} {replayed} job(s); {protected} left alone as "
        f"{UNCERTAIN_DELIVERY}; {skipped} unreadable."
    )
    if protected:
        print(  # noqa: T201
            f"An {UNCERTAIN_DELIVERY} record means the customer may already have that "
            "reply. Read the conversation; replay one only with --include-uncertain, "
            "and preferably with --job-id so it is the only one."
        )
    if not dry_run:
        print(  # noqa: T201
            "The dead-letter records are kept; clear them once the replay has worked."
        )
    return 0


def _record_age(record: dict[str, Any], *, now: datetime) -> str:
    """How long ago this job was given up on, or `-` if the record cannot say."""
    stamp = record.get("dead_lettered_at")
    if not isinstance(stamp, str):
        return "-"
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return "-"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"{(now - moment).total_seconds():.0f}s"


async def unprocessed_inbound(database: Database, *, limit: int) -> int:
    """Inbound events that were stored and never finished.

    A row here is a customer message sitting in somebody's inbox that no worker
    was ever told to answer - almost always because Redis was unavailable when
    the webhook arrived (MSG-02). `InboundRecoveryWorker` drains these on its
    own; this command exists so a person can see the backlog, see whether it is
    shrinking, and see the reason each event is stuck on.

    No message bodies. The workspace and conversation are enough to find the
    conversation in the product, and printing customer text into a terminal
    scrollback and a shell history is not something an operator asked for.
    """
    async with database.session() as session:
        events = await InboundEventSweep(session).claim_unprocessed(
            older_than=unprocessed_since(datetime.now(UTC)),
            limit=limit,
        )
        if not events:
            print("no unprocessed inbound events")  # noqa: T201
            return 0
        header = f"{'age':>10}  {'kind':<8}  {'workspace':<36}  {'reason':<24}  event"
        print(header)  # noqa: T201
        print("-" * len(header))  # noqa: T201
        now = datetime.now(UTC)
        for event in events:
            age = f"{(now - event.created_at).total_seconds():.0f}s"
            print(  # noqa: T201
                f"{age:>10}  {event.kind.value:<8}  {event.tenant_id!s:<36}  "
                f"{(event.error or '-'):<24}  {event.event_id}"
            )
    # The claim's transaction ends here without marking anything, so the rows
    # are released exactly as they were found. Reading the backlog must not
    # change it.
    return 0


async def unindexed_documents(database: Database, *, limit: int) -> int:
    """Documents that were uploaded and never indexed.

    A row here is a file somebody in a workspace successfully uploaded that no
    agent can search - almost always because Redis was unavailable when the
    upload committed (WQ-03). `IngestionRecoveryWorker` drains these on its own;
    this command exists so a person can see the backlog, see whether it is
    shrinking, and see how old the oldest one is.

    No document contents and no filenames-as-payload: the workspace, the
    knowledge base and the document id are enough to find it in the product, and
    printing a customer's uploaded text into a terminal scrollback and a shell
    history is not something an operator asked for.
    """
    async with database.session() as session:
        documents = await PendingDocumentSweep(session).claim_pending(
            older_than=unindexed_since(datetime.now(UTC)),
            limit=limit,
        )
        if not documents:
            print("no unindexed documents")  # noqa: T201
            return 0
        header = f"{'age':>10}  {'workspace':<36}  {'knowledge base':<36}  document"
        print(header)  # noqa: T201
        print("-" * len(header))  # noqa: T201
        now = datetime.now(UTC)
        for document in documents:
            age = f"{(now - document.created_at).total_seconds():.0f}s"
            print(  # noqa: T201
                f"{age:>10}  {document.tenant_id!s:<36}  "
                f"{document.knowledge_base_id!s:<36}  {document.id}"
            )
    # The claim's transaction ends here without marking anything, so the rows
    # are released exactly as they were found. Reading the backlog must not
    # change it.
    return 0


async def unresolved_sends(database: Database, *, limit: int) -> int:
    """Sends Meta may already have delivered, whose outcome is unknown.

    **Nothing here may be sent again automatically, and this command will not
    do it.** A `requested` row means the send intent was committed, Meta was
    asked, and no usable answer came back - so the message may be on the
    customer's phone. There is no idempotency key on Meta's send endpoint and
    no lookup keyed on anything this system generated, which is why the state
    is terminal by construction (ADR-093). The only thing that settles one of
    these is a person reading the conversation.

    See `docs/RUNBOOK.md` for what to do with what this prints.
    """
    async with database.session() as session:
        rows = await UnresolvedOutboundDirectory(session).list_unresolved(limit=limit)
        if not rows:
            print("no unresolved outbound sends")  # noqa: T201
            return 0
        header = (
            f"{'age':>10}  {'workspace':<36}  {'conversation':<36}  {'provider id':<24}  message"
        )
        print(header)  # noqa: T201
        print("-" * len(header))  # noqa: T201
        now = datetime.now(UTC)
        for row in rows:
            age = f"{(now - row.created_at).total_seconds():.0f}s"
            print(  # noqa: T201
                f"{age:>10}  {row.tenant_id!s:<36}  {row.conversation_id!s:<36}  "
                f"{(row.wa_message_id or '-'):<24}  {row.id}"
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
    again.add_argument(
        "--include-uncertain",
        action="store_true",
        help=(
            "also replay records whose failure was uncertain_delivery - the "
            "customer may already have that reply"
        ),
    )
    again.add_argument(
        "--job-id",
        help="replay only the record for this job id",
    )
    again.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be replayed without replaying anything",
    )

    stuck = commands.add_parser(
        "unprocessed-inbound",
        help="inbound events that were stored and never handed to a worker",
    )
    stuck.add_argument("--limit", type=int, default=50)

    unindexed = commands.add_parser(
        "unindexed-documents",
        help="documents that were uploaded and never handed to a worker",
    )
    unindexed.add_argument("--limit", type=int, default=50)

    open_sends = commands.add_parser(
        "unresolved-sends",
        help="sends whose outcome WhatsApp never confirmed (never resent automatically)",
    )
    open_sends.add_argument("--limit", type=int, default=50)
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)

    # The two database-backed commands build no Redis client and the three
    # queue commands build no database pool, so neither half of the deployment
    # has to be reachable to inspect the other. During the outage that produced
    # a backlog, that is not a detail.
    if arguments.command in {"unprocessed-inbound", "unindexed-documents", "unresolved-sends"}:
        database = Database(settings)
        try:
            if arguments.command == "unprocessed-inbound":
                return await unprocessed_inbound(database, limit=arguments.limit)
            if arguments.command == "unindexed-documents":
                return await unindexed_documents(database, limit=arguments.limit)
            return await unresolved_sends(database, limit=arguments.limit)
        finally:
            await database.dispose()

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
            include_uncertain=arguments.include_uncertain,
            job_id=arguments.job_id,
            dry_run=arguments.dry_run,
        )
    finally:
        await redis.close()


def main() -> int:  # pragma: no cover - process entry point
    return asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
