"""A worker really dies between the object and the row, and the object is recovered.

Everything else in `test_media_write_atomicity.py` injects the failure by
raising, which is honest about the *state* it produces and dishonest about how
tidily it gets there: an exception unwinds through context managers, the session
closes, the transaction is rolled back deliberately. A container being killed
mid-job does none of that.

So this one starts a real Python process, waits for it to say the object is
written, and terminates it from outside. The child holds an open connection and
an unfinished unit of work at the moment it is killed. Nothing catches
anything.

What is asserted afterwards is the whole of P2-D:

    the object is in MinIO
    a committed row names it, and says it is not yet stored
    reconciliation verifies the bytes and finalises
    one object, not two

This is a **real process kill**, not a deterministic transaction injection.
`kill()` is `TerminateProcess` on Windows and `SIGKILL` on POSIX; neither is
catchable.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.object_store import S3MediaStorage
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MediaStorageState, MessageMedia
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services.media_upload_service import MediaUploadReconciler

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"a synthetic photograph" * 16
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
GRACE = 900.0

CHILD = Path(__file__).with_name("interrupted_upload_child.py")
# How long to wait for the child to write an object to a store on this machine.
# Generous, because what is being timed is somebody else's process starting
# Python - and a timeout here is a failed test rather than a flaky assertion,
# since the parent has nothing to assert until the child has spoken.
CHILD_TIMEOUT_SECONDS = 120


def _store() -> S3MediaStorage:
    endpoint = os.environ.get("TEST_S3_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("No object store configured; set TEST_S3_ENDPOINT_URL to run these.")
    return S3MediaStorage(
        bucket=os.environ.get("TEST_S3_BUCKET", "wasla-media"),
        access_key_id=os.environ.get("TEST_S3_ACCESS_KEY_ID", ""),
        secret_access_key=os.environ.get("TEST_S3_SECRET_ACCESS_KEY", ""),
        endpoint_url=endpoint,
        path_style=True,
    )


@pytest_asyncio.fixture
async def committing(prepared_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(prepared_database, pool_size=4, max_overflow=2)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = Tenant(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"2012{uuid.uuid4().int % 10**8:08d}")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(tenant_id=tenant.id, contact_id=contact.id, account_id=account.id)
    session.add(conversation)
    await session.flush()

    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        kind=MessageKind.IMAGE,
        status=MessageStatus.DELIVERED,
    )
    session.add(message)
    await session.flush()

    media = MessageMedia(
        tenant_id=tenant.id,
        message_id=message.id,
        conversation_id=conversation.id,
        wa_media_id=f"wamid-{uuid.uuid4().hex[:10]}",
        mime_type="image/png",
        status=MediaStatus.PENDING,
    )
    session.add(media)
    await session.commit()
    return tenant.id, media.id


async def test_a_killed_worker_leaves_an_object_reconciliation_can_finish(
    committing: async_sessionmaker[AsyncSession],
    prepared_database: str,
) -> None:
    storage = _store()

    async with committing() as setup:
        tenant_id, media_id = await _seed(setup)

    root = Path(__file__).parents[2]
    environment = dict(os.environ)
    environment.update(
        {
            "WASLA_CHILD_DATABASE_URL": prepared_database,
            "WASLA_CHILD_MEDIA_ID": str(media_id),
            "WASLA_CHILD_TENANT_ID": str(tenant_id),
            # A script's `sys.path[0]` is its own directory, not the working
            # directory, so without this the child imports whichever `app` an
            # editable install happens to point at - which in a worktree is a
            # different checkout entirely. pytest does the equivalent for the
            # parent by putting the rootdir on the path.
            "PYTHONPATH": str(root),
        }
    )

    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(CHILD),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=str(root),
    )
    key: str | None = None
    try:
        assert child.stdout is not None
        async with asyncio.timeout(CHILD_TIMEOUT_SECONDS):
            while True:
                raw = await child.stdout.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace")
                if line.startswith("KEY "):
                    key = line[4:].strip()
                    break

        if key is None:
            stderr = b"" if child.stderr is None else await child.stderr.read()
            pytest.fail(f"the child never wrote an object: {stderr.decode(errors='replace')}")

        # Killed here. Between the object and the row, holding an open
        # connection, with nothing to catch it. `kill` is TerminateProcess on
        # Windows and SIGKILL on POSIX - neither can be caught, and no
        # `finally` in the child runs.
        child.kill()
    finally:
        await child.wait()

    assert key is not None
    assert child.returncode != 0, "the child was supposed to be killed, not to exit"

    try:
        # 1. The object is in the store.
        assert await storage.get(key) == PNG

        # 2. A committed row owns it, and does not claim it is readable.
        async with committing() as check:
            row = await check.get(MessageMedia, media_id, populate_existing=True)
            assert row is not None
            assert row.storage_state is MediaStorageState.PENDING
            assert row.storage_key == key
            assert row.is_stored is False

            # Retention cannot touch it, however old the message is.
            row.created_at = NOW - timedelta(days=400)
            row.upload_started_at = NOW - timedelta(seconds=GRACE + 60)
            await check.commit()

        # 3. A replacement process reconciles it.
        async with committing() as session:
            outcome = await MediaUploadReconciler(session=session, storage=storage).run(
                now=NOW, grace_seconds=GRACE, limit=10
            )

        assert outcome.finalized == 1
        assert outcome.missing == 0

        async with committing() as check:
            row = await check.get(MessageMedia, media_id, populate_existing=True)
            assert row is not None
            assert row.storage_state is MediaStorageState.STORED
            # The same object. Nothing wrote a second one to recover the first.
            assert row.storage_key == key
            assert row.is_stored is True
    finally:
        await storage.delete(key)
        async with committing() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.commit()
