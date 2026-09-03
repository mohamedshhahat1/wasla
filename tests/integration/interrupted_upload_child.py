"""A worker that writes an object and is killed before it can record the result.

Not a test - the entry point of one. `test_media_crash_recovery.py` runs this as
a real child process, waits for the line it prints after the object write, and
then terminates it from outside. The kill is the point: nothing here catches a
signal, no `finally` runs, no transaction is rolled back politely, and the
process simply stops existing between the two halves of the write protocol.

That is the failure ADR-087 is built for, and it cannot be produced in-process.
An injected exception unwinds through context managers, closes the session and
rolls the transaction back, which is a tidier ending than a container being
killed mid-job. What this leaves behind is the untidy one.

Deliberately no `test_` prefix, so pytest does not collect it.

Reads its configuration from the environment the parent passes:

    WASLA_CHILD_DATABASE_URL   the test database
    WASLA_CHILD_MEDIA_ID       the row to write an object for
    WASLA_CHILD_TENANT_ID      the workspace that owns it
    TEST_S3_*                  the object store, as the rest of the suite uses

Prints exactly one line - `KEY <object key>` - once the object is written and
the intent is committed, then waits to be killed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.object_store import S3MediaStorage
from app.core.storage import build_key
from app.db.models.media import MediaStorageState, MessageMedia

# The same synthetic bytes the parent expects back.
PNG = b"\x89PNG\r\n\x1a\n" + b"a synthetic photograph" * 16


def _storage() -> S3MediaStorage:
    return S3MediaStorage(
        bucket=os.environ.get("TEST_S3_BUCKET", "wasla-media"),
        access_key_id=os.environ.get("TEST_S3_ACCESS_KEY_ID", ""),
        secret_access_key=os.environ.get("TEST_S3_SECRET_ACCESS_KEY", ""),
        endpoint_url=os.environ["TEST_S3_ENDPOINT_URL"],
        path_style=True,
    )


async def _main() -> None:
    media_id = uuid.UUID(os.environ["WASLA_CHILD_MEDIA_ID"])
    tenant_id = uuid.UUID(os.environ["WASLA_CHILD_TENANT_ID"])

    engine = create_async_engine(os.environ["WASLA_CHILD_DATABASE_URL"])
    maker = async_sessionmaker(engine, expire_on_commit=False)
    storage = _storage()

    key = build_key(tenant_id=tenant_id, mime_type="image/png")

    # TX1: the durable intent. Written and committed before anything exists in
    # the store, which is the property the parent is about to test.
    session: AsyncSession = maker()
    row = await session.get(MessageMedia, media_id)
    assert row is not None
    row.storage_key = key
    row.storage_state = MediaStorageState.PENDING
    row.upload_started_at = datetime.now(UTC)
    row.byte_size = len(PNG)
    row.content_hash = hashlib.sha256(PNG).hexdigest()
    await session.commit()
    await session.close()
    await engine.dispose()

    # The object.
    await storage.put_at(key=key, data=PNG, mime_type="image/png")

    # Told the parent, which kills this process here. Nothing after this line
    # runs, and in particular nothing marks the row stored.
    print(f"KEY {key}", flush=True)
    # Waits to be killed. An `Event` nothing ever sets, rather than a sleep
    # loop, so this blocks for exactly as long as the parent takes and no
    # timer wakes it up in between.
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception as error:  # pragma: no cover - the parent reports the failure
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise
