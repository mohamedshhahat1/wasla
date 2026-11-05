"""Two uploads of the same text at the same moment end as two uploads one after the other (RGM-05).

The duplicate checks - the same text in this knowledge base, the same source
key, the same text in another knowledge base of the workspace - decide only if
they run under the workspace's upload lock, the advisory lock the plan's
document slot is reserved under. These tests make the overlap certain rather
than likely: the test holds that lock itself, from its own connection, with
the key computed by the application's own function, starts the uploads
through the real routes, waits until every one of them is queued on the lock,
and only then lets go. Run in the order the checks used to run - before the
lock - every request has already decided "new document" by then, and each
case fails every time; run under it, one request creates the document and
every other request gets exactly the answer a second, sequential upload gets.

The sequential answer is not written down here: each case measures it, on a
fresh workspace of the same shape, through the same routes. A loser must also
leave nothing behind - no document, generation or audit entry claiming one,
no job, and no original that is neither referenced nor owed to the purge
worker.

What else the lock must not do is pinned beside it: two workspaces never wait
for each other, a full plan with one slot left still admits exactly one new
document, and two different texts are both accepted. That a slow extraction
holds no lock is `test_upload_concurrency.py`'s
`test_two_uploads_to_one_workspace_succeed_while_a_slow_pdf_parses` (UL-05).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select, text

from app.api.dependencies import ActiveWorkspaceDep, get_knowledge_service
from app.core.dependencies import RedisDep, SessionDep, SettingsDep
from app.core.redis import RedisClient
from app.core.security import create_access_token
from app.core.storage import LocalMediaStorage
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.billing import LimitKey
from app.db.models.enums import TenantRole
from app.db.models.knowledge import Document, DocumentIndexGeneration, KnowledgeBase
from app.db.models.media_purge import MediaPurgeObject
from app.db.models.membership import Membership
from app.db.models.user import User
from app.db.session import Database
from app.main import create_app
from app.repositories.knowledge_repository import DocumentRepository
from app.services.entitlement_service import _lock_id
from app.services.knowledge_service import KnowledgeService
from app.workers.ingestion_queue import IngestionQueue
from tests.channel_plans import allow_channels
from tests.integration.ai_harness import settings_for
from tests.integration.rag_harness import Indexing

pytestmark = pytest.mark.integration

API = "/api/v1/knowledge"
PRICE_LIST = (
    "قائمة الأسعار\n\nتشطيب سوبر لوكس: ٤٥٠٠ جنيه للمتر.\n"
    "Super lux finishing: 4500 EGP per square metre."
)
#: How long a test waits for every request to queue on the lock before it
#: fails, rather than hangs. Well inside the app's lock timeout below.
QUEUE_WAIT_SECONDS = 8.0
LOCK_TIMEOUT_MS = 20_000
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?")


@dataclass(frozen=True)
class Upload:
    """One upload request: which route, into which knowledge base, with what."""

    route: str = "json"
    base: int = 0
    content: str = PRICE_LIST
    filename: str | None = "prices.txt"
    replace: bool = False


@dataclass
class Space:
    """A seeded workspace: its owner's headers and its knowledge bases."""

    tenant_id: uuid.UUID
    headers: dict[str, str]
    bases: list[uuid.UUID]
    queue: IngestionQueue
    namespace: str


@dataclass
class Uploads:
    """The real API over the indexing harness's database and Redis."""

    indexing: Indexing
    http: httpx.AsyncClient
    storage_root: Path
    users: list[uuid.UUID] = field(default_factory=list)
    queues: dict[uuid.UUID, IngestionQueue] = field(default_factory=dict)

    async def space(self, *, bases: int = 1, document_limit: int | None = None) -> Space:
        tenant_id = await self.indexing.workspace()
        async with self.indexing.database.session() as session:
            user = User(
                email=f"uploader-{uuid.uuid4().hex[:10]}@example.com",
                full_name="Uploader",
                hashed_password="x",
                is_active=True,
                email_verified_at=datetime.now(UTC),
                platform_role=None,
            )
            session.add(user)
            await session.flush()
            session.add(
                Membership(tenant_id=tenant_id, user_id=user.id, role=TenantRole.TENANT_OWNER)
            )
            made = [KnowledgeBase(tenant_id=tenant_id, name=f"Base {n}") for n in range(bases)]
            session.add_all(made)
            if document_limit is not None:
                await allow_channels(
                    session,
                    tenant_id,
                    limits={LimitKey.KNOWLEDGE_DOCUMENTS.value: document_limit},
                )
            await session.flush()
            token, _ = create_access_token(
                settings=self.indexing.settings,
                subject=user.id,
                tenant_id=tenant_id,
                token_version=user.token_version,
            )
            base_ids = [base.id for base in made]
            self.users.append(user.id)
        namespace = f"{self.indexing.namespace}:{tenant_id.hex[:8]}"
        queue = IngestionQueue(
            self.indexing.redis, namespace=namespace, visibility_timeout_seconds=60
        )
        self.queues[tenant_id] = queue
        return Space(
            tenant_id=tenant_id,
            headers={"Authorization": f"Bearer {token}"},
            bases=base_ids,
            queue=queue,
            namespace=namespace,
        )

    async def send(self, space: Space, upload: Upload) -> httpx.Response:
        base = space.bases[upload.base]
        params = {"replace": "true"} if upload.replace else None
        if upload.route == "json":
            body: dict[str, Any] = {"title": "Price list", "content": upload.content}
            if upload.filename is not None:
                body["filename"] = upload.filename
            return await self.http.post(
                f"{API}/bases/{base}/documents", json=body, params=params, headers=space.headers
            )
        return await self.http.post(
            f"{API}/bases/{base}/documents/upload",
            files={
                "file": (upload.filename or "prices.txt", upload.content.encode(), "text/plain")
            },
            data={"title": "Price list"},
            params=params,
            headers=space.headers,
        )

    async def at_once(self, space: Space, uploads: Sequence[Upload]) -> list[httpx.Response]:
        """Start every upload while the workspace's lock is held, release it once all queue."""
        key = _lock_id(space.tenant_id, LimitKey.KNOWLEDGE_DOCUMENTS)
        async with self.indexing.database.engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            await holder.commit()
            tasks = [asyncio.create_task(self.send(space, upload)) for upload in uploads]
            try:
                await self._until_queued(holder, key, len(uploads), tasks)
            finally:
                await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await holder.commit()
            return list(await asyncio.wait_for(asyncio.gather(*tasks), QUEUE_WAIT_SECONDS * 3))

    async def _until_queued(
        self,
        holder: Any,
        key: int,
        expected: int,
        tasks: Sequence[asyncio.Task[httpx.Response]],
    ) -> None:
        deadline = asyncio.get_running_loop().time() + QUEUE_WAIT_SECONDS
        while True:
            waiting = await self.waiting(holder, key)
            if waiting >= expected:
                return
            finished = [task for task in tasks if task.done()]
            if finished:
                answers = [_answer(task.result()) for task in finished]
                pytest.fail(f"{len(finished)} upload(s) finished without the lock: {answers}")
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail(f"only {waiting} of {expected} uploads queued on the lock")
            await asyncio.sleep(0.05)

    @staticmethod
    async def waiting(holder: Any, key: int) -> int:
        result = await holder.execute(
            text(
                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND NOT l.granted "
                "AND a.datname = current_database() AND l.objid = :objid AND l.objsubid = 1"
            ),
            {"objid": key & 0xFFFFFFFF},
        )
        await holder.commit()
        return int(result.scalar_one())

    async def sequential(
        self, uploads: Sequence[Upload], *, bases: int = 1
    ) -> tuple[list[dict[str, Any]], Leftovers]:
        """The answers of `uploads` sent one after the other, on a fresh workspace."""
        space = await self.space(bases=bases)
        answers = [_answer(await self.send(space, upload)) for upload in uploads]
        return answers, await self.leftovers(space)

    async def leftovers(self, space: Space) -> Leftovers:
        tenant = space.tenant_id
        async with self.indexing.database.session() as session:
            documents = int(
                await session.scalar(
                    select(func.count(Document.id)).where(Document.tenant_id == tenant)
                )
                or 0
            )
            generations = list(
                await session.scalars(
                    select(DocumentIndexGeneration).where(
                        DocumentIndexGeneration.tenant_id == tenant
                    )
                )
            )
            created = [
                row
                for row in await session.scalars(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant,
                        AuditLog.action == AuditAction.KNOWLEDGE_DOCUMENT_SUBMITTED,
                    )
                )
                if (row.meta or {}).get("created") is True
            ]
            owed = set(
                await session.scalars(
                    select(MediaPurgeObject.storage_key).where(MediaPurgeObject.tenant_id == tenant)
                )
            )
            usage = int(
                await session.scalar(
                    text("SELECT count(*) FROM usage_events WHERE tenant_id = :t"), {"t": tenant}
                )
                or 0
            )
        referenced = {g.original_object_key for g in generations if g.original_object_key}
        stored = {
            path.relative_to(self.storage_root).as_posix()
            for path in self.storage_root.rglob("*")
            if path.is_file() and str(tenant) in path.as_posix()
        }
        return Leftovers(
            documents=documents,
            generations=len(generations),
            audit_created=len(created),
            queued=int(await self.indexing.redis.llen(f"{space.namespace}:pending")),  # type: ignore[misc]
            usage=usage,
            orphans=sorted(stored - referenced - owed),
            referenced_present=referenced <= stored,
        )

    async def cleanup(self) -> None:
        async with self.indexing.database.session() as session:
            if self.indexing.tenants:
                await session.execute(
                    delete(MediaPurgeObject).where(
                        MediaPurgeObject.tenant_id.in_(self.indexing.tenants)
                    )
                )
            if self.users:
                await session.execute(delete(User).where(User.id.in_(self.users)))


@dataclass(frozen=True)
class Leftovers:
    documents: int
    generations: int
    audit_created: int
    queued: int
    usage: int
    orphans: list[str]
    referenced_present: bool


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k != "request_id"}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return _MOMENT.sub("<moment>", _UUID.sub("<id>", value))
    return value


def _answer(response: httpx.Response) -> dict[str, Any]:
    """Status and body, without the ids and moments that differ between workspaces."""
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    return {"status": response.status_code, "body": _scrub(body)}


def _winner_first(
    responses: Sequence[httpx.Response], uploads: Sequence[Upload]
) -> tuple[list[Upload], list[dict[str, Any]]]:
    """The uploads in the order they took effect - the creator first - and their answers."""
    assert all(r.status_code < 500 for r in responses), [_answer(r) for r in responses]
    created = [i for i, r in enumerate(responses) if r.status_code == 202 and r.json()["created"]]
    assert len(created) == 1, [_answer(r) for r in responses]
    order = created + [i for i in range(len(uploads)) if i not in created]
    return [uploads[i] for i in order], [_answer(responses[i]) for i in order]


async def _assert_like_sequential(
    uploads: Uploads, space: Space, uploads_sent: Sequence[Upload], *, bases: int
) -> list[httpx.Response]:
    responses = await uploads.at_once(space, uploads_sent)
    ordered, answers = _winner_first(responses, uploads_sent)
    expected, expected_left = await uploads.sequential(ordered, bases=bases)
    assert answers == expected, {"concurrent": answers, "sequential": expected}
    left = await uploads.leftovers(space)
    assert left == expected_left, {"concurrent": left, "sequential": expected_left}
    assert left.documents == 1
    assert left.audit_created == 1
    # The queue is pinned by the equality above: a repeat nudges the document's
    # pending job again, exactly as it does one after the other.
    assert left.orphans == []
    assert left.referenced_present
    return responses


@pytest_asyncio.fixture
async def uploads(indexing: Indexing, tmp_path: Path) -> AsyncIterator[Uploads]:
    settings = settings_for(
        indexing.url,
        rate_limit_enabled=False,
        media_storage_backend="local",
        media_storage_path=str(tmp_path),
        database_lock_timeout_ms=LOCK_TIMEOUT_MS,
        log_level="CRITICAL",
    )
    indexing.settings = settings
    application: FastAPI = create_app(settings)
    database = Database(settings)
    application.state.database = database
    application.state.redis = RedisClient(settings)
    queues: dict[uuid.UUID, IngestionQueue] = {}

    def scoped(
        session: SessionDep,
        redis: RedisDep,
        workspace: ActiveWorkspaceDep,
        settings: SettingsDep,
    ) -> KnowledgeService:
        service = get_knowledge_service(session, redis, workspace, settings)
        # The workspace's own queue and a temporary store, so a test counts only
        # its own jobs and originals.
        service._queue = queues[workspace.tenant.id]
        service._storage = LocalMediaStorage(str(tmp_path))
        return service

    application.dependency_overrides[get_knowledge_service] = scoped
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://wasla.test",
    ) as http:
        harness = Uploads(indexing=indexing, http=http, storage_root=tmp_path, queues=queues)
        try:
            yield harness
        finally:
            await harness.cleanup()
            await database.dispose()


# ------------------------------------------------------------------ the cases


async def test_c1_one_knowledge_base_two_uploads_of_one_file(uploads: Uploads) -> None:
    space = await uploads.space()
    await _assert_like_sequential(uploads, space, [Upload(), Upload()], bases=1)


async def test_c1_multipart_one_knowledge_base_two_uploads_of_one_file(uploads: Uploads) -> None:
    space = await uploads.space()
    same = Upload(route="multipart")
    await _assert_like_sequential(uploads, space, [same, same], bases=1)


async def test_c2_two_knowledge_bases_of_one_workspace_one_text(uploads: Uploads) -> None:
    space = await uploads.space(bases=2)
    responses = await _assert_like_sequential(
        uploads, space, [Upload(base=0), Upload(base=1)], bases=2
    )
    refused = [r for r in responses if r.status_code == 409]
    assert [r.json()["error"]["code"] for r in refused] == [
        "document_exists_in_other_knowledge_base"
    ]


async def test_c3_five_uploads_across_two_knowledge_bases(uploads: Uploads) -> None:
    space = await uploads.space(bases=2)
    sent = [Upload(base=0), Upload(base=0), Upload(base=0), Upload(base=1), Upload(base=1)]
    await _assert_like_sequential(uploads, space, sent, bases=2)


async def test_c4_json_against_multipart_one_text(uploads: Uploads) -> None:
    space = await uploads.space()
    sent = [Upload(route="json", filename=None), Upload(route="multipart", filename="list.txt")]
    await _assert_like_sequential(uploads, space, sent, bases=1)


async def test_c2b_same_text_under_another_filename(uploads: Uploads) -> None:
    space = await uploads.space()
    sent = [Upload(filename="prices.txt"), Upload(filename="prices-copy.txt")]
    await _assert_like_sequential(uploads, space, sent, bases=1)


async def test_c5_two_workspaces_never_wait_for_each_other(uploads: Uploads) -> None:
    first = await uploads.space()
    second = await uploads.space()
    key = _lock_id(first.tenant_id, LimitKey.KNOWLEDGE_DOCUMENTS)
    async with uploads.indexing.database.engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        await holder.commit()
        blocked = asyncio.create_task(uploads.send(first, Upload()))
        try:
            await uploads._until_queued(holder, key, 1, [blocked])
            # The first workspace's lock is still held here.
            other = await asyncio.wait_for(uploads.send(second, Upload()), QUEUE_WAIT_SECONDS)
            assert other.status_code == 202 and other.json()["created"], _answer(other)
            assert not blocked.done()
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            await holder.commit()
        mine = await asyncio.wait_for(blocked, QUEUE_WAIT_SECONDS)
    assert mine.status_code == 202 and mine.json()["created"], _answer(mine)


async def test_c6_a_plan_with_one_slot_left_admits_one_new_document(uploads: Uploads) -> None:
    space = await uploads.space(document_limit=2)
    first = await uploads.send(
        space, Upload(content="Opening hours are nine to five.", filename="hours.txt")
    )
    assert first.status_code == 202, _answer(first)
    responses = await uploads.at_once(
        space,
        [
            Upload(content="Delivery takes three days.", filename="delivery.txt"),
            Upload(content="Warranty is one year.", filename="warranty.txt"),
        ],
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [202, 402], [_answer(r) for r in responses]
    refused = next(r for r in responses if r.status_code == 402)
    assert refused.json()["error"]["code"] == "plan_limit_exceeded"
    left = await uploads.leftovers(space)
    assert left.documents == 2
    assert left.orphans == []


async def test_c7_two_different_texts_into_one_knowledge_base_are_both_kept(
    uploads: Uploads,
) -> None:
    space = await uploads.space()
    responses = await uploads.at_once(
        space,
        [
            Upload(content="Delivery takes three days.", filename="delivery.txt"),
            Upload(content="Warranty is one year.", filename="warranty.txt"),
        ],
    )
    assert [(r.status_code, r.json()["created"]) for r in responses] == [(202, True), (202, True)]
    assert (await uploads.leftovers(space)).documents == 2


async def test_c8_a_replacing_upload_against_a_plain_one_of_the_same_file(
    uploads: Uploads,
) -> None:
    space = await uploads.space()
    await _assert_like_sequential(
        uploads, space, [Upload(replace=True), Upload(replace=False)], bases=1
    )


async def test_c8_a_replacing_upload_of_new_text_against_a_plain_one(uploads: Uploads) -> None:
    """Replace races a plain upload of the same file whose source already exists."""
    space = await uploads.space()
    seeded = await uploads.send(space, Upload(content="Super lux finishing: 4200 EGP."))
    assert seeded.status_code == 202, _answer(seeded)
    sent = [Upload(replace=True), Upload(replace=False)]
    responses = await uploads.at_once(space, sent)
    assert all(r.status_code < 500 for r in responses), [_answer(r) for r in responses]
    left = await uploads.leftovers(space)
    assert left.documents == 1, [_answer(r) for r in responses]
    assert left.orphans == []
    # Whichever took effect first, the other got what it would have got second.
    replaced = next(i for i, r in enumerate(responses) if r.status_code == 202)
    order = [sent[replaced], sent[1 - replaced]]
    reference = await uploads.space()
    await uploads.send(reference, Upload(content="Super lux finishing: 4200 EGP."))
    expected = [_answer(await uploads.send(reference, upload)) for upload in order]
    assert [_answer(responses[replaced]), _answer(responses[1 - replaced])] == expected


async def test_c9_unforced_bursts_never_answer_5xx_or_store_a_text_twice(
    uploads: Uploads,
) -> None:
    outcomes: list[list[int]] = []
    for round_number in range(20):
        space = await uploads.space(bases=2)
        width = 2 + round_number % 4
        content = f"{PRICE_LIST}\nRound {round_number}."
        sent = [
            Upload(
                route="multipart" if n % 2 else "json",
                base=n % 2 if round_number % 3 == 0 else 0,
                content=content,
            )
            for n in range(width)
        ]
        responses = await asyncio.gather(*(uploads.send(space, upload) for upload in sent))
        outcomes.append([r.status_code for r in responses])
        assert all(r.status_code < 500 for r in responses), [_answer(r) for r in responses]
        left = await uploads.leftovers(space)
        assert left.documents == 1, (round_number, [_answer(r) for r in responses])
        assert left.orphans == []


async def test_a_unique_violation_past_the_checks_gets_the_repeat_answer(
    uploads: Uploads, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop: should an insert still meet the knowledge base's unique key, the
    request answers as a repeat upload does - never a 5xx (UL-03, M-U04)."""
    space = await uploads.space()
    first = await uploads.send(space, Upload())
    assert first.status_code == 202, _answer(first)
    expected, _ = await uploads.sequential([Upload(), Upload()])

    real = DocumentRepository.get_by_hash
    blinded: list[int] = []

    async def blind_once(self: DocumentRepository, **kwargs: Any) -> Any:
        # The first look misses the existing document, as a check that ran
        # before a concurrent commit would have.
        if not blinded:
            blinded.append(1)
            return None
        return await real(self, **kwargs)

    monkeypatch.setattr(DocumentRepository, "get_by_hash", blind_once)
    monkeypatch.setattr(DocumentRepository, "get_by_hash_in_workspace", _none)
    monkeypatch.setattr(DocumentRepository, "get_by_source_key", _none_once(blinded))
    second = await uploads.send(space, Upload())

    assert blinded == [1]
    assert _answer(second) == expected[1]
    left = await uploads.leftovers(space)
    assert left.documents == 1
    assert left.orphans == []


async def _none(self: DocumentRepository, **kwargs: Any) -> None:
    return None


def _none_once(seen: list[int]) -> Any:
    real = DocumentRepository.get_by_source_key

    async def lookup(self: DocumentRepository, **kwargs: Any) -> Any:
        if len(seen) == 1 and not getattr(lookup, "used", False):
            lookup.used = True  # type: ignore[attr-defined]
            return None
        return await real(self, **kwargs)

    return lookup
