"""What a worker does with a job that did not succeed.

Extracted because the three queue workers made the same decision three times
and would now have to make a five-branch version of it three times. The
branches — is this failure worth another attempt, is there budget left, how
long to wait, what to write down when there is nothing left to try — are
identical whatever is in the payload. What differs is the policy, and that is
an argument.

The worker keeps its own `try`/`except`, deliberately. Handing the handler to
this module as a callback would put the one thing each worker does differently
behind a layer of indirection, and the `except` block is where a reader looks
to find out what happens when a customer's message goes unanswered.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.core.logging import get_logger
from app.core.telemetry import JobOutcome, record_job_outcome
from app.workers.queue import DeadLetterRecord, JobEnvelope, ReliableQueue
from app.workers.retry import FailureCategory, RetryPolicy, classify

logger = get_logger(__name__)

Action = Literal["retried", "dead_lettered", "lost"]


@dataclass(frozen=True, slots=True)
class JobIdentity:
    """Who the job was for, in terms safe to write into an operational record.

    Both optional, because a malformed payload has neither and that is exactly
    the case the dead-letter list most needs to record.
    """

    tenant_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class FailureOutcome:
    """What happened to the job, for the caller's log line and for tests."""

    action: Action
    category: FailureCategory
    attempt: int
    delay_seconds: float | None = None


async def handle_failure(
    queue: ReliableQueue,
    raw: str,
    envelope: JobEnvelope,
    *,
    job_type: str,
    identity: JobIdentity,
    error: BaseException | None = None,
    category: FailureCategory | None = None,
    policy: RetryPolicy,
    now: datetime | None = None,
    jitter: float | None = None,
) -> FailureOutcome:
    """Retry the job, or write down that it is finished.

    `category` overrides classification, which is what the malformed path
    uses: a payload that will not decode has no exception worth classifying
    and one obvious category.

    `jitter` is a fraction in [0, 1) and is drawn here when the caller does not
    supply one, so a test can pin the delay without patching `random` and
    production still spreads a burst of failures apart.

    Returns `lost` in the one case that is neither: the reservation was gone
    before this ran, so another worker — or a `dead_letter` that already
    succeeded — owns the job and this call must not write a second record.
    """
    moment = now or datetime.now(UTC)
    resolved = category if category is not None else classify(error or Exception())
    attempt = envelope.attempt

    if policy.should_retry(resolved, attempt=attempt):
        fraction = jitter if jitter is not None else random.random()  # noqa: S311 - not crypto
        delay = policy.delay_for(attempt, jitter=fraction)
        if await queue.schedule_retry(
            raw, envelope, category=resolved, delay_seconds=delay, now=moment
        ):
            await record_job_outcome(
                queue=job_type,
                outcome=JobOutcome.RETRIED,
                category=str(resolved),
            )
            logger.warning(
                "worker.job_retry_scheduled",
                extra={
                    "event": "worker.job_retry_scheduled",
                    "queue": job_type,
                    "attempt": attempt,
                    "max_attempts": policy.max_attempts,
                    "category": str(resolved),
                    "delay_seconds": round(delay, 1),
                    **_context(identity),
                },
            )
            return FailureOutcome(
                action="retried",
                category=resolved,
                attempt=attempt,
                delay_seconds=delay,
            )
        return FailureOutcome(action="lost", category=resolved, attempt=attempt)

    record = DeadLetterRecord(
        queue=queue.namespace,
        job_type=job_type,
        tenant_id=str(identity.tenant_id) if identity.tenant_id else None,
        job_id=str(identity.job_id) if identity.job_id else None,
        attempts=attempt,
        category=resolved,
        enqueued_at=envelope.enqueued_at,
        first_attempted_at=envelope.first_attempted_at,
        last_attempted_at=moment,
        dead_lettered_at=moment,
        body=envelope.body,
    )
    if not await queue.dead_letter(raw, record):
        return FailureOutcome(action="lost", category=resolved, attempt=attempt)

    await record_job_outcome(
        queue=job_type,
        outcome=JobOutcome.DEAD_LETTERED,
        category=str(resolved),
    )
    logger.error(
        "worker.job_dead_lettered",
        extra={
            "event": "worker.job_dead_lettered",
            "queue": job_type,
            "attempts": attempt,
            "category": str(resolved),
            **_context(identity),
        },
    )
    return FailureOutcome(action="dead_lettered", category=resolved, attempt=attempt)


async def record_success(*, job_type: str) -> None:
    await record_job_outcome(queue=job_type, outcome=JobOutcome.SUCCEEDED)


def _context(identity: JobIdentity) -> dict[str, str]:
    context: dict[str, str] = {}
    if identity.tenant_id is not None:
        context["tenant_id"] = str(identity.tenant_id)
    if identity.job_id is not None:
        context["job_id"] = str(identity.job_id)
    return context


__all__ = ["FailureOutcome", "JobIdentity", "handle_failure", "record_success"]
