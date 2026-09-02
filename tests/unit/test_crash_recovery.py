"""What happens to a job whose worker stopped answering for it.

Before this existed the answer was "nothing, for ever". A job moved to the
in-flight list when it was reserved and left it only when the worker that
reserved it said so; a worker that died said nothing, and the job stayed there
until somebody noticed and moved it by hand. The queue looked healthy the whole
time, because a stranded job is not a failure — it is an absence.

Two properties are being proved here and they pull in opposite directions:

    a safe job must never be stranded
    an unsafe job must never be repeated

The first wants recovery to be eager. The second wants it to refuse. What
reconciles them is the reservation *stage*, written to Redis at the moment a
turn engages the provider, so a crash cannot take that knowledge with it.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.workers.ingestion_queue import IngestionJob, IngestionQueue
from app.workers.media_queue import MediaJob, MediaQueue
from app.workers.queue import (
    DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    AgentJob,
    AgentQueue,
    JobEnvelope,
    Reservation,
    ReservationStage,
)
from app.workers.retry import (
    IDEMPOTENT_RETRY,
    NEEDS_OPERATOR,
    NO_RETRY,
    RETRYABLE,
    FailureCategory,
    RetryPolicy,
    classify,
)
from tests.fake_queue_redis import FakeQueueRedis

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
DOCUMENT = uuid.UUID("33333333-3333-3333-3333-333333333333")
MEDIA = uuid.UUID("44444444-4444-4444-4444-444444444444")

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
TIMEOUT = 120.0
EXPIRED = NOW + timedelta(seconds=TIMEOUT + 1)

POLICY = RetryPolicy(max_attempts=3, base_seconds=10.0, max_seconds=100.0, jitter_ratio=0.0)


@pytest.fixture
def redis():
    return FakeQueueRedis()


def agent_queue(redis, *, worker_id="worker-a"):
    return AgentQueue(redis, worker_id=worker_id, visibility_timeout_seconds=TIMEOUT)


async def reserved_agent_job(redis, *, worker_id="worker-a", now=NOW):
    queue = agent_queue(redis, worker_id=worker_id)
    await queue.enqueue(AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION), now=now)
    raw = await queue.reserve(wait_seconds=1, now=now)
    return queue, raw


# ------------------------------------------------------- the reservation itself


async def test_reserving_records_who_holds_the_job_and_until_when(redis):
    queue, raw = await reserved_agent_job(redis)

    reservation = await queue.reservation(raw, now=NOW)

    assert reservation is not None
    assert reservation.worker == "worker-a"
    assert reservation.reserved_at == NOW
    assert reservation.lease_until == NOW + timedelta(seconds=TIMEOUT)
    assert reservation.stage is ReservationStage.RESERVED


async def test_acknowledging_a_job_forgets_its_reservation(redis):
    """A reservation that outlived its job would be recovered for ever."""
    queue, raw = await reserved_agent_job(redis)

    await queue.release(raw)

    assert await queue.reservation(raw, now=NOW) is None
    assert redis.strings.get("agent:jobs:reservations", {}) == {}


async def test_a_retry_forgets_its_reservation_too(redis):
    queue, raw = await reserved_agent_job(redis)

    await queue.schedule_retry(
        raw,
        JobEnvelope.decode(raw),
        category=classify(TimeoutError()),
        delay_seconds=1.0,
        now=NOW,
    )

    assert await queue.reservation(raw, now=NOW) is None


# --------------------------------------------------------------- the lease


async def test_a_live_lease_is_not_reclaimed(redis):
    """The property that stops recovery stealing work from a living worker."""
    queue, _ = await reserved_agent_job(redis)

    just_before = NOW + timedelta(seconds=TIMEOUT - 1)
    assert await queue.recover_expired(policy=POLICY, now=just_before) == []
    assert await queue.inflight_depth() == 1


async def test_renewal_pushes_the_lease_out(redis):
    """A long job is safe because the process holding it keeps saying so."""
    queue, raw = await reserved_agent_job(redis)

    midway = NOW + timedelta(seconds=TIMEOUT / 2)
    assert await queue.renew_leases(now=midway) == 1

    # The moment that would have expired the original lease no longer does.
    assert await queue.recover_expired(policy=POLICY, now=EXPIRED) == []
    reservation = await queue.reservation(raw, now=midway)
    assert reservation is not None
    assert reservation.lease_until == midway + timedelta(seconds=TIMEOUT)


async def test_renewal_does_not_resurrect_a_reclaimed_job(redis):
    """The race the renewal loop must not lose.

    A reaper reclaims the job; the worker that lost it renews a moment later.
    If renewal simply wrote the reservation back, the job would be both
    requeued *and* in-flight, and the next pass would recover it a second time.

    Two queue objects, deliberately: the worker still believes it holds the
    job, which is the whole point. Using one object for both roles emptied the
    worker's own set of held reservations as a side effect of the reaper's
    claim, so the renewal loop iterated over nothing and the test passed
    whatever the code did.
    """
    worker, raw = await reserved_agent_job(redis, worker_id="doomed-worker")
    reaper = agent_queue(redis, worker_id="reaper")

    assert await reaper.recover_expired(policy=POLICY, now=EXPIRED) != []
    assert raw in worker.held, "the worker has not noticed it lost the job"

    assert await worker.renew_leases(now=EXPIRED) == 0
    assert await worker.reservation(raw, now=EXPIRED) is None
    assert raw not in worker.held
    assert await worker.delayed_depth() == 1
    assert await worker.inflight_depth() == 0


async def test_renewal_only_covers_what_this_process_holds(redis):
    """A second queue object over the same Redis renews nothing."""
    await reserved_agent_job(redis, worker_id="worker-a")
    other = agent_queue(redis, worker_id="worker-b")

    assert await other.renew_leases(now=NOW) == 0


# ------------------------------------------------------- safe job recovery


async def test_a_crashed_safe_job_comes_back(redis):
    """The gap this whole module closes."""
    queue, _ = await reserved_agent_job(redis)

    outcomes = await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)

    assert [outcome.action for outcome in outcomes] == ["requeued"]
    assert await queue.inflight_depth() == 0
    assert await queue.delayed_depth() == 1
    assert await queue.failed_depth() == 0


async def test_recovery_preserves_the_attempt_history(redis):
    """A crash is an execution attempt, not a fresh start."""
    queue, _ = await reserved_agent_job(redis)

    await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)

    (scheduled,) = redis.zsets["agent:jobs:delayed"]
    envelope = JobEnvelope.decode(scheduled)
    assert envelope.attempt == 2
    assert envelope.last_failure is not None
    assert str(envelope.last_failure) == "worker_crashed"
    assert envelope.enqueued_at == NOW


async def test_recovery_keeps_the_original_enqueue_time(redis):
    """Queue age must still measure how long the customer has waited."""
    queue, _ = await reserved_agent_job(redis)

    await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)
    await queue.promote_due(now=EXPIRED + timedelta(hours=1))

    age = await queue.oldest_pending_age_seconds(now=EXPIRED + timedelta(hours=1))
    assert age is not None
    assert age > 3600


# ------------------------------------------------------------ no duplication


async def test_recovering_twice_recovers_once(redis):
    queue, _ = await reserved_agent_job(redis)

    first = await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)
    second = await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)

    assert len(first) == 1
    assert second == []
    assert await queue.delayed_depth() == 1


async def test_two_reapers_that_both_saw_the_job_recover_it_once(redis):
    """Two processes, one Redis, the same expired reservation.

    **Both must have enumerated before either claims**, or the test proves
    nothing: run one after the other and the second finds an empty in-flight
    list and never reaches the claim at all. The barrier below holds both
    inside `expired()` until each has seen the entry, which is the interleaving
    a real pair of reapers hits and the only one where `_claim_inflight`
    matters.

    `LREM` returns 1 for exactly one caller, so the loser does nothing rather
    than requeueing a job somebody else has already requeued.
    """
    # The barrier sits on the *claim*, not on the enumeration. Holding both
    # inside `expired()` looked right and proved nothing: the first reaper to
    # get through deletes the reservation record, so the second finds no
    # reservation, adopts the entry and never reaches the claim at all. Pausing
    # at `lrem` instead lets both complete their enumeration - both genuinely
    # see the job - and then makes them race for it.
    both_claiming = asyncio.Event()
    claimants = 0

    class Interleaving(FakeQueueRedis):
        async def lrem(self, key, count, value):
            nonlocal claimants
            if key.endswith(":inflight"):
                claimants += 1
                if claimants >= 2:
                    both_claiming.set()
                await both_claiming.wait()
            return await super().lrem(key, count, value)

    redis = Interleaving()
    await reserved_agent_job(redis, worker_id="dead-worker")
    reaper_a = agent_queue(redis, worker_id="reaper-a")
    reaper_b = agent_queue(redis, worker_id="reaper-b")

    outcomes_a, outcomes_b = await asyncio.gather(
        reaper_a.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0),
        reaper_b.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0),
    )

    assert len(outcomes_a) + len(outcomes_b) == 1, "both reapers acted on one job"
    assert await reaper_a.delayed_depth() == 1
    assert await reaper_a.failed_depth() == 0
    assert await reaper_a.inflight_depth() == 0


# --------------------------------------------------------- attempt ceiling


async def test_a_crash_on_the_last_attempt_does_not_buy_another(redis):
    """The hidden-extra-attempt trap, closed.

    A job that crashes a worker every time would otherwise loop for ever:
    each crash would look like a fresh reason to try again and the budget
    would never run out.
    """
    queue = agent_queue(redis)
    final = JobEnvelope(
        body=AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION).encode(),
        attempt=POLICY.max_attempts,
        enqueued_at=NOW,
    )
    redis.lists["agent:jobs:pending"] = [final.encode()]
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    assert raw is not None

    outcomes = await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)

    assert [outcome.action for outcome in outcomes] == ["quarantined"]
    assert await queue.delayed_depth() == 0
    assert await queue.failed_depth() == 1


async def test_a_job_can_never_crash_its_way_around_the_budget(redis):
    """The property, over a loop of crashes rather than one example."""
    queue, _ = await reserved_agent_job(redis)

    # The clock only ever moves forward: each pass waits out the lease, then
    # jumps past the backoff so the retry is due, then reserves again - which
    # is a worker picking the job up and dying on it once more.
    now = NOW
    for _ in range(20):
        now += timedelta(seconds=TIMEOUT + 1)
        await queue.recover_expired(policy=POLICY, now=now, jitter=0.0)
        now += timedelta(days=1)
        await queue.promote_due(now=now)
        if await queue.reserve(wait_seconds=1, now=now) is None:
            break
    else:  # pragma: no cover - only reached if the budget never runs out
        pytest.fail("the job kept being recovered after twenty crashes")

    assert await queue.failed_depth() == 1


async def test_a_job_whose_last_failure_was_terminal_is_not_resurrected(redis):
    """No amount of crashing turns a permanent failure into a retryable one."""
    queue = agent_queue(redis)
    doomed = JobEnvelope(
        body=AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION).encode(),
        attempt=1,
        enqueued_at=NOW,
        last_failure=FailureCategory.INVALID_REQUEST,
    )
    redis.lists["agent:jobs:pending"] = [doomed.encode()]
    await queue.reserve(wait_seconds=1, now=NOW)

    outcomes = await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)

    assert [outcome.action for outcome in outcomes] == ["quarantined"]
    assert await queue.delayed_depth() == 0


# ------------------------------------------------------- agent crash safety


async def test_an_agent_crash_before_engagement_is_recovered(redis):
    """Nothing had left the process, so nobody outside saw anything."""
    queue, raw = await reserved_agent_job(redis)
    reservation = await queue.reservation(raw, now=NOW)
    assert reservation is not None and reservation.stage is ReservationStage.RESERVED

    outcomes = await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)

    assert [outcome.action for outcome in outcomes] == ["requeued"]


async def test_an_agent_crash_after_engagement_is_never_resent(redis):
    """The invariant this whole design exists to protect.

    The turn had begun talking to Meta. Whether the message landed is
    unknowable from here, and the one action that must not follow is another
    send.
    """
    queue, raw = await reserved_agent_job(redis)
    assert await queue.mark_engaged(raw, now=NOW) is True

    outcomes = await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)

    assert [outcome.action for outcome in outcomes] == ["quarantined"]
    assert [str(outcome.category) for outcome in outcomes] == ["uncertain_delivery"]
    assert await queue.delayed_depth() == 0, "an engaged agent turn must never be requeued"
    assert await queue.depth() == 0
    assert await queue.failed_depth() == 1


async def test_the_engaged_stage_survives_the_process_that_set_it(redis):
    """Which is the entire reason it is written to Redis rather than memory."""
    queue, raw = await reserved_agent_job(redis, worker_id="dying-worker")
    await queue.mark_engaged(raw, now=NOW)

    # A different process entirely, with no memory of the first.
    fresh = agent_queue(redis, worker_id="replacement-worker")
    reservation = await fresh.reservation(raw, now=NOW)

    assert reservation is not None
    assert reservation.stage is ReservationStage.ENGAGED


async def test_marking_a_reclaimed_job_engaged_reports_failure(redis):
    """The worker cannot fix it, but it belongs in a log line."""
    queue, raw = await reserved_agent_job(redis)
    await queue.recover_expired(policy=POLICY, now=EXPIRED)

    assert await queue.mark_engaged(raw, now=EXPIRED) is False


# ----------------------------------------------------- idempotent queues


@pytest.mark.parametrize(
    ("build", "job", "namespace"),
    [
        pytest.param(
            IngestionQueue,
            IngestionJob(tenant_id=TENANT, document_id=DOCUMENT),
            "knowledge:ingestion",
            id="ingestion",
        ),
        pytest.param(
            MediaQueue,
            MediaJob(tenant_id=TENANT, media_id=MEDIA),
            "media:understanding",
            id="media",
        ),
    ],
)
async def test_an_idempotent_queue_recovers_even_an_engaged_job(redis, build, job, namespace):
    """Re-running one of these changes nothing anybody can see."""
    queue = build(redis, visibility_timeout_seconds=TIMEOUT)
    await queue.enqueue(job, now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    await queue.mark_engaged(raw, now=NOW)

    outcomes = await queue.recover_expired(policy=IDEMPOTENT_RETRY, now=EXPIRED, jitter=0.0)

    assert [outcome.action for outcome in outcomes] == ["requeued"]
    assert await queue.delayed_depth() == 1


def test_only_the_agent_queue_declares_itself_unsafe_to_repeat(redis):
    """One flag, read by recovery, rather than a list recovery has to be told."""
    assert AgentQueue(redis).idempotent is False
    assert IngestionQueue(redis).idempotent is True
    assert MediaQueue(redis).idempotent is True


# ------------------------------------------------------------ adoption


async def test_an_inflight_entry_with_no_reservation_is_adopted(redis):
    """A crash in the microseconds between claiming a job and recording it.

    It is not recovered on the spot: adopting it starts the clock, so the next
    pass judges it by the same rule as everything else. Recovering immediately
    would race a worker that is reserving right now.
    """
    queue = agent_queue(redis)
    orphan = JobEnvelope.wrap(
        AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION).encode(), now=NOW
    ).encode()
    redis.lists["agent:jobs:inflight"] = [orphan]

    assert await queue.recover_expired(policy=POLICY, now=NOW) == []
    adopted = await queue.reservation(orphan, now=NOW)
    assert adopted is not None
    assert adopted.stage is ReservationStage.UNKNOWN

    later = NOW + timedelta(seconds=TIMEOUT + 1)
    outcomes = await queue.recover_expired(policy=POLICY, now=later, jitter=0.0)
    assert len(outcomes) == 1


async def test_an_adopted_agent_job_is_quarantined_rather_than_guessed_at(redis):
    """A stage nobody recorded is the case where guessing costs a customer."""
    queue = agent_queue(redis)
    orphan = JobEnvelope.wrap(
        AgentJob(tenant_id=TENANT, conversation_id=CONVERSATION).encode(), now=NOW
    ).encode()
    redis.lists["agent:jobs:inflight"] = [orphan]
    await queue.recover_expired(policy=POLICY, now=NOW)

    later = NOW + timedelta(seconds=TIMEOUT + 1)
    outcomes = await queue.recover_expired(policy=POLICY, now=later, jitter=0.0)

    assert [outcome.action for outcome in outcomes] == ["quarantined"]


async def test_an_unreadable_reservation_is_treated_as_expired(redis):
    """A record that cannot be parsed must not pin a job in flight for ever."""
    queue, raw = await reserved_agent_job(redis)
    redis.strings["agent:jobs:reservations"][raw] = "not json at all"

    outcomes = await queue.recover_expired(policy=POLICY, now=NOW, jitter=0.0)

    assert len(outcomes) == 1


# ------------------------------------------------------------- observability


async def test_expired_reservations_are_countable_without_reclaiming_them(redis):
    """A scrape must not change what it measures."""
    queue, _ = await reserved_agent_job(redis)

    assert await queue.expired_depth(now=NOW) == 0
    assert await queue.expired_depth(now=EXPIRED) == 1
    # Counting did not reclaim it.
    assert await queue.inflight_depth() == 1


async def test_an_unleased_inflight_entry_counts_as_expired(redis):
    """It is every bit as stuck as one whose lease ran out."""
    queue = agent_queue(redis)
    redis.lists["agent:jobs:inflight"] = ["{}"]

    assert await queue.expired_depth(now=NOW) == 1


# ---------------------------------------------------------------- policy


def test_the_default_visibility_timeout_is_bounded_and_sane():
    """Long enough to outlast a renewal interval, short enough to notice a death."""
    assert 30.0 <= DEFAULT_VISIBILITY_TIMEOUT_SECONDS <= 900.0


def test_a_crash_is_retryable_but_still_spends_a_attempt():
    """Both halves of the crash category, stated together.

    It is in `RETRYABLE`, so a crashed job is worth another go; and it goes
    through `should_retry`, so the attempt ceiling applies to it exactly as it
    applies to a provider error. A crash is a machine problem, not a free pass.
    """
    assert FailureCategory.WORKER_CRASHED in RETRYABLE
    assert POLICY.should_retry(FailureCategory.WORKER_CRASHED, attempt=1)
    assert not POLICY.should_retry(FailureCategory.WORKER_CRASHED, attempt=POLICY.max_attempts)
    assert not NO_RETRY.should_retry(FailureCategory.WORKER_CRASHED, attempt=1)


def test_an_uncertain_delivery_is_never_retryable():
    """The category exists to be terminal; if it were retryable it would resend."""
    assert FailureCategory.UNCERTAIN_DELIVERY not in RETRYABLE
    assert FailureCategory.UNCERTAIN_DELIVERY in NEEDS_OPERATOR


def test_a_reservation_round_trips(redis):
    reservation = Reservation(
        worker="w1",
        reserved_at=NOW,
        lease_until=NOW + timedelta(seconds=TIMEOUT),
        stage=ReservationStage.ENGAGED,
    )

    decoded = Reservation.decode(reservation.encode(), now=NOW)

    assert decoded == reservation


async def test_a_quarantine_record_names_the_conversation_to_open(redis):
    """The runbook step for an uncertain delivery is "open the conversation".

    An operator should not have to parse a nested JSON string to find out
    which one, so recovery identifies the job rather than leaving the
    identifiers inside the opaque body.
    """
    queue, raw = await reserved_agent_job(redis)
    await queue.mark_engaged(raw, now=NOW)

    await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)

    record = json.loads(redis.lists["agent:jobs:failed"][0])
    assert record["tenant_id"] == str(TENANT)
    assert record["job_id"] == str(CONVERSATION)
    assert record["category"] == "uncertain_delivery"


async def test_a_recovered_job_that_will_not_decode_is_still_recorded(redis):
    """Identifying must never be the reason a failure goes unwritten."""
    queue = agent_queue(redis)
    await queue.enqueue_body("not a job at all", now=NOW)
    raw = await queue.reserve(wait_seconds=1, now=NOW)
    await queue.mark_engaged(raw, now=NOW)

    outcomes = await queue.recover_expired(policy=POLICY, now=EXPIRED, jitter=0.0)

    assert [outcome.action for outcome in outcomes] == ["quarantined"]
    record = json.loads(redis.lists["agent:jobs:failed"][0])
    assert "tenant_id" not in record
    assert record["body"] == "not a job at all"


@pytest.mark.parametrize(
    ("build", "job", "subject"),
    [
        pytest.param(
            IngestionQueue,
            IngestionJob(tenant_id=TENANT, document_id=DOCUMENT),
            DOCUMENT,
            id="ingestion",
        ),
        pytest.param(MediaQueue, MediaJob(tenant_id=TENANT, media_id=MEDIA), MEDIA, id="media"),
    ],
)
def test_every_queue_can_name_its_own_subject(redis, build, job, subject):
    queue = build(redis)

    assert queue.identify(job.encode()) == (TENANT, subject)
    assert queue.identify("nonsense") == (None, None)
