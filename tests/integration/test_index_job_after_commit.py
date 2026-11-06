"""An indexing job reaches Redis only after the generation it names has committed (RGM-16).

The order was: the knowledge service published the job inside the request's
transaction, and `CommittingRoute` committed after the handler returned. A
worker polling the queue in between claimed a job for a generation it could
not see yet, failed with "not found" and retried about three seconds later;
and a request that rolled back after the publish left a job for a document
that never existed.

These tests make the window certain rather than likely, without sleeping:

- the witness queue looks, at the instant of every publish, from a connection
  of its own, for the pending generation of the document it is publishing -
  under the old order it is never there, under the new one it always is;
- the gate holds a real request's transaction open after the service has run
  and before the route commits, so a test can look at Redis, and run a real
  worker against it, inside the window;
- the injected failure rolls a real request back after the publish point.

A publish that fails after the commit is not a failed upload: the request
answers as it always did, and `IngestionRecoveryWorker` - the sweep that
already re-queues work owed a worker - publishes the job once, with its own
workspace's identity, never beside a job that is coming or a lease that is
live (EQ-02..EQ-04). The sweep is run by the test, never waited for.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

import app.workers.queues as queues_module
from app.core.redis import RedisClient
from app.db.models.knowledge import Document, DocumentIndexGeneration, GenerationState
from app.services.knowledge_service import KnowledgeService
from app.workers.ingestion_queue import IngestionJob, IngestionQueue
from app.workers.ingestion_recovery import IngestionRecoveryWorker
from app.workers.knowledge_repair import reindex_all
from app.workers.queue import JobEnvelope
from tests.integration.test_upload_dedupe_concurrency import (
    PRICE_LIST,
    Space,
    Upload,
    Uploads,
    uploads,  # noqa: F401 - the fixture, reused as it is
)

pytestmark = pytest.mark.integration

API = "/api/v1/knowledge"
#: How long a test waits for anything before it fails rather than hangs.
WAIT_SECONDS = 15.0
#: How long the worker polls the queue while a request's transaction is held.
POLL_WHILE_HELD_SECONDS = 2.0
OTHER_TEXT = "سياسة الاسترجاع: يمكن إرجاع المنتج خلال ١٤ يومًا.\nReturns within 14 days."


class WitnessQueue(IngestionQueue):
    """The workspace's queue, which notes what a stranger could see at each publish."""

    def __init__(self, redis: Any, *, namespace: str, engine: AsyncEngine) -> None:
        super().__init__(redis, namespace=namespace, visibility_timeout_seconds=60)
        self._engine = engine
        #: (document id, pending generations of it visible from another connection)
        self.seen: list[tuple[uuid.UUID, int]] = []
        self.fail_next = 0

    async def enqueue(self, job: IngestionJob, *, now: datetime | None = None) -> None:
        async with self._engine.connect() as stranger:
            visible = await stranger.scalar(
                select(func.count())
                .select_from(DocumentIndexGeneration)
                .where(
                    DocumentIndexGeneration.document_id == job.document_id,
                    DocumentIndexGeneration.state == GenerationState.PENDING,
                )
            )
            await stranger.rollback()
        self.seen.append((job.document_id, int(visible or 0)))
        if self.fail_next:
            self.fail_next -= 1
            raise RedisError("injected: Redis refused the publish")
        await super().enqueue(job, now=now)


class Gate:
    """Holds a request inside the service, after it ran and before the route commits."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self) -> None:
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), WAIT_SECONDS)


@pytest_asyncio.fixture
async def witnessed(uploads: Uploads) -> AsyncIterator[Uploads]:  # noqa: F811
    yield uploads


async def _space(api: Uploads, **kwargs: Any) -> tuple[Space, WitnessQueue]:
    space = await api.space(**kwargs)
    queue = WitnessQueue(
        api.indexing.redis,
        namespace=space.namespace,
        engine=api.indexing.database.engine,
    )
    api.queues[space.tenant_id] = queue
    space.queue = queue
    return space, queue


async def _jobs(api: Uploads, namespace: str) -> list[IngestionJob]:
    raw = await api.indexing.redis.lrange(f"{namespace}:pending", 0, -1)  # type: ignore[misc]
    return [IngestionJob.decode(JobEnvelope.decode(item).body) for item in raw]


async def _jobs_for(api: Uploads, namespace: str, document_id: uuid.UUID) -> int:
    return sum(1 for job in await _jobs(api, namespace) if job.document_id == document_id)


async def _retries(api: Uploads, namespace: str) -> int:
    redis = api.indexing.redis
    return int(await redis.zcard(f"{namespace}:delayed")) + int(
        await redis.llen(f"{namespace}:failed")  # type: ignore[misc]
    )


async def _document_id(api: Uploads, space: Space) -> uuid.UUID:
    async with api.indexing.database.session() as session:
        found = await session.scalar(
            select(Document.id).where(Document.tenant_id == space.tenant_id)
        )
    assert found is not None
    return found


async def _generations(api: Uploads, document_id: uuid.UUID) -> list[DocumentIndexGeneration]:
    return await api.indexing.generations(document_id)


def _worker(api: Uploads, namespace: str) -> Any:
    worker = api.indexing.worker()
    worker._queue = IngestionQueue(
        api.indexing.redis, namespace=namespace, visibility_timeout_seconds=60
    )
    return worker


def _sweep(api: Uploads, namespace: str, *, grace_seconds: float) -> IngestionRecoveryWorker:
    sweep = IngestionRecoveryWorker(
        database=api.indexing.database,
        redis=RedisClient(api.indexing.settings),
        settings=api.indexing.settings,
        grace_seconds=grace_seconds,
    )
    sweep._queue = IngestionQueue(
        api.indexing.redis, namespace=namespace, visibility_timeout_seconds=60
    )
    return sweep


async def _drain_one(api: Uploads, namespace: str) -> int:
    worker = _worker(api, namespace)
    consumed = 0
    while await worker.run_once(wait_seconds=1):
        consumed += 1
        assert consumed < 10, "the queue never emptied"
    return consumed


async def _ready(api: Uploads, space: Space, upload: Upload | None = None) -> uuid.UUID:
    """A document of this workspace, uploaded, committed and indexed."""
    response = await api.send(space, upload or Upload())
    assert response.status_code == 202, response.text
    document_id = uuid.UUID(response.json()["document"]["id"])
    assert await _drain_one(api, space.namespace) == 1
    document = await api.indexing.document(document_id)
    assert document is not None and str(document.status) == "ready", document
    return document_id


def _held(monkeypatch: pytest.MonkeyPatch, method: str, gate: Gate) -> None:
    """Make the service's `method` wait at the gate once it has done its work."""
    original: Callable[..., Awaitable[Any]] = getattr(KnowledgeService, method)

    async def held(self: KnowledgeService, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, *args, **kwargs)
        await gate.hold()
        return result

    monkeypatch.setattr(KnowledgeService, method, held)


def _assert_published_after_commit(queue: WitnessQueue, *, expected: int = 1) -> None:
    assert len(queue.seen) == expected, queue.seen
    early = [document for document, visible in queue.seen if visible == 0]
    assert (
        not early
    ), f"{len(early)} job(s) published while their generation was not yet committed: {queue.seen}"


# ------------------------------------------------------------------ Q-1, Q-2


async def test_q1_no_job_is_queued_while_the_upload_transaction_is_open(
    witnessed: Uploads, monkeypatch: pytest.MonkeyPatch
) -> None:
    space, queue = await _space(witnessed)
    gate = Gate()
    _held(monkeypatch, "submit", gate)

    request = asyncio.create_task(witnessed.send(space, Upload()))
    try:
        await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)
        assert (
            await _jobs(witnessed, space.namespace) == []
        ), "the job was queued before the upload committed"
    finally:
        gate.release.set()
    response = await asyncio.wait_for(request, WAIT_SECONDS)

    assert response.status_code == 202, response.text
    document_id = uuid.UUID(response.json()["document"]["id"])
    assert await _jobs_for(witnessed, space.namespace, document_id) == 1
    _assert_published_after_commit(queue)


async def test_q2_a_polling_worker_claims_the_job_only_after_the_commit_and_indexes_it_once(
    witnessed: Uploads, monkeypatch: pytest.MonkeyPatch
) -> None:
    space, _queue = await _space(witnessed)
    gate = Gate()
    _held(monkeypatch, "submit", gate)
    worker = _worker(witnessed, space.namespace)

    request = asyncio.create_task(witnessed.send(space, Upload()))
    claimed_while_held = 0
    try:
        await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)
        deadline = asyncio.get_running_loop().time() + POLL_WHILE_HELD_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await worker.run_once(wait_seconds=1):
                claimed_while_held += 1
    finally:
        gate.release.set()
    response = await asyncio.wait_for(request, WAIT_SECONDS)
    assert response.status_code == 202, response.text
    assert (
        claimed_while_held == 0
    ), f"the worker claimed {claimed_while_held} job(s) for a generation it could not see"

    document_id = uuid.UUID(response.json()["document"]["id"])
    assert await _drain_one(witnessed, space.namespace) == 1
    (generation,) = await _generations(witnessed, document_id)
    assert generation.state == GenerationState.ACTIVE
    assert generation.attempts == 1
    assert await _retries(witnessed, space.namespace) == 0


# ------------------------------------------------------------------ Q-3


async def test_q3_an_upload_that_rolls_back_publishes_nothing(
    witnessed: Uploads, monkeypatch: pytest.MonkeyPatch
) -> None:
    space, queue = await _space(witnessed)

    def refuse(self: KnowledgeService, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected: the audit write failed after the publish point")

    monkeypatch.setattr(KnowledgeService, "_audit_submitted", refuse)
    response = await witnessed.send(space, Upload())

    assert response.status_code == 500
    left = await witnessed.leftovers(space)
    assert (left.documents, left.generations, left.queued) == (0, 0, 0), left
    assert left.orphans == [], left
    assert await _jobs(witnessed, space.namespace) == []
    assert queue.seen == [], f"a job was published for an upload that rolled back: {queue.seen}"


# ------------------------------------------------------------------ Q-4 .. Q-7


async def test_q4_a_publish_that_fails_after_the_commit_is_recovered_by_the_sweep_once(
    witnessed: Uploads,
) -> None:
    space, queue = await _space(witnessed)
    queue.fail_next = 1

    response = await witnessed.send(space, Upload())

    assert response.status_code == 202, response.text
    assert response.json()["created"] is True
    document_id = uuid.UUID(response.json()["document"]["id"])
    _assert_published_after_commit(queue)
    assert await _jobs(witnessed, space.namespace) == []

    outcome = await _sweep(witnessed, space.namespace, grace_seconds=0).run_once()
    assert outcome.queued >= 1
    assert await _jobs_for(witnessed, space.namespace, document_id) == 1
    assert await _drain_one(witnessed, space.namespace) == 1
    (generation,) = await _generations(witnessed, document_id)
    assert generation.state == GenerationState.ACTIVE
    assert generation.attempts == 1


async def test_q5_the_sweep_never_doubles_a_coming_job_a_live_lease_or_a_young_upload(
    witnessed: Uploads,
) -> None:
    space, queue = await _space(witnessed)
    queued = uuid.UUID(
        (await witnessed.send(space, Upload(filename="a.txt"))).json()["document"]["id"]
    )
    leased = uuid.UUID(
        (await witnessed.send(space, Upload(filename="b.txt", content=OTHER_TEXT))).json()[
            "document"
        ]["id"]
    )
    await witnessed.indexing.execute(
        update(DocumentIndexGeneration)
        .where(DocumentIndexGeneration.document_id == leased)
        .values(
            state=GenerationState.PROCESSING, claim_token=uuid.uuid4(), claimed_at=datetime.now(UTC)
        )
    )
    queue.fail_next = 1
    young = uuid.UUID(
        (await witnessed.send(space, Upload(filename="c.txt", content=PRICE_LIST + "\nv3"))).json()[
            "document"
        ]["id"]
    )
    before = {d: await _jobs_for(witnessed, space.namespace, d) for d in (queued, leased, young)}
    assert before == {queued: 1, leased: 1, young: 0}, before

    # The deployed grace period: a lost publish of a second ago is not yet owed.
    await _sweep(witnessed, space.namespace, grace_seconds=300).run_once()

    after = {d: await _jobs_for(witnessed, space.namespace, d) for d in (queued, leased, young)}
    assert after == before, after


async def test_q6_the_sweep_run_twice_publishes_a_lost_job_once(witnessed: Uploads) -> None:
    space, queue = await _space(witnessed)
    queue.fail_next = 1
    document_id = uuid.UUID((await witnessed.send(space, Upload())).json()["document"]["id"])

    sweep = _sweep(witnessed, space.namespace, grace_seconds=0)
    await sweep.run_once()
    await sweep.run_once()

    assert await _jobs_for(witnessed, space.namespace, document_id) == 1
    requests_before = len(witnessed.indexing.provider.requests)
    assert await _drain_one(witnessed, space.namespace) == 1
    (generation,) = await _generations(witnessed, document_id)
    assert generation.state == GenerationState.ACTIVE
    assert generation.attempts == 1
    assert len(witnessed.indexing.provider.requests) > requests_before


async def test_q7_the_sweep_publishes_each_lost_job_with_its_own_workspace(
    witnessed: Uploads,
) -> None:
    first, first_queue = await _space(witnessed)
    second, second_queue = await _space(witnessed)
    first_queue.fail_next = 1
    second_queue.fail_next = 1
    one = uuid.UUID((await witnessed.send(first, Upload())).json()["document"]["id"])
    two = uuid.UUID(
        (await witnessed.send(second, Upload(content=OTHER_TEXT))).json()["document"]["id"]
    )

    shared = f"{witnessed.indexing.namespace}:shared"
    await _sweep(witnessed, shared, grace_seconds=0).run_once()

    owners = {job.document_id: job.tenant_id for job in await _jobs(witnessed, shared)}
    assert owners.get(one) == first.tenant_id
    assert owners.get(two) == second.tenant_id


# ------------------------------------------------------------------ Q-8: every path


async def test_q8_json_upload_publishes_after_commit(witnessed: Uploads) -> None:
    space, queue = await _space(witnessed)
    assert (await witnessed.send(space, Upload(route="json"))).status_code == 202
    _assert_published_after_commit(queue)


async def test_q8_multipart_upload_publishes_after_commit(witnessed: Uploads) -> None:
    space, queue = await _space(witnessed)
    assert (await witnessed.send(space, Upload(route="multipart"))).status_code == 202
    _assert_published_after_commit(queue)


async def test_q8_replace_true_upload_publishes_after_commit(witnessed: Uploads) -> None:
    space, queue = await _space(witnessed)
    await _ready(witnessed, space)
    response = await witnessed.send(space, Upload(content=OTHER_TEXT, replace=True))
    assert response.status_code == 202, response.text
    _assert_published_after_commit(queue, expected=2)


async def test_q8_a_repeat_of_a_failed_upload_publishes_after_commit(witnessed: Uploads) -> None:
    space, queue = await _space(witnessed)
    response = await witnessed.send(space, Upload())
    document_id = uuid.UUID(response.json()["document"]["id"])
    await witnessed.indexing.execute(
        update(DocumentIndexGeneration)
        .where(DocumentIndexGeneration.document_id == document_id)
        .values(state=GenerationState.FAILED)
    )
    assert (await witnessed.send(space, Upload())).status_code == 202
    _assert_published_after_commit(queue, expected=2)


async def test_q8_json_content_replacement_publishes_after_commit(witnessed: Uploads) -> None:
    space, queue = await _space(witnessed)
    document_id = await _ready(witnessed, space)
    response = await witnessed.http.put(
        f"{API}/documents/{document_id}/content",
        json={"content": OTHER_TEXT},
        headers=space.headers,
    )
    assert response.status_code == 202, response.text
    _assert_published_after_commit(queue, expected=2)


async def test_q8_multipart_content_replacement_publishes_after_commit(
    witnessed: Uploads,
) -> None:
    space, queue = await _space(witnessed)
    document_id = await _ready(witnessed, space)
    response = await witnessed.http.put(
        f"{API}/documents/{document_id}/content/upload",
        files={"file": ("prices.txt", OTHER_TEXT.encode(), "text/plain")},
        headers=space.headers,
    )
    assert response.status_code == 202, response.text
    _assert_published_after_commit(queue, expected=2)


async def test_q8_reindex_route_publishes_after_commit(witnessed: Uploads) -> None:
    space, queue = await _space(witnessed)
    document_id = await _ready(witnessed, space)
    response = await witnessed.http.post(
        f"{API}/documents/{document_id}/ingest", headers=space.headers
    )
    assert response.status_code == 202, response.text
    _assert_published_after_commit(queue, expected=2)


async def test_q8_reindex_all_publishes_after_commit(witnessed: Uploads) -> None:
    space, queue = await _space(witnessed)
    await _ready(witnessed, space)
    report = await reindex_all(
        witnessed.indexing.database, queue, workspace=space.tenant_id, rate=0
    )
    assert report.requested == 1
    _assert_published_after_commit(queue, expected=2)


async def test_q8_reindex_document_command_publishes_after_commit(
    witnessed: Uploads, monkeypatch: pytest.MonkeyPatch
) -> None:
    space, queue = await _space(witnessed)
    document_id = await _ready(witnessed, space)
    monkeypatch.setattr(queues_module, "IngestionQueue", lambda *_args, **_kwargs: queue)
    await queues_module._reindex(
        witnessed.indexing.database,
        RedisClient(witnessed.indexing.settings),
        space.tenant_id,
        document_id,
        trigger="reindex",
    )
    _assert_published_after_commit(queue, expected=2)
