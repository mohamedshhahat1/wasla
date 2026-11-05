"""The re-index command and the recovery sweep page through a backlog (RV-12, RV-18).

P-R09: `reindex-stale-embeddings --limit 2` run twice over five stale documents
selected the same two rows both times - the re-index it asked for was coalesced
into the generation already pending, so the second run did nothing and the
docstring's "run it again for the next batch" never reached document three.

P-R14: the recovery sweep re-queued every generation still owed an attempt on
every pass, so a backlog larger than the workers could drain grew the queue by a
batch a minute.

Real PostgreSQL, real Redis, the real commands; fake embeddings at the transport.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.redis import RedisClient
from app.db.models.knowledge import GenerationState
from app.workers.queues import reindex_stale_embeddings
from tests.integration.rag_harness import LARGE, Indexing, respond

pytestmark = pytest.mark.integration

LISTED = re.compile(r"^(?:would )?re-index\s+(\S+)\s+(\S+)$", re.MULTILINE)
CURSOR = re.compile(r"^next page: --after (\S+)$", re.MULTILINE)


async def _stale_run(
    indexing: Indexing,
    capsys: pytest.CaptureFixture[str],
    *,
    after: str | None = None,
    dry_run: bool = False,
) -> tuple[list[uuid.UUID], str | None]:
    redis = RedisClient(indexing.settings)
    try:
        # `after` only when continuing, so the first-page call is the one the
        # command has always accepted.
        paging = {"after": after} if after is not None else {}
        await reindex_stale_embeddings(
            indexing.database, redis, space=LARGE, limit=2, dry_run=dry_run, **paging
        )
    finally:
        await redis.close()
    printed = capsys.readouterr().out
    cursor = CURSOR.search(printed)
    return [uuid.UUID(document) for _, document in LISTED.findall(printed)], (
        cursor.group(1) if cursor else None
    )


async def test_reindex_stale_embeddings_twice_selects_disjoint_documents(
    indexing: Indexing, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant_id = await indexing.workspace()
    documents = []
    for n in range(5):
        documents.append(await indexing.submit(tenant_id, f"Branch {n}\n\nOpens at nine."))
    await indexing.drain()

    first, _ = await _stale_run(indexing, capsys)
    second, _ = await _stale_run(indexing, capsys)
    third, _ = await _stale_run(indexing, capsys)

    assert len(first) == 2 and len(second) == 2 and len(third) == 1
    assert set(first).isdisjoint(second), "the second run must reach the next documents"
    assert set(first) | set(second) | set(third) == set(documents)
    for document_id in documents:
        assert [g.state for g in await indexing.generations(document_id)] == [
            GenerationState.ACTIVE,
            GenerationState.PENDING,
        ], "each document re-indexed once, and still served meanwhile"


async def test_reindex_stale_embeddings_resumes_from_the_printed_cursor(
    indexing: Indexing, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant_id = await indexing.workspace()
    documents = [
        await indexing.submit(tenant_id, f"Branch {n}\n\nOpens at nine.") for n in range(3)
    ]
    await indexing.drain()

    # Dry runs request nothing, so only the cursor can keep the pages apart.
    first, cursor = await _stale_run(indexing, capsys, dry_run=True)
    assert len(first) == 2 and cursor is not None, "a full page prints where to continue"
    rest, final = await _stale_run(indexing, capsys, after=cursor, dry_run=True)
    assert set(rest).isdisjoint(first)
    assert set(first) | set(rest) == set(documents)
    assert final is None, "a short page is the last one"


async def test_a_document_whose_reindex_failed_is_not_swept_again(
    indexing: Indexing, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant_id = await indexing.workspace()
    document_id = await indexing.submit(tenant_id, "Branch\n\nOpens at nine.")
    await indexing.drain()
    indexing.provider.handler = respond(404)
    await indexing.reindex(tenant_id, document_id)
    await indexing.drain()
    indexing.provider.handler = None
    assert [g.state for g in await indexing.generations(document_id)] == [
        GenerationState.ACTIVE,
        GenerationState.FAILED,
    ]

    listed, _ = await _stale_run(indexing, capsys)
    assert document_id not in listed


async def test_the_recovery_sweep_does_not_grow_the_queue_during_a_backlog(
    indexing: Indexing,
) -> None:
    tenant_id = await indexing.workspace()
    for n in range(6):
        await indexing.submit(tenant_id, f"Branch {n}\n\nOpens at nine.", enqueue=False)
    assert await indexing.queued() == 0

    sweep = indexing.recovery()
    sweep._batch_limit = 4
    moment = datetime.now(UTC)
    depths = []
    for minute in range(3):
        await sweep.run_once(now=moment + timedelta(minutes=minute) / 60)
        depths.append(await indexing.queued())
    assert depths == [4, 6, 6], "each owed attempt queued once while its job is on its way"

    # A job that is lost after all - its visibility window has passed and no
    # worker claimed it - is still owed, and the sweep sends it again.
    later = moment + timedelta(seconds=indexing.settings.queue_visibility_timeout_seconds + 5)
    await sweep.run_once(now=later)
    assert await indexing.queued() == 10
