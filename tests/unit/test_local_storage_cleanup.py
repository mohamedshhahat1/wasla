"""A failed local write leaves no staging file behind (MEDIA-14).

The audit's P6: on a real 2 MB tmpfs the third write raised `StorageError`
correctly and left a 248 KB `.partial` at 0 KB free - the failed write holding
the very space whose absence failed it.

Two layers. The failure modes are injected deterministically below, on any
filesystem. And `test_a_full_filesystem_leaves_nothing_behind` runs against a
genuinely full, genuinely small filesystem when one is provided in
`WASLA_TEST_TMPFS` (the remediation evidence mounts a 2 MB tmpfs there); without
one it is skipped rather than faked.
"""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path

import pytest

from app.core.storage import LocalMediaStorage, StorageError, build_key

TENANT = "3f2b8c1e-7d4a-4c9b-9e1f-2a6b5c8d0e7f"


def _key() -> str:
    import uuid

    return build_key(tenant_id=uuid.UUID(TENANT), mime_type="image/png")


def _partials(root: Path) -> list[Path]:
    return list(root.rglob("*.partial"))


async def test_a_write_that_fails_part_way_removes_its_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalMediaStorage(tmp_path)
    real = Path.write_bytes

    def half_then_full(self: Path, data: bytes) -> int:
        real(self, data[: len(data) // 2])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", half_then_full)

    with pytest.raises(StorageError):
        await store.put_at(key=_key(), data=b"x" * 4096)

    assert _partials(tmp_path) == []


async def test_a_failed_rename_removes_the_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalMediaStorage(tmp_path)

    def refuse(self: Path, target: object) -> Path:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(Path, "replace", refuse)

    with pytest.raises(StorageError):
        await store.put_at(key=_key(), data=b"y" * 1024)

    assert _partials(tmp_path) == []


async def test_a_cancelled_write_removes_its_partial_and_stays_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalMediaStorage(tmp_path)
    real = Path.write_bytes

    def cancelled(self: Path, data: bytes) -> int:
        real(self, data)
        raise asyncio.CancelledError

    monkeypatch.setattr(Path, "write_bytes", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await store.put_at(key=_key(), data=b"z" * 1024)

    assert _partials(tmp_path) == []


async def test_a_cleanup_that_itself_fails_does_not_hide_the_storage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalMediaStorage(tmp_path)

    def full(self: Path, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    def stuck(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "write_bytes", full)
    monkeypatch.setattr(Path, "unlink", stuck)

    with pytest.raises(StorageError) as raised:
        await store.put_at(key=_key(), data=b"w" * 1024)

    assert isinstance(raised.value.__cause__, OSError)
    assert raised.value.__cause__.errno == errno.ENOSPC


async def test_a_successful_write_leaves_only_the_object(tmp_path: Path) -> None:
    store = LocalMediaStorage(tmp_path)
    key = _key()

    await store.put_at(key=key, data=b"ok" * 100)

    assert _partials(tmp_path) == []
    assert await store.get(key) == b"ok" * 100


async def test_a_full_filesystem_leaves_nothing_behind() -> None:
    """P6 on a real small filesystem: write until it is full, then check."""
    root = os.environ.get("WASLA_TEST_TMPFS")
    if not root:
        pytest.skip("No size-limited filesystem provided in WASLA_TEST_TMPFS.")
    store = LocalMediaStorage(Path(root))
    chunk = b"p" * (900 * 1024)
    written: list[str] = []
    failures = 0
    for _ in range(4):
        key = _key()
        try:
            await store.put_at(key=key, data=chunk)
            written.append(key)
        except StorageError:
            failures += 1

    # Presence first: the filesystem really did fill, after real writes.
    assert written and failures >= 1
    assert _partials(Path(root)) == []
    for key in written:
        assert await store.get(key) == chunk
